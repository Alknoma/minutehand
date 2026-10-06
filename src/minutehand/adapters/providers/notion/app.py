"""Notion's public API, version 2022-06-28, as one ASGI app over the run's store and clock.

**Sign-in.** Every `/v1` call carries `Authorization: Bearer <secret>`: a seeded internal
integration's token or an access token minted by `/v1/oauth/token`. An unknown, missing or
revoked secret is 401 `unauthorized`. The token names the integration, its bot user and
its workspace; every call acts as that bot. `/v1/oauth/token` takes `Authorization: Basic`
with a public integration's client id and secret, and a JSON body: an authorization code
the seed holds (single use) or a refresh token it minted.

**Version.** `Notion-Version` must be present (400 `missing_version`) and must be
`2022-06-28`: a later version changes shapes this fake does not serve, and is refused with
a `validation_error` that says so, never answered in the wrong shape.

**Sharing.** An integration reaches a page or database only when it, or a page or database
above it, is shared with the integration; everything else answers `object_not_found`,
and search leaves it out. A capability the integration lacks answers 403
`restricted_resource`.

**Faults** the seed arms are answered before the call is: `rate_limited` with
`Retry-After`, and `conflict_error` on block edits. Each firing is recorded.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote

from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match, Route, Router
from starlette.types import Receive, Scope, Send

from minutehand.adapters import answering
from minutehand.adapters.providers.notion import query as notion_query
from minutehand.adapters.providers.notion import webhooks, wire
from minutehand.adapters.providers.notion.edits import Editor
from minutehand.adapters.providers.notion.state import NotionWorld, is_row, page_ref, record_ref, title_of
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

JSON = "application/json; charset=utf-8"
EDITS_BLOCKS = ("PATCH", "DELETE")


@dataclass
class Call:
    """Who is calling, read from its token."""

    integration: wire.StoredIntegration
    secret: str

    @property
    def bot(self) -> str:
        return self.integration.id

    @property
    def workspace(self) -> str:
        return self.integration.workspace


Handler = Callable[[Request, Call], Awaitable[Response]]


def _answer(found: JsonValue, status: int = 200) -> Response:
    return Response(wire.respond(found), status_code=status, media_type=JSON)


class NotionApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._world = NotionWorld(store)
        self._clock = clock

    # ------------------------------------------------------------------ the gate

    def request_id(self, request: Request) -> str:
        return wire.request_id(f"{self._store.head()}:{request.method}:{request.url.path}")

    def refused(self, request: Request, refusal: wire.Refusal) -> Response:
        return Response(
            refusal.body(self.request_id(request)),
            status_code=refusal.status,
            media_type=JSON,
            headers=refusal.headers,
        )

    def guarded(self, handler: Handler, need: wire.Capability | None) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            try:
                call = self._signed_in(request)
                if "notion-version" not in request.headers:
                    raise wire.Refusal(
                        wire.ErrorCode.MISSING_VERSION, "The Notion-Version header is required and was not sent."
                    )
                version = request.headers["notion-version"]
                if version != wire.API_VERSION:
                    raise wire.invalid(
                        f"Notion-Version {version} is not served by this simulation; it answers {wire.API_VERSION}."
                    )
                if need is not None and need not in call.integration.capabilities:
                    raise wire.restricted(need.value)
                self._faults(request, call)
                return await handler(request, call)
            except wire.Refusal as refusal:
                return self.refused(request, refusal)

        return endpoint

    def _signed_in(self, request: Request) -> Call:
        authorization = request.headers["authorization"] if "authorization" in request.headers else ""
        scheme, _, secret = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secret.strip():
            raise wire.unauthorized()
        token = self._world.token(secret.strip())
        if token is None or token.used or token.type in (wire.TokenKind.CODE, wire.TokenKind.REFRESH):
            raise wire.unauthorized()
        integration = self._world.integration(token.integration)
        if integration is None:
            raise wire.unauthorized()
        return Call(integration=integration, secret=secret.strip())

    def _faults(self, request: Request, call: Call) -> None:
        path = request.url.path
        for n, fault in self._world.armed():
            if fault.integration is not None and fault.integration != call.bot:
                continue
            if fault.kind is wire.FaultKind.CONFLICT:
                if not (request.method in EDITS_BLOCKS and path.startswith("/v1/blocks/")):
                    continue
            else:
                if fault.method is not None and fault.method != request.method:
                    continue
                if fault.path is not None and not path.startswith(fault.path):
                    continue
            fired = self._world.fired(n)
            if fired >= fault.times:
                continue
            self._world.count_fault(n, fired + 1)
            answering.injected()
            raise wire.rate_limited(fault.retry_after) if fault.kind is wire.FaultKind.RATE_LIMITED else wire.conflict()

    def _editor(self, call: Call) -> Editor:
        return Editor(self._world, call.workspace, self._clock, actor=Actor.AGENT)

    @staticmethod
    async def _body(request: Request) -> wire.Json:
        return wire.read_object(await request.body())

    @staticmethod
    def _query(request: Request) -> dict[str, list[str]]:
        return parse_qs(request.url.query)

    @staticmethod
    def _one(found: dict[str, list[str]], name: str) -> str | None:
        return found[name][0] if found.get(name) else None

    @staticmethod
    def _id(request: Request, name: str) -> str:
        return wire.canonical_id(request.path_params[name], f"path.{name}")

    # ------------------------------------------------------------------ sharing

    def reaches(self, call: Call, object_id: str) -> bool:
        chain = self._world.ancestry(object_id, call.workspace)
        return chain is not None and any(c in call.integration.shared for c in chain)

    def _buried(self, object_id: str, workspace: str) -> bool:
        """Archived itself, or under an archived page or database."""
        for above in self._world.ancestry(object_id, workspace) or []:
            page = self._world.page(above)
            if page is not None and page.archived:
                return True
            database = self._world.database(above)
            if database is not None and database.archived:
                return True
        return False

    def _page(self, call: Call, page_id: str) -> wire.StoredPage:
        page = self._world.page(page_id)
        if page is None or not self.reaches(call, page_id):
            raise wire.not_found(wire.Missing.PAGE, page_id)
        return page

    def _database(self, call: Call, database_id: str) -> wire.StoredDatabase:
        database = self._world.database(database_id)
        if database is None or not self.reaches(call, database_id):
            raise wire.not_found(wire.Missing.DATABASE, database_id)
        return database

    def _holding(self, call: Call, block_id: str) -> wire.StoredPage:
        holder = self._world.holding(call.workspace, block_id)
        if holder is None or not self.reaches(call, holder.id):
            raise wire.not_found(wire.Missing.BLOCK, block_id)
        return holder

    # ------------------------------------------------------------------ rendering

    def _user(self, call: Call, user: wire.StoredUser) -> wire.Json:
        email = wire.Capability.READ_USERS_WITH_EMAIL in call.integration.capabilities
        if user.type is wire.UserType.PERSON or user.integration is None:
            return wire.render_user(user, email=email)
        integration = self._world.integration(user.integration)
        workspace = self._world.workspace(user.workspace)
        owner: JsonValue = {"type": "workspace", "workspace": True}
        if integration is not None and integration.owner is not None:
            person = self._world.user(integration.owner)
            if person is not None:
                owner = {"type": "user", "user": wire.render_user(person, email=email)}
        return wire.render_user(user, email=email, owner=owner, workspace_name=workspace.name if workspace else "")

    def _value(self, call: Call, value: wire.Json) -> wire.Json:
        kind = wire.PropertyType(str(value["type"]))
        if kind is wire.PropertyType.PEOPLE and isinstance(value["people"], list):
            people: list[JsonValue] = []
            for item in value["people"]:
                user = self._world.user(str(item["id"])) if isinstance(item, dict) else None
                if user is not None:
                    people.append(self._user(call, user))
            return {**value, "people": people}
        if kind is wire.PropertyType.RELATION:
            return {**value, "has_more": False}
        return value

    def render_page(self, call: Call, page: wire.StoredPage, only: Sequence[str] | None = None) -> wire.Json:
        properties: wire.Json = {}
        if is_row(page) and page.parent.id is not None:
            database = self._world.database(page.parent.id)
            schema = database.schema_ if database is not None else {}
            for name, prop in schema.items():
                kind = wire.schema_type(prop)
                stored = page.properties[name] if name in page.properties else wire.empty_value(prop)
                if kind in wire.READ_ONLY:
                    stored = self._computed(call, page, prop, kind)
                properties[name] = self._value(call, stored)
        else:
            properties = {name: self._value(call, value) for name, value in page.properties.items()}
        if only is not None:
            properties = {n: v for n, v in properties.items() if isinstance(v, dict) and v["id"] in only}
        return {
            "object": "page",
            "id": page.id,
            **page.stamps.render(),
            "cover": page.cover,
            "icon": page.icon,
            "parent": page.parent.render(),
            "archived": page.archived,
            "in_trash": page.archived,
            "properties": properties,
            "url": wire.page_url(page.id, title_of(page)),
            "public_url": None,
        }

    def _computed(self, call: Call, page: wire.StoredPage, prop: wire.Json, kind: wire.PropertyType) -> wire.Json:
        stamps = page.stamps
        held: JsonValue
        if kind is wire.PropertyType.CREATED_TIME:
            held = stamps.created_time
        elif kind is wire.PropertyType.LAST_EDITED_TIME:
            held = stamps.last_edited_time
        else:
            who = stamps.created_by if kind is wire.PropertyType.CREATED_BY else stamps.last_edited_by
            user = self._world.user(who)
            held = self._user(call, user) if user is not None else {"object": "user", "id": who}
        return {"id": prop["id"], "type": kind.value, kind.value: held}

    def render_database(self, database: wire.StoredDatabase) -> wire.Json:
        return {
            "object": "database",
            "id": database.id,
            "cover": database.cover,
            "icon": database.icon,
            **database.stamps.render(),
            "title": database.title,
            "description": database.description,
            "is_inline": database.is_inline,
            "properties": {name: dict(prop) for name, prop in database.schema_.items()},
            "parent": database.parent.render(),
            "url": f"https://www.notion.so/{database.id.replace('-', '')}",
            "public_url": None,
            "archived": database.archived,
            "in_trash": database.archived,
        }

    def _live_children(self, page: wire.StoredPage, container: str) -> list[wire.StoredBlock]:
        listed = page.children[container] if container in page.children else []
        found: list[wire.StoredBlock] = []
        for block_id in listed:
            block = page.blocks[block_id]
            if block.archived or self._stub_archived(block):
                continue
            found.append(block)
        return found

    def _stub_archived(self, block: wire.StoredBlock) -> bool:
        if block.type is wire.BlockType.CHILD_PAGE:
            page = self._world.page(block.id)
            return page is None or page.archived
        if block.type is wire.BlockType.CHILD_DATABASE:
            database = self._world.database(block.id)
            return database is None or database.archived
        return False

    def render_block(self, page: wire.StoredPage, block: wire.StoredBlock) -> wire.Json:
        content: JsonValue = block.content
        archived = block.archived
        has_children = bool(self._live_children(page, block.id))
        stamps = block.stamps
        if block.type is wire.BlockType.CHILD_PAGE:
            child = self._world.page(block.id)
            content = {"title": title_of(child) if child else ""}
            archived = archived or child is None or child.archived
            has_children = child is not None and bool(self._live_children(child, child.id))
            stamps = child.stamps if child else stamps
        elif block.type is wire.BlockType.CHILD_DATABASE:
            database = self._world.database(block.id)
            content = {"title": wire.plain(database.title) if database else ""}
            archived = archived or database is None or database.archived
            has_children = False
        return {
            "object": "block",
            "id": block.id,
            "parent": block.parent.render(),
            **stamps.render(),
            "has_children": has_children,
            "archived": archived,
            "in_trash": archived,
            "type": block.type.value,
            block.type.value: content,
        }

    def _page_as_block(self, page: wire.StoredPage) -> wire.Json:
        return {
            "object": "block",
            "id": page.id,
            "parent": page.parent.render(),
            **page.stamps.render(),
            "has_children": bool(self._live_children(page, page.id)),
            "archived": page.archived,
            "in_trash": page.archived,
            "type": "child_page",
            "child_page": {"title": title_of(page)},
        }

    def _database_as_block(self, database: wire.StoredDatabase) -> wire.Json:
        return {
            "object": "block",
            "id": database.id,
            "parent": database.parent.render(),
            **database.stamps.render(),
            "has_children": False,
            "archived": database.archived,
            "in_trash": database.archived,
            "type": "child_database",
            "child_database": {"title": wire.plain(database.title)},
        }

    def _listed(
        self, request: Request, items: Sequence[JsonValue], ids: Sequence[str], cursor: str | None, size: int, kind: str
    ) -> Response:
        return _answer(self._list_body(request, items, ids, cursor, size, kind))

    def _list_body(
        self, request: Request, items: Sequence[JsonValue], ids: Sequence[str], cursor: str | None, size: int, kind: str
    ) -> wire.Json:
        start = 0
        if cursor is not None:
            cursor_id = wire.canonical_id(cursor, "start_cursor")
            if cursor_id not in ids:
                raise wire.invalid("start_cursor does not name a position in this list.")
            start = list(ids).index(cursor_id)
        page = list(items[start : start + size])
        following = ids[start + size] if start + size < len(ids) else None
        return wire.render_list(page, following, kind, self.request_id(request))

    # ------------------------------------------------------------------ search

    async def search(self, request: Request, call: Call) -> Response:
        body = await self._body(request)
        wire.only_keys(body, ["query", "filter", "sort", "start_cursor", "page_size"], "body")
        words = wire.as_text(body["query"], "body.query").casefold() if body.get("query") else ""
        wanted: str | None = None
        if "filter" in body and body["filter"] is not None:
            found = wire.as_object(body["filter"], "body.filter")
            wire.only_keys(found, ["property", "value"], "body.filter")
            if "property" not in found or found["property"] != "object":
                raise wire.invalid("body.filter.property should be `object`.")
            wanted = wire.as_text(found["value"], "body.filter.value") if "value" in found else ""
            if wanted not in ("page", "database"):
                raise wire.invalid(f"body.filter.value should be `page` or `database`; got `{wanted}`.")
        descending = True
        if "sort" in body and body["sort"] is not None:
            sort = wire.as_object(body["sort"], "body.sort")
            wire.only_keys(sort, ["direction", "timestamp"], "body.sort")
            if "timestamp" not in sort or sort["timestamp"] != "last_edited_time":
                raise wire.invalid("body.sort.timestamp should be `last_edited_time`.")
            direction = wire.as_text(sort["direction"], "body.sort.direction") if "direction" in sort else ""
            if direction not in ("ascending", "descending"):
                raise wire.invalid("body.sort.direction should be `ascending` or `descending`.")
            descending = direction == "descending"
        size = wire.page_size(body["page_size"] if "page_size" in body else None, "body.page_size")
        cursor = wire.as_text(body["start_cursor"], "body.start_cursor") if "start_cursor" in body else None
        found_items: list[tuple[str, str, wire.Json]] = []
        if wanted != "database":
            for page in self._world.pages(call.workspace):
                if not self.reaches(call, page.id) or self._buried(page.id, call.workspace):
                    continue
                if words and words not in title_of(page).casefold():
                    continue
                found_items.append((page.stamps.last_edited_time, page.id, self.render_page(call, page)))
        if wanted != "page":
            for database in self._world.databases(call.workspace):
                if not self.reaches(call, database.id) or self._buried(database.id, call.workspace):
                    continue
                if words and words not in wire.plain(database.title).casefold():
                    continue
                found_items.append((database.stamps.last_edited_time, database.id, self.render_database(database)))
        found_items.sort(key=lambda f: f[1])
        found_items.sort(key=lambda f: f[0], reverse=descending)
        workspace = self._world.workspace(call.workspace)
        if workspace is not None:
            self._world.saw(record_ref(workspace.id), Operation.SEARCH)
        return self._listed(
            request, [f[2] for f in found_items], [f[1] for f in found_items], cursor, size, "page_or_database"
        )

    # ------------------------------------------------------------------ pages

    async def page_get(self, request: Request, call: Call) -> Response:
        page = self._page(call, self._id(request, "page_id"))
        only = self._query(request)["filter_properties"] if "filter_properties" in self._query(request) else None
        self._world.saw(page_ref(page), Operation.READ)
        return _answer(self.render_page(call, page, only))

    async def page_create(self, request: Request, call: Call) -> Response:
        body = await self._body(request)
        wire.only_keys(body, ["parent", "properties", "children", "icon", "cover"], "body")
        if "parent" not in body:
            raise wire.invalid("body.parent should be defined.")
        parent = self._parent(call, wire.as_object(body["parent"], "body.parent"))
        properties = wire.as_object(body["properties"], "body.properties") if "properties" in body else {}
        children = wire.as_list(body["children"], "body.children") if "children" in body else []
        editor = self._editor(call)
        page = editor.create_page(
            editor.mint("page", within=parent.id or call.workspace),
            parent,
            properties,
            children,
            by=call.bot,
            icon=body["icon"] if "icon" in body else None,
            cover=body["cover"] if "cover" in body else None,
        )
        if parent.type is wire.ParentType.WORKSPACE:
            self._world.write_integration(
                call.integration.model_copy(update={"shared": [*call.integration.shared, page.id]}),
                operation=Operation.UPDATE,
            )
        return _answer(self.render_page(call, page))

    def _parent(self, call: Call, given: wire.Json) -> wire.Parent:
        keys = [k for k in given if k != "type"]
        if len(keys) != 1:
            raise wire.invalid("body.parent should name exactly one of page_id, database_id or workspace.")
        key = keys[0]
        try:
            kind = wire.ParentType(key)
        except ValueError as error:
            raise wire.invalid(f"body.parent.{key} is not a parent a page can be made under.") from error
        if kind is wire.ParentType.WORKSPACE:
            if given["workspace"] is not True or call.integration.type is not wire.IntegrationKind.PUBLIC:
                raise wire.invalid("body.parent: only a public integration may make a page at the workspace's top.")
            return wire.Parent(type=wire.ParentType.WORKSPACE)
        if kind is wire.ParentType.PAGE_ID:
            page = self._page(
                call, wire.canonical_id(wire.as_text(given[key], "body.parent.page_id"), "body.parent.page_id")
            )
            return wire.Parent(type=wire.ParentType.PAGE_ID, id=page.id)
        if kind is wire.ParentType.DATABASE_ID:
            database = self._database(
                call, wire.canonical_id(wire.as_text(given[key], "body.parent.database_id"), "body.parent.database_id")
            )
            return wire.Parent(type=wire.ParentType.DATABASE_ID, id=database.id)
        raise wire.invalid(f"body.parent.{key} is not a parent a page can be made under.")

    async def page_update(self, request: Request, call: Call) -> Response:
        page = self._page(call, self._id(request, "page_id"))
        changed = self._editor(call).update_page(page.id, await self._body(request), by=call.bot)
        return _answer(self.render_page(call, changed))

    async def page_property(self, request: Request, call: Call) -> Response:
        page = self._page(call, self._id(request, "page_id"))
        wanted = unquote(request.path_params["property_id"])
        rendered = self.render_page(call, page)["properties"]
        assert isinstance(rendered, dict)
        value = next((v for v in rendered.values() if isinstance(v, dict) and v["id"] == wanted), None)
        if value is None:
            raise wire.not_found(wire.Missing.PAGE, f"{page.id} (property {wanted})")
        kind = wire.PropertyType(str(value["type"]))
        self._world.saw(page_ref(page), Operation.READ)
        if kind in (
            wire.PropertyType.TITLE,
            wire.PropertyType.RICH_TEXT,
            wire.PropertyType.PEOPLE,
            wire.PropertyType.RELATION,
        ):
            held = value[kind.value]
            items: list[JsonValue] = []
            for item in held if isinstance(held, list) else []:
                items.append({"object": "property_item", "id": wanted, "type": kind.value, kind.value: item})
            query = self._query(request)
            size = wire.page_size(self._one(query, "page_size"))
            ids = [wire.minted_id(page.id, wanted, str(i)) for i in range(len(items))]
            found = self._list_body(request, items, ids, self._one(query, "start_cursor"), size, "property_item")
            found["property_item"] = {"id": wanted, "next_url": None, "type": kind.value, kind.value: {}}
            return _answer(found)
        return _answer({"object": "property_item", "id": wanted, "type": kind.value, kind.value: value[kind.value]})

    # ------------------------------------------------------------------ blocks

    async def block_get(self, request: Request, call: Call) -> Response:
        block_id = self._id(request, "block_id")
        page = self._world.page(block_id)
        if page is not None and self.reaches(call, block_id):
            self._world.saw(page_ref(page), Operation.READ)
            return _answer(self._page_as_block(page))
        database = self._world.database(block_id)
        if database is not None and self.reaches(call, block_id):
            return _answer(self._database_as_block(database))
        holder = self._holding(call, block_id)
        self._world.saw(page_ref(holder), Operation.READ)
        return _answer(self.render_block(holder, holder.blocks[block_id]))

    async def block_update(self, request: Request, call: Call) -> Response:
        block_id = self._id(request, "block_id")
        body = await self._body(request)
        if self._world.page(block_id) is not None:
            self._page(call, block_id)
            wire.only_keys(body, ["archived", "in_trash"], "body")
            changed = self._editor(call).update_page(block_id, body, by=call.bot)
            return _answer(self._page_as_block(changed))
        if self._world.database(block_id) is not None:
            self._database(call, block_id)
            wire.only_keys(body, ["archived", "in_trash"], "body")
            return _answer(self._database_as_block(self._editor(call).update_database(block_id, body, by=call.bot)))
        self._holding(call, block_id)
        page, block = self._editor(call).update_block(block_id, body, by=call.bot)
        return _answer(self.render_block(page, block))

    async def block_delete(self, request: Request, call: Call) -> Response:
        block_id = self._id(request, "block_id")
        if self._world.page(block_id) is not None:
            self._page(call, block_id)
            return _answer(self._page_as_block(self._editor(call).archive(block_id, by=call.bot)))
        if self._world.database(block_id) is not None:
            self._database(call, block_id)
            changed = self._editor(call).update_database(block_id, {"archived": True}, by=call.bot)
            return _answer(self._database_as_block(changed))
        self._holding(call, block_id)
        page, block = self._editor(call).archive_block(block_id, by=call.bot)
        latest = self._world.page(page.id) or page
        return _answer(self.render_block(latest, latest.blocks[block.id]))

    async def children_list(self, request: Request, call: Call) -> Response:
        block_id = self._id(request, "block_id")
        query = self._query(request)
        size = wire.page_size(self._one(query, "page_size"))
        cursor = self._one(query, "start_cursor")
        page = self._world.page(block_id)
        if page is not None and self.reaches(call, block_id):
            holder, container = page, page.id
        elif self._world.database(block_id) is not None and self.reaches(call, block_id):
            raise wire.invalid("A child_database block has no block children; query the database for its rows.")
        else:
            holder, container = self._holding(call, block_id), block_id
        live = self._live_children(holder, container)
        self._world.saw(page_ref(holder), Operation.READ)
        return self._listed(
            request, [self.render_block(holder, b) for b in live], [b.id for b in live], cursor, size, "block"
        )

    async def children_append(self, request: Request, call: Call) -> Response:
        block_id = self._id(request, "block_id")
        body = await self._body(request)
        wire.only_keys(body, ["children", "after"], "body")
        if "children" not in body:
            raise wire.invalid("body.children should be defined.")
        if self._world.page(block_id) is not None:
            self._page(call, block_id)
        else:
            self._holding(call, block_id)
        after = (
            wire.canonical_id(wire.as_text(body["after"], "body.after"), "body.after")
            if "after" in body and body["after"] is not None
            else None
        )
        page, top = self._editor(call).append(block_id, body["children"], after=after, by=call.bot)
        rendered = [self.render_block(page, b) for b in top]
        return _answer(wire.render_list(rendered, None, "block", self.request_id(request)))

    # ------------------------------------------------------------------ databases

    async def database_get(self, request: Request, call: Call) -> Response:
        database = self._database(call, self._id(request, "database_id"))
        self._world.saw(record_ref(database.id), Operation.READ)
        return _answer(self.render_database(database))

    async def database_query(self, request: Request, call: Call) -> Response:
        database = self._database(call, self._id(request, "database_id"))
        body = await self._body(request)
        wire.only_keys(body, ["filter", "sorts", "start_cursor", "page_size", "archived", "in_trash"], "body")
        schema = database.schema_
        found = (
            notion_query.parse_filter(body["filter"], schema)
            if "filter" in body and body["filter"] is not None
            else None
        )
        sorts = notion_query.parse_sorts(body["sorts"], schema) if "sorts" in body and body["sorts"] is not None else []
        size = wire.page_size(body["page_size"] if "page_size" in body else None, "body.page_size")
        cursor = wire.as_text(body["start_cursor"], "body.start_cursor") if "start_cursor" in body else None
        now = self._clock.now()
        rows: list[tuple[notion_query.Row, wire.StoredPage]] = []
        for page in self._world.rows(database.id):
            if page.archived:
                continue
            row = notion_query.Row(
                values={
                    n: page.properties[n] if n in page.properties else wire.empty_value(s) for n, s in schema.items()
                },
                created=wire.moment(page.stamps.created_time),
                edited=wire.moment(page.stamps.last_edited_time),
                created_by=page.stamps.created_by,
                edited_by=page.stamps.last_edited_by,
            )
            if found is None or notion_query.matches(found, row, now):
                rows.append((row, page))
        by_row = {id(r): p for r, p in rows}
        ordered_rows = [r for r, _ in sorted(rows, key=lambda rp: (rp[0].created, rp[1].id))]
        ordered_rows = notion_query.sorted_rows(ordered_rows, sorts, schema, self._name)
        pages = [by_row[id(r)] for r in ordered_rows]
        only = self._query(request)["filter_properties"] if "filter_properties" in self._query(request) else None
        self._world.saw(record_ref(database.id), Operation.SEARCH)
        return self._listed(
            request, [self.render_page(call, p, only) for p in pages], [p.id for p in pages], cursor, size, "page"
        )

    def _name(self, user_id: str) -> str:
        user = self._world.user(user_id)
        return user.name if user is not None else user_id

    async def database_create(self, request: Request, call: Call) -> Response:
        body = await self._body(request)
        wire.only_keys(body, ["parent", "title", "description", "properties", "icon", "cover", "is_inline"], "body")
        if "parent" not in body or "properties" not in body:
            raise wire.invalid("body.parent and body.properties should be defined.")
        parent = self._parent(call, wire.as_object(body["parent"], "body.parent"))
        editor = self._editor(call)
        database_id = editor.mint("database", within=parent.id or call.workspace)
        schema: dict[str, wire.Json] = {}
        for name, raw in wire.as_object(body["properties"], "body.properties").items():
            made = wire.schema_from_request(
                database_id, name, raw, f"body.properties.{name}", lambda d: self._world.database(d) is not None
            )
            schema[str(made["name"])] = made
        title = wire.rich_text(body["title"], "body.title", editor) if "title" in body else []
        inline = wire.as_bool(body["is_inline"], "body.is_inline") if "is_inline" in body else False
        database = editor.create_database(database_id, parent, title, schema, inline=inline, by=call.bot)
        return _answer(self.render_database(database))

    async def database_update(self, request: Request, call: Call) -> Response:
        database = self._database(call, self._id(request, "database_id"))
        changed = self._editor(call).update_database(database.id, await self._body(request), by=call.bot)
        return _answer(self.render_database(changed))

    # ------------------------------------------------------------------ users

    def _users_allowed(self, call: Call) -> None:
        held = call.integration.capabilities
        if wire.Capability.READ_USERS_WITH_EMAIL not in held and wire.Capability.READ_USERS_WITHOUT_EMAIL not in held:
            raise wire.restricted(wire.Capability.READ_USERS_WITHOUT_EMAIL.value)

    async def users_list(self, request: Request, call: Call) -> Response:
        self._users_allowed(call)
        query = self._query(request)
        users = self._world.users(call.workspace)
        return self._listed(
            request,
            [self._user(call, u) for u in users],
            [u.id for u in users],
            self._one(query, "start_cursor"),
            wire.page_size(self._one(query, "page_size")),
            "user",
        )

    async def users_me(self, request: Request, call: Call) -> Response:
        me = self._world.user(call.bot)
        if me is None:
            raise wire.unauthorized()
        return _answer(self._user(call, me))

    async def user_get(self, request: Request, call: Call) -> Response:
        self._users_allowed(call)
        user_id = self._id(request, "user_id")
        user = self._world.user(user_id)
        if user is None or user.workspace != call.workspace or user.removed:
            raise wire.not_found(wire.Missing.USER, user_id)
        return _answer(self._user(call, user))

    # ------------------------------------------------------------------ comments

    def render_comment(self, comment: wire.StoredComment) -> wire.Json:
        return {
            "object": "comment",
            "id": comment.id,
            "parent": {"type": "page_id", "page_id": comment.page},
            "discussion_id": comment.discussion_id,
            "created_time": comment.created_time,
            "last_edited_time": comment.created_time,
            "created_by": {"object": "user", "id": comment.created_by},
            "rich_text": comment.rich_text,
        }

    async def comments_list(self, request: Request, call: Call) -> Response:
        query = self._query(request)
        raw = self._one(query, "block_id")
        if raw is None:
            raise wire.invalid("block_id should be given in the query.")
        page = self._page(call, wire.canonical_id(raw, "block_id"))
        comments = self._world.comments(page.id)
        comments.sort(key=lambda c: c.created_time)
        self._world.saw(page_ref(page), Operation.READ)
        return self._listed(
            request,
            [self.render_comment(c) for c in comments],
            [c.id for c in comments],
            self._one(query, "start_cursor"),
            wire.page_size(self._one(query, "page_size")),
            "comment",
        )

    async def comments_create(self, request: Request, call: Call) -> Response:
        body = await self._body(request)
        wire.only_keys(body, ["parent", "discussion_id", "rich_text"], "body")
        if ("parent" in body) == ("discussion_id" in body):
            raise wire.invalid("body should give either parent or discussion_id.")
        editor = self._editor(call)
        if "rich_text" not in body:
            raise wire.invalid("body.rich_text should be defined.")
        text = wire.rich_text(body["rich_text"], "body.rich_text", editor)
        if "parent" in body:
            parent = wire.as_object(body["parent"], "body.parent")
            if "page_id" not in parent:
                raise wire.invalid("body.parent.page_id should be defined.")
            page = self._page(
                call, wire.canonical_id(wire.as_text(parent["page_id"], "page_id"), "body.parent.page_id")
            )
            discussion = None
        else:
            discussion = wire.canonical_id(wire.as_text(body["discussion_id"], "discussion_id"), "body.discussion_id")
            page = self._discussion_page(call, discussion)
        return _answer(self.render_comment(editor.comment(page.id, discussion, text, by=call.bot)))

    def _discussion_page(self, call: Call, discussion: str) -> wire.StoredPage:
        for page in self._world.pages(call.workspace):
            if any(c.discussion_id == discussion for c in self._world.comments(page.id)):
                return self._page(call, page.id)
        raise wire.not_found(wire.Missing.COMMENT, discussion)

    # ------------------------------------------------------------------ OAuth

    async def token(self, request: Request) -> Response:
        """The token endpoint of a public integration: an authorization code, or a refresh token, for tokens."""
        try:
            integration = self._client(request)
            if integration is None:
                return Response(
                    wire.oauth_error("invalid_client", "The client id and secret do not name a public integration."),
                    status_code=401,
                    media_type=JSON,
                )
            asked = wire.read_token_request(await self._body(request))
        except wire.Refusal as refusal:
            return self.refused(request, refusal)
        if asked.grant_type == wire.GrantType.AUTHORIZATION_CODE:
            presented, kind = asked.code, wire.TokenKind.CODE
        elif asked.grant_type == wire.GrantType.REFRESH_TOKEN:
            presented, kind = asked.refresh_token, wire.TokenKind.REFRESH
        else:
            return Response(
                wire.oauth_error("unsupported_grant_type", f"grant_type {asked.grant_type} is not taken here."),
                status_code=400,
                media_type=JSON,
            )
        held = self._world.token(presented) if presented else None
        if (
            presented is None
            or held is None
            or held.type is not kind
            or held.used
            or held.integration != integration.id
        ):
            return Response(
                wire.oauth_error("invalid_grant", "The code or refresh token is unknown, used, or another client's."),
                status_code=400,
                media_type=JSON,
            )
        if kind is wire.TokenKind.CODE and held.redirect_uri is not None and asked.redirect_uri != held.redirect_uri:
            return Response(
                wire.oauth_error("invalid_grant", "redirect_uri does not match the one the code was issued for."),
                status_code=400,
                media_type=JSON,
            )
        seq = str(self._world.next_seq())
        access = "ntn_" + wire.digest(f"access\x1f{presented}\x1f{seq}")[:46]
        refresh = "nrt_" + wire.digest(f"refresh\x1f{presented}\x1f{seq}")[:46]
        self._world.write_token(
            presented, held.model_copy(update={"used": True}), operation=Operation.UPDATE, actor=Actor.AGENT
        )
        for secret, made in ((access, wire.TokenKind.ACCESS), (refresh, wire.TokenKind.REFRESH)):
            self._world.write_token(
                secret,
                wire.StoredToken(type=made, integration=integration.id),
                operation=Operation.CREATE,
                actor=Actor.AGENT,
            )
        workspace = self._world.workspace(integration.workspace)
        bot = self._world.user(integration.id)
        owner: JsonValue = {"type": "workspace", "workspace": True}
        if integration.owner is not None:
            person = self._world.user(integration.owner)
            if person is not None:
                owner = {"type": "user", "user": wire.render_user(person, email=True)}
        return _answer(
            {
                "access_token": access,
                "token_type": "bearer",
                "refresh_token": refresh,
                "bot_id": bot.id if bot else integration.id,
                "workspace_id": integration.workspace,
                "workspace_name": workspace.name if workspace else "",
                "workspace_icon": None,
                "owner": owner,
                "duplicated_template_id": None,
                "request_id": self.request_id(request),
            }
        )

    def _client(self, request: Request) -> wire.StoredIntegration | None:
        authorization = request.headers["authorization"] if "authorization" in request.headers else ""
        scheme, _, encoded = authorization.partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            client_id, _, secret = base64.b64decode(encoded.strip()).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        for workspace in self._world.workspaces():
            for integration in self._world.integrations(workspace.id):
                if integration.client_id == client_id and integration.client_secret_digest == wire.digest(secret):
                    return integration
        return None


class NotionApp:
    """Routes, and Notion's answer to a path or method it does not serve. Once a call that may have changed
    something is answered, what webhook subscriptions are owed is sent (`webhooks.deliver`) in a task of its own,
    so the caller's answer never waits on its own webhook endpoint."""

    def __init__(self, api: NotionApi, routes: list[Route], world: NotionWorld, clock: Clock) -> None:
        self._api = api
        self._router = Router(routes=routes)
        self._routes = routes
        self._world = world
        self._clock = clock
        self._sending: set[asyncio.Task[None]] = set()
        self._one_at_a_time = asyncio.Lock()

    async def _deliver(self) -> None:
        async with self._one_at_a_time:
            await webhooks.deliver(self._world, self._clock)

    def delivering(self) -> int:
        """`DeliversInBackground`: webhook deliveries started and not yet answered."""
        return len(self._sending)

    async def settled(self) -> None:
        """Wait for every delivery already started."""
        while self._sending:
            await asyncio.gather(*list(self._sending))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            matched = [r.matches(scope)[0] for r in self._routes]
            if Match.FULL not in matched:
                request = Request(scope, receive)
                refusal = (
                    wire.Refusal(wire.ErrorCode.INVALID_REQUEST, f"{request.method} is not taken at this path.")
                    if Match.PARTIAL in matched
                    else wire.Refusal(wire.ErrorCode.INVALID_REQUEST_URL, "There is no endpoint at this path.")
                )
                await self._api.refused(request, refusal)(scope, receive, send)
                return
        await self._router(scope, receive, send)
        if scope["type"] == "http" and scope["method"] != "GET" and webhooks.watching(self._world):
            task = asyncio.create_task(self._deliver())
            self._sending.add(task)
            task.add_done_callback(self._sending.discard)


def build_app(store: Store, clock: Clock) -> NotionApp:
    api = NotionApi(store, clock)
    read, update, insert = wire.Capability.READ_CONTENT, wire.Capability.UPDATE_CONTENT, wire.Capability.INSERT_CONTENT

    def route(path: str, method: str, handler: Handler, need: wire.Capability | None) -> Route:
        return Route(path, api.guarded(handler, need), methods=[method])

    routes = [
        route("/v1/search", "POST", api.search, read),
        route("/v1/pages", "POST", api.page_create, insert),
        route("/v1/pages/{page_id}", "GET", api.page_get, read),
        route("/v1/pages/{page_id}", "PATCH", api.page_update, update),
        route("/v1/pages/{page_id}/properties/{property_id}", "GET", api.page_property, read),
        route("/v1/blocks/{block_id}", "GET", api.block_get, read),
        route("/v1/blocks/{block_id}", "PATCH", api.block_update, update),
        route("/v1/blocks/{block_id}", "DELETE", api.block_delete, update),
        route("/v1/blocks/{block_id}/children", "GET", api.children_list, read),
        route("/v1/blocks/{block_id}/children", "PATCH", api.children_append, insert),
        route("/v1/databases", "POST", api.database_create, insert),
        route("/v1/databases/{database_id}", "GET", api.database_get, read),
        route("/v1/databases/{database_id}", "PATCH", api.database_update, update),
        route("/v1/databases/{database_id}/query", "POST", api.database_query, read),
        route("/v1/users", "GET", api.users_list, None),
        route("/v1/users/me", "GET", api.users_me, None),
        route("/v1/users/{user_id}", "GET", api.user_get, None),
        route("/v1/comments", "GET", api.comments_list, wire.Capability.READ_COMMENTS),
        route("/v1/comments", "POST", api.comments_create, wire.Capability.INSERT_COMMENTS),
        Route("/v1/oauth/token", api.token, methods=["POST"]),
    ]
    return NotionApp(api, routes, NotionWorld(store), clock)
