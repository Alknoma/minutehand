"""Notion, driven through its public API (https://developers.notion.com/reference/intro) as an integration's client
calls it: every endpoint under `https://api.notion.com/v1/`, the integration's token as a bearer in `Authorization`,
`Notion-Version: 2022-06-28` on every call, bodies as JSON, and every answer read as Notion documents it: the object,
or an error object `{"object": "error", "status", "code", "message"}` that `ok` raises.

A world is one workspace with one integration, its own by `tag`: a PUBLIC integration (client id and secret unique
to the tag, installed by the seed's first person) holding the access token `ntn_<tag>...` an earlier OAuth grant
handed it. Public, because only a public integration may make a page at the workspace's top
(https://developers.notion.com/reference/post-page: "workspace: true" parents are for public integrations), and
`create_document(title, None)` asks for exactly that. Every call acts as the integration's bot: Notion has no
credential that acts as a person (https://developers.notion.com/docs/authorization).

Documents are pages. Notion has no folders; a page sits under a page, so a folder path is the chain of titles of the
pages (and databases) above it, and a page that holds nothing but sub-pages is a folder, left out of the listing.
Trash is `archived: true` (https://developers.notion.com/reference/archive-a-page).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from notion_client import APIResponseError, Client

from minutehand.adapters.control.wire import Claims, CreateWorld, WorldView
from minutehand.adapters.providers.notion.wire import API_VERSION
from minutehand.domain.scenario import AccessRole, DocumentKind, Seed
from tests.conformance.contract import (
    Api,
    CommentSeen,
    Documents,
    DocumentSeen,
    Driver,
    FaultCase,
    IdKind,
    PersonSeen,
    Session,
    ok,
    uncredentialed,
)

PROVIDER = "notion"
API = "https://api.notion.com/v1/"
VERSION = "2022-06-28"
LISTING = 100
"""The page size this driver lists with where it reads everything: Notion's maximum
(https://developers.notion.com/reference/intro#pagination)."""
MAX_CHILDREN = 100
"""Children per append (https://developers.notion.com/reference/request-limits)."""
MAX_TEXT = 2000
"""Characters per rich text `text.content` (https://developers.notion.com/reference/request-limits)."""
UNSEEDED = "ntn_000000000000000000000000000000000000000000000unseeded"
"""A token of Notion's shape that no world's seed names."""
WORKSPACE = "team"
INTEGRATION = "agent"
FAULT_PAGE = "Fault probe"
CONTAINERS = ("child_page", "child_database")
"""Block types that are another page or database, not the page's own content."""
NOTION_TIME = "%Y-%m-%dT%H:%M:%S.%fZ"
"""Notion's ISO 8601 timestamps, e.g. `2020-03-17T19:10:04.968Z` (https://developers.notion.com/reference/page)."""

Doc = Mapping[str, Any]
"""A JSON object as Notion answered it, read only inside this driver."""


def access_token(tag: str) -> str:
    return f"ntn_{tag}conformanceaccesstoken"


def at_of(stamp: str) -> datetime:
    """A Notion timestamp as aware UTC; anything else is refused."""
    return datetime.strptime(stamp, NOTION_TIME).replace(tzinfo=UTC)


def plain(rich_text: object) -> str:
    """The `plain_text` of a rich text array, joined."""
    if not isinstance(rich_text, list):
        raise ValueError(f"not a rich text array: {rich_text!r}")
    return "".join(str(item["plain_text"]) for item in rich_text)


def runs(text: str) -> list[dict[str, object]]:
    """`text` as rich text runs of at most 2000 characters each, as a client must split it."""
    return [{"type": "text", "text": {"content": text[n : n + MAX_TEXT]}} for n in range(0, len(text), MAX_TEXT)]


def title_of(found: Doc) -> str:
    """A page's title (its one `title` property) or a database's title."""
    if found["object"] == "database":
        return plain(found["title"])
    titles = [p for p in found["properties"].values() if p["type"] == "title"]
    if len(titles) != 1:
        raise ValueError(f"page {found['id']} answered {len(titles)} title properties")
    return plain(titles[0]["title"])


def title_property(page: Doc) -> str:
    return next(name for name, p in page["properties"].items() if p["type"] == "title")


def _merged_body(found: object) -> dict[str, Any]:
    if isinstance(found, str):
        loaded = json.loads(found)
        if not isinstance(loaded, dict):
            raise ValueError("the notion provider seed is not a JSON object")
        return loaded
    if isinstance(found, Mapping):
        return dict(found)
    raise ValueError(f"the notion provider seed is neither JSON text nor an object: {found!r}")


class NotionSession(Documents):
    def __init__(self, api: Api, token: str) -> None:
        self._api = api
        self._token = token
        self._http = api.http({"Authorization": f"Bearer {token}", "Notion-Version": VERSION}, base_url=API)
        self.secrets = (token, UNSEEDED)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ the API

    def _get(self, what: str, path: str, **query: str | int) -> Doc:
        return ok(what, self._http.get(path, params={k: str(v) for k, v in query.items()})).json()

    def _send(self, what: str, method: str, path: str, body: Mapping[str, object] | None = None) -> Doc:
        return ok(what, self._http.request(method, path, json=dict(body) if body is not None else None)).json()

    def _get_pages(self, what: str, path: str, page_size: int, **query: str) -> list[list[Doc]]:
        """Every page of a paginated GET (https://developers.notion.com/reference/intro#pagination)."""
        pages: list[list[Doc]] = []
        cursor: str | None = None
        while True:
            more: dict[str, str | int] = {"page_size": page_size, **query}
            if cursor is not None:
                more["start_cursor"] = cursor
            answered = self._get(what, path, **more)
            pages.append(list(answered["results"]))
            if answered["has_more"] is not True:
                return pages
            cursor = str(answered["next_cursor"])

    def _search_pages(self, page_size: int) -> list[list[Doc]]:
        """Every page of `POST /v1/search` filtered to pages (https://developers.notion.com/reference/post-search)."""
        pages: list[list[Doc]] = []
        cursor: str | None = None
        while True:
            body: dict[str, object] = {"filter": {"property": "object", "value": "page"}, "page_size": page_size}
            if cursor is not None:
                body["start_cursor"] = cursor
            answered = self._send("search pages", "POST", "search", body)
            pages.append(list(answered["results"]))
            if answered["has_more"] is not True:
                return pages
            cursor = str(answered["next_cursor"])

    def _children(self, block: str) -> list[Doc]:
        return [b for page in self._get_pages("list block children", f"blocks/{block}/children", LISTING) for b in page]

    def _all_pages(self) -> list[Doc]:
        return [p for page in self._search_pages(LISTING) for p in page]

    # ------------------------------------------------------------------ accounts

    def _me(self) -> Doc:
        return self._get("retrieve the bot user", "users/me")

    def whoami(self) -> str:
        return str(self._me()["name"])

    def _users(self) -> list[Doc]:
        return [u for page in self._get_pages("list users", "users", LISTING) for u in page]

    def _emails(self) -> dict[str, str | None]:
        return {str(u["id"]): _email(u) for u in self._users()}

    def people(self) -> list[PersonSeen]:
        return [
            PersonSeen(id=str(u["id"]), name=str(u["name"]), email=_email(u), bot=u["type"] == "bot")
            for u in self._users()
        ]

    def people_pages(self, page_size: int) -> list[list[str]]:
        return [[str(u["id"]) for u in page] for page in self._get_pages("list users", "users", page_size)]

    def stranger(self, *, credentialed: bool) -> httpx.Response:
        if credentialed:
            return self._http.get("users/me", headers={"Authorization": f"Bearer {UNSEEDED}"})
        return uncredentialed(self._http, "GET", "users/me")

    def observe(self) -> str:
        answered: list[str] = []
        for what, method, path, body, query in (
            ("list users", "GET", "users", None, {"page_size": str(LISTING)}),
            ("search pages", "POST", "search", {"filter": {"property": "object", "value": "page"},
                                                "page_size": LISTING}, None),
        ):  # fmt: skip
            response = self._http.request(method, path, json=body, params=query)
            ok(what, response)
            answered.append(response.text)
        return "\n".join(answered)

    def change(self, label: str) -> None:
        self.create_document(label, None)

    # ------------------------------------------------------------------ documents

    def _lookup(self, listed: Mapping[str, Doc], kind: str, object_id: str) -> Doc:
        if kind == "page_id" and object_id in listed:
            return listed[object_id]
        if kind == "page_id":
            return self._get("retrieve a page", f"pages/{object_id}")
        if kind == "database_id":
            return self._get("retrieve a database", f"databases/{object_id}")
        return self._get("retrieve a block", f"blocks/{object_id}")

    def _folder_of(self, page: Doc, listed: Mapping[str, Doc]) -> str | None:
        """The titles of the pages and databases above it, from the top, '/'-joined; None at the top."""
        path: list[str] = []
        parent = page["parent"]
        while parent["type"] != "workspace":
            kind = str(parent["type"])
            above = self._lookup(listed, kind, str(parent[kind]))
            if kind != "block_id":
                path.append(title_of(above))
            parent = above["parent"]
        return "/".join(reversed(path)) or None

    def _folders(self, listed: Mapping[str, Doc]) -> set[str]:
        """The pages that hold sub-pages and nothing else: what a person uses as a folder."""
        holders = {str(p["parent"]["page_id"]) for p in listed.values() if p["parent"]["type"] == "page_id"}
        found: set[str] = set()
        for holder in holders & set(listed):
            children = self._children(holder)
            if children and all(c["type"] in CONTAINERS for c in children):
                found.add(holder)
        return found

    def _seen(self, page: Doc, listed: Mapping[str, Doc], emails: Mapping[str, str | None]) -> DocumentSeen:
        creator = str(page["created_by"]["id"])
        editor = str(page["last_edited_by"]["id"])
        return DocumentSeen(
            id=str(page["id"]),
            title=title_of(page),
            kind=DocumentKind.DOCUMENT,
            folder=self._folder_of(page, listed),
            owner_email=emails[creator] if creator in emails else None,
            last_editor_email=emails[editor] if editor in emails else None,
            modified=at_of(str(page["last_edited_time"])),
            created=at_of(str(page["created_time"])),
            trashed=page["archived"] is True,
        )

    def documents(self) -> list[DocumentSeen]:
        listed = {str(p["id"]): p for p in self._all_pages()}
        folders = self._folders(listed)
        emails = self._emails()
        return [
            self._seen(p, listed, emails) for i, p in listed.items() if i not in folders and p["archived"] is not True
        ]

    def _title_body(self, title: str) -> dict[str, object]:
        return {"title": {"title": runs(title)}}

    def _create_page(self, title: str, parent: str | None, children: list[dict[str, object]] | None = None) -> str:
        where: dict[str, object] = {"workspace": True} if parent is None else {"page_id": parent}
        body: dict[str, object] = {"parent": where, "properties": self._title_body(title)}
        if children:
            body["children"] = children
        return str(self._send("create a page", "POST", "pages", body)["id"])

    def _folder(self, path: str) -> str:
        """The page at that '/'-path of titles from the top, each made when not there."""
        listed = self._all_pages()
        above: str | None = None
        for name in [n for n in path.split("/") if n]:
            found = [
                str(p["id"])
                for p in listed
                if title_of(p) == name
                and (
                    p["parent"]["type"] == "workspace"
                    if above is None
                    else p["parent"]["type"] == "page_id" and p["parent"]["page_id"] == above
                )
            ]
            if len(found) > 1:
                raise LookupError(f"search answered {len(found)} pages titled {name!r} in the same place")
            above = found[0] if found else self._create_page(name, above)
        if above is None:
            raise ValueError(f"{path!r} names no folder")
        return above

    def create_document(self, title: str, folder: str | None) -> str:
        return self._create_page(title, self._folder(folder) if folder else None)

    def write(self, document: str, text: str) -> None:
        for block in self._children(document):
            if block["type"] not in CONTAINERS:
                self._send("delete a block", "DELETE", f"blocks/{block['id']}")
        paragraphs: list[dict[str, object]] = [
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": runs(line)}}
            for line in text.split("\n")
        ]
        for n in range(0, len(paragraphs), MAX_CHILDREN):
            self._send(
                "append block children",
                "PATCH",
                f"blocks/{document}/children",
                {"children": paragraphs[n : n + MAX_CHILDREN]},
            )

    def read_text(self, document: str) -> str:
        lines: list[str] = []
        for block in self._children(document):
            body = block[block["type"]]
            if block["type"] not in CONTAINERS and "rich_text" in body:
                lines.append(plain(body["rich_text"]))
        return "\n".join(lines)

    def rename(self, document: str, title: str) -> None:
        page = self._get("retrieve a page", f"pages/{document}")
        name = title_property(page)
        self._send(
            "update page properties", "PATCH", f"pages/{document}", {"properties": {name: {"title": runs(title)}}}
        )

    def move(self, document: str, folder: str) -> None:
        """`POST /v1/pages/{id}/move` (https://developers.notion.com/reference/move-page)."""
        target = self._folder(folder)
        self._send("move a page", "POST", f"pages/{document}/move", {"parent": {"type": "page_id", "page_id": target}})

    def share(self, document: str, email: str, role: AccessRole) -> None:
        raise NotImplementedError(
            "Notion's public API has no endpoint that shares a page with a person or reads who it is shared with: "
            "sharing is done in the Notion app"
        )

    def trash(self, document: str) -> None:
        self._send("archive a page", "PATCH", f"pages/{document}", {"archived": True})

    def comments(self, document: str) -> list[CommentSeen]:
        emails = self._emails()
        found = self._get_pages("retrieve comments", "comments", LISTING, block_id=document)
        return [
            CommentSeen(
                author_email=emails[str(c["created_by"]["id"])] if str(c["created_by"]["id"]) in emails else None,
                text=plain(c["rich_text"]),
            )
            for page in found
            for c in page
        ]

    def document_pages(self, page_size: int) -> list[list[str]]:
        pages = self._search_pages(page_size)
        listed = {str(p["id"]): p for page in pages for p in page}
        folders = self._folders(listed)
        return [[str(p["id"]) for p in page if str(p["id"]) not in folders] for page in pages]

    # ------------------------------------------------------------------ files

    def upload(self, name: str, content: bytes, mime_type: str) -> str:
        """The File Upload API (https://developers.notion.com/reference/create-a-file-upload): create the upload,
        send its one part, then attach it as a `file` block of a page titled `name` at the top."""
        made = self._send("create a file upload", "POST", "file_uploads", {"filename": name, "content_type": mime_type})
        upload = str(made["id"])
        ok(
            "send a file upload",
            self._http.post(f"file_uploads/{upload}/send", files={"file": (name, content, mime_type)}),
        )
        block = {"object": "block", "type": "file", "file": {"type": "file_upload", "file_upload": {"id": upload}}}
        return self._create_page(name, None, [block])

    def download(self, document: str) -> bytes:
        files = [b for b in self._children(document) if b["type"] == "file"]
        if len(files) != 1:
            raise LookupError(f"page {document} holds {len(files)} file blocks")
        held = files[0]["file"]
        url = str(held[held["type"]]["url"])
        with self._api.http() as plain_http:
            return ok("download a file", plain_http.get(url)).content


def _email(user: Doc) -> str | None:
    if user["type"] != "person" or "email" not in user["person"]:
        return None
    return str(user["person"]["email"])


def _token(world: WorldView) -> str:
    return world.claims.tokens[0]


def _client(api: Api, world: WorldView) -> Client:
    """The official `notion-client` over a client through the proxy, trusting its CA."""
    return Client(notion_version=API_VERSION, retry=False, auth=_token(world), client=api.http())


def _raised(call: Callable[[Client], object]) -> Callable[[Api, WorldView], BaseException | None]:
    def trigger(api: Api, world: WorldView) -> BaseException | None:
        client = _client(api, world)
        try:
            call(client)
        except APIResponseError as raised:
            return raised
        finally:
            client.close()
        return None

    return trigger


def _succeeds(call: Callable[[Client], object]) -> Callable[[Api, WorldView], None]:
    def then(api: Api, world: WorldView) -> None:
        client = _client(api, world)
        try:
            call(client)
        finally:
            client.close()

    return then


def _users_me(client: Client) -> object:
    return client.users.me()


def _append_to_probe(client: Client) -> object:
    """A block edit: a paragraph appended to the page `Fault probe`, made at the top first when not there."""
    found = client.search(query=FAULT_PAGE, filter={"property": "object", "value": "page"})
    assert isinstance(found, dict)
    pages = [p for p in found["results"] if title_of(p) == FAULT_PAGE]
    if pages:
        page = str(pages[0]["id"])
    else:
        made = client.pages.create(parent={"workspace": True}, properties={"title": {"title": runs(FAULT_PAGE)}})
        assert isinstance(made, dict)
        page = str(made["id"])
    paragraph = {"object": "block", "type": "paragraph", "paragraph": {"rich_text": runs("probe")}}
    return client.blocks.children.append(page, children=[paragraph])


UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class NotionDriver(Driver):
    provider: ClassVar[str] = PROVIDER
    session: ClassVar[type[Session]] = NotionSession
    # Notion answers created_time and last_edited_time rounded down to the minute (provider README, Timestamps).
    time_resolution: ClassVar[timedelta] = timedelta(minutes=1)
    absent: ClassVar[Mapping[str, str]] = {
        "accounts.person_credentials": "Notion issues credentials only to integrations: every API call acts as the "
        "integration's bot user, never as a person (https://developers.notion.com/docs/authorization)",
        "accounts.title": "Notion's user object has an id, a name, an avatar and a person's email, and no job title "
        "(https://developers.notion.com/reference/user)",
        "accounts.guest": "Notion's user object does not say whether a person is a guest, and its user listing leaves "
        "guests out (https://developers.notion.com/reference/get-users)",
        "accounts.deactivated": "Notion's user object has no active or deactivated state; a member who leaves is no "
        "longer listed (https://developers.notion.com/reference/user)",
        "accounts.no_email": "every Notion person signs in with an email address; only a bot user has none, and a bot "
        "is not a person (https://developers.notion.com/reference/user)",
        "accounts.vendor_login": "a Notion user has no login or username of its own, only a name and an email "
        "(https://developers.notion.com/reference/user)",
        "documents.spreadsheet": "Notion has no spreadsheet document: tabular data is a database, which is not a page",
        "documents.presentation": "Notion has no presentation document; every document is a page of blocks",
        "documents.file": "an uploaded file in Notion is a file block inside a page, never a document of its own",
        "documents.sharing": "Notion's public API has no endpoint that shares a page with a person or reads who it is "
        "shared with; sharing is done in the Notion app (https://developers.notion.com/reference/intro)",
        "documents.spaces": "Notion's page object does not say which teamspace a page sits in, so the API cannot place "
        "a page in a shared space or read one back (https://developers.notion.com/reference/page)",
    }
    id_formats: ClassVar[Mapping[IdKind, re.Pattern[str]]] = {
        # Notion user ids are UUIDs, answered dashed (https://developers.notion.com/reference/user: "id: Unique
        # identifier for this user", UUID; the provider's README: "Ids: UUIDs, accepted with or without dashes").
        IdKind.PERSON: UUID,
        # Page ids are UUIDv4, answered dashed (https://developers.notion.com/reference/page: "id: Unique identifier
        # of the page", UUIDv4; https://developers.notion.com/reference/intro: IDs are UUIDs, dashes optional on input).
        IdKind.DOCUMENT: UUID,
    }
    page_floor: ClassVar[Mapping[str, int]] = {
        # `page_size`: "The number of items from the full list desired in the response. Maximum: 100", at least 1
        # (https://developers.notion.com/reference/intro#pagination).
        "people": 1,
        "documents": 1,
    }
    # 401 `unauthorized`: "The bearer token is not valid." (https://developers.notion.com/reference/status-codes)

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise ValueError("a Notion user has no login of its own (accounts.vendor_login is declared absent)")
        found = dict(seed)
        people = found["people"] if "people" in found else []
        if not isinstance(people, list) or not people or not isinstance(people[0], Mapping):
            raise ValueError("a Notion world's public integration is installed by a person, and the seed has none")
        installer = str(people[0]["key"])
        integration: dict[str, object] = {
            "key": INTEGRATION,
            "name": "Agent",
            "type": "public",
            "client_id": f"client-{tag}",
            "client_secret": f"client-secret-{tag}",
            "installed_by": installer,
            "tokens": [access_token(tag)],
        }
        provider_seeds = found["provider_seeds"] if "provider_seeds" in found else []
        if not isinstance(provider_seeds, list):
            raise ValueError("provider_seeds is not a list")
        mine = [s for s in provider_seeds if isinstance(s, Mapping) and s["provider"] == PROVIDER]
        others = [s for s in provider_seeds if s not in mine]
        if len(mine) > 1:
            raise ValueError("the seed holds two notion provider seeds")
        body = _merged_body(mine[0]["body"]) if mine else {}
        workspaces = body["workspaces"] if "workspaces" in body else []
        if workspaces:
            first = dict(workspaces[0])
            first["integrations"] = [*(first["integrations"] if "integrations" in first else []), integration]
            body["workspaces"] = [first, *workspaces[1:]]
        else:
            body["workspaces"] = [{"key": WORKSPACE, "name": "Team", "integrations": [integration]}]
        found["provider_seeds"] = [*others, {"provider": PROVIDER, "body": json.dumps(body)}]
        return CreateWorld(seed=Seed.model_validate(found), claims=Claims(tokens=[access_token(tag)]))

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        if person is not None:
            raise NotImplementedError(
                "Notion has no credential that acts as a person: every call acts as an integration's bot"
            )
        return NotionSession(api, _token(world))

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        return _token(world) if person is None else None

    def faults(self) -> list[FaultCase]:
        return [
            FaultCase(
                name="rate_limited",
                fragment={
                    "faults": [
                        {"kind": "rate_limited", "times": 2, "retry_after": 1, "method": "GET", "path": "/v1/users/me"}
                    ]
                },
                trigger=_raised(_users_me),
                typed=APIResponseError,
                # 429 `rate_limited` (https://developers.notion.com/reference/request-limits)
                status=429,
                holds="rate_limited",
                uses=2,
                then=_succeeds(_users_me),
            ),
            FaultCase(
                name="conflict",
                fragment={"faults": [{"kind": "conflict", "times": 1}]},
                trigger=_raised(_append_to_probe),
                typed=APIResponseError,
                # 409 `conflict_error` (https://developers.notion.com/reference/status-codes)
                status=409,
                holds="conflict_error",
                uses=1,
                then=_succeeds(_append_to_probe),
            ),
        ]


DRIVER = NotionDriver()
