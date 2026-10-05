"""Every change to a workspace's pages, blocks, databases and comments, whoever makes it.

The API answers the agent through this, the seed writes the scenario's world through it,
and a person's act (`provider.NotionProvider.person_*`) lands through it, so a change reads
the same in the log whoever made it: one call is one version of the page it touched, with
the page's whole text as its snapshot. What Notion refuses raises `wire.Refusal`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import JsonValue

from minutehand.adapters.providers.notion import wire
from minutehand.adapters.providers.notion.state import NotionWorld, is_row
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock


class Editor:
    """Changes made in one workspace by one kind of actor, stamped from `clock`."""

    def __init__(
        self, world: NotionWorld, workspace: str, clock: Clock, *, actor: Actor, seeding: bool = False
    ) -> None:
        """`seeding` names what it makes by what it is (the page it is in, its kind, its place among that page's
        own), never by where the log has reached, so the same seed makes the same ids however much the world
        already holds; everything else mints from the event about to be written."""
        self.world = world
        self.workspace = workspace
        self._clock = clock
        self._actor = actor
        self._seeding = seeding
        self._minted = 0
        self._within: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------ lookups

    def now(self) -> datetime:
        return self._clock.now()

    def user_name(self, user_id: str) -> str | None:
        user = self.world.user(user_id)
        return user.name if user is not None and user.workspace == self.workspace else None

    def title_of(self, object_id: str) -> str | None:
        return self.world.title(object_id)

    def is_member(self, user_id: str) -> bool:
        return self.user_name(user_id) is not None

    def mint(self, what: str, *, within: str) -> str:
        """A new object's id; `within` is the page or database it is made in (or under)."""
        if self._seeding:
            n = self._within[(within, what)] + 1 if (within, what) in self._within else 1
            self._within[(within, what)] = n
            return wire.minted_id("seed", self.workspace, within, what, str(n))
        self._minted += 1
        return self.world.mint(what, str(self._minted))

    def page(self, page_id: str) -> wire.StoredPage:
        found = self.world.page(page_id)
        if found is None or found.workspace != self.workspace:
            raise wire.not_found(wire.Missing.PAGE, page_id)
        return found

    def database(self, database_id: str) -> wire.StoredDatabase:
        found = self.world.database(database_id)
        if found is None or found.workspace != self.workspace:
            raise wire.not_found(wire.Missing.DATABASE, database_id)
        return found

    def _stamps(self, by: str) -> wire.Stamps:
        when = wire.stamp(self.now())
        return wire.Stamps(created_time=when, created_by=by, last_edited_time=when, last_edited_by=by)

    def _write(self, page: wire.StoredPage, operation: Operation) -> None:
        self.world.write_page(page, operation=operation, actor=self._actor, at=self.now())

    # ------------------------------------------------------------------ blocks into a page

    def _place(
        self,
        page: wire.StoredPage,
        container: str,
        made: Sequence[wire.NewBlock],
        by: str,
        *,
        after: str | None = None,
    ) -> tuple[wire.StoredPage, list[wire.StoredBlock]]:
        """Put new blocks into `container` (the page or one of its blocks), after `after` or at the end."""
        blocks = dict(page.blocks)
        children = {k: list(v) for k, v in page.children.items()}
        stamps = self._stamps(by)
        top: list[wire.StoredBlock] = []

        def put(into: str, items: Sequence[wire.NewBlock], position: int | None) -> list[wire.StoredBlock]:
            placed: list[wire.StoredBlock] = []
            parent = (
                wire.Parent(type=wire.ParentType.PAGE_ID, id=page.id)
                if into == page.id
                else wire.Parent(type=wire.ParentType.BLOCK_ID, id=into)
            )
            for item in items:
                block = wire.StoredBlock(
                    id=self.mint("block", within=page.id),
                    type=item.type,
                    content=item.content,
                    parent=parent,
                    stamps=stamps,
                )
                blocks[block.id] = block
                listed = children.setdefault(into, [])
                if position is None:
                    listed.append(block.id)
                else:
                    listed.insert(position, block.id)
                    position += 1
                placed.append(block)
                put(block.id, item.children, None)
            return placed

        position: int | None = None
        if after is not None:
            listed = children[container] if container in children else []
            if after not in listed:
                raise wire.invalid(f"after: {after} is not a child of {container}.")
            position = listed.index(after) + 1
        top = put(container, made, position)
        changed = page.model_copy(
            update={"blocks": blocks, "children": children, "stamps": page.stamps.edited(stamps.last_edited_time, by)}
        )
        return changed, top

    def _stub(self, page: wire.StoredPage, kind: wire.BlockType, object_id: str, by: str) -> wire.StoredPage:
        """The child_page or child_database block a new page or database leaves in its parent page."""
        stamps = self._stamps(by)
        block = wire.StoredBlock(
            id=object_id,
            type=kind,
            content={},
            parent=wire.Parent(type=wire.ParentType.PAGE_ID, id=page.id),
            stamps=stamps,
        )
        children = {k: list(v) for k, v in page.children.items()}
        children.setdefault(page.id, []).append(object_id)
        return page.model_copy(
            update={
                "blocks": {**page.blocks, object_id: block},
                "children": children,
                "stamps": page.stamps.edited(stamps.last_edited_time, by),
            }
        )

    # ------------------------------------------------------------------ pages

    def create_page(
        self,
        page_id: str,
        parent: wire.Parent,
        properties: wire.Json,
        children: Sequence[JsonValue],
        *,
        by: str,
        icon: JsonValue = None,
        cover: JsonValue = None,
    ) -> wire.StoredPage:
        made = wire.new_blocks(list(children), "body.children", self, seeding=self._actor is Actor.SCENARIO)
        if parent.type is wire.ParentType.DATABASE_ID:
            assert parent.id is not None
            database = self.database(parent.id)
            if database.archived:
                raise wire.archived(wire.Missing.DATABASE)
            values, schema = self._row_values(database.schema_, {}, properties)
            if schema != database.schema_:
                self.world.write_database(
                    database.model_copy(update={"schema_": schema}), operation=Operation.UPDATE, actor=self._actor
                )
        else:
            values = {"title": self._title_value(properties)}
        stamps = self._stamps(by)
        page = wire.StoredPage(
            id=page_id,
            workspace=self.workspace,
            parent=parent,
            stamps=stamps,
            icon=wire.icon(icon, "body.icon"),
            cover=cover,
            properties=values,
        )
        page, _ = self._place(page, page.id, made, by)
        page = page.model_copy(update={"stamps": stamps})
        if parent.type is wire.ParentType.PAGE_ID:
            assert parent.id is not None
            above = self.page(parent.id)
            if above.archived:
                raise wire.archived(wire.Missing.PAGE)
            self._write(page, Operation.CREATE)
            self._write(self._stub(above, wire.BlockType.CHILD_PAGE, page.id, by), Operation.UPDATE)
        else:
            self._write(page, Operation.CREATE)
        return page

    def _title_value(self, properties: wire.Json) -> wire.Json:
        """A page outside a database has one property, its title, under `title` or any name."""
        if len(properties) > 1:
            raise wire.invalid("A page that is not in a database has only a title property.")
        if not properties:
            return {"id": "title", "type": "title", "title": []}
        name, value = next(iter(properties.items()))
        given = value
        if isinstance(given, dict) and "title" in given:
            given = given["title"]
        if not isinstance(given, list):
            raise wire.invalid(f"body.properties.{name} should be the page's title, as rich text.")
        return {"id": "title", "type": "title", "title": wire.rich_text(given, f"body.properties.{name}", self)}

    def _row_values(
        self, schema: dict[str, wire.Json], current: dict[str, wire.Json], given: wire.Json
    ) -> tuple[dict[str, wire.Json], dict[str, wire.Json]]:
        """A row's values after `given`, checked against its database, and the schema after them."""
        by_id = {str(s["id"]): name for name, s in schema.items()}
        values = {name: current[name] if name in current else wire.empty_value(s) for name, s in schema.items()}
        widened = dict(schema)
        for key, raw in given.items():
            name = key if key in widened else by_id[key] if key in by_id else None
            if name is None:
                raise wire.invalid(f"{key} is not a property of this database.")
            values[name], widened[name] = wire.property_value(widened[name], raw, f"body.properties.{key}", self, self)
        return values, widened

    def update_page(self, page_id: str, body: wire.Json, *, by: str) -> wire.StoredPage:
        page = self.page(page_id)
        wire.only_keys(body, ["properties", "archived", "in_trash", "icon", "cover"], "body")
        restoring = False
        archived = page.archived
        for key in ("archived", "in_trash"):
            if key in body:
                archived = wire.as_bool(body[key], f"body.{key}")
                restoring = restoring or (page.archived and not archived)
        if page.archived and not restoring and any(k in body for k in ("properties", "icon", "cover")):
            raise wire.archived(wire.Missing.PAGE)
        update: dict[str, object] = {"archived": archived}
        if "properties" in body:
            given = wire.as_object(body["properties"], "body.properties")
            if is_row(page):
                assert page.parent.id is not None
                database = self.database(page.parent.id)
                values, schema = self._row_values(database.schema_, page.properties, given)
                if schema != database.schema_:
                    self.world.write_database(
                        database.model_copy(update={"schema_": schema}), operation=Operation.UPDATE, actor=self._actor
                    )
                update["properties"] = values
            elif given:
                update["properties"] = {"title": self._title_value(given)}
        if "icon" in body:
            update["icon"] = wire.icon(body["icon"], "body.icon")
        if "cover" in body:
            update["cover"] = body["cover"]
        update["stamps"] = page.stamps.edited(wire.stamp(self.now()), by)
        changed = page.model_copy(update=update)
        self._write(changed, Operation.UPDATE)
        return changed

    def archive(self, page_id: str, *, by: str) -> wire.StoredPage:
        return self.update_page(page_id, {"archived": True}, by=by)

    # ------------------------------------------------------------------ blocks

    def append(
        self, container: str, children: JsonValue, *, after: str | None, by: str
    ) -> tuple[wire.StoredPage, list[wire.StoredBlock]]:
        if not wire.as_list(children, "body.children"):
            raise wire.invalid("body.children should hold at least one block.")
        page = self.world.page(container)
        if page is not None and page.workspace == self.workspace:
            if page.archived:
                raise wire.archived(wire.Missing.PAGE)
            made = wire.new_blocks(children, "body.children", self)
            changed, top = self._place(page, page.id, made, by, after=after)
        else:
            page = self.world.holding(self.workspace, container)
            if page is None:
                raise wire.not_found(wire.Missing.BLOCK, container)
            block = page.blocks[container]
            if block.archived or page.archived:
                raise wire.archived(wire.Missing.BLOCK)
            if block.type is wire.BlockType.TABLE:
                rows = [
                    wire.table_row(c, block.content, f"body.children[{i}]", self)
                    for i, c in enumerate(wire.as_list(children, "body.children"))
                ]
                changed, top = self._place(page, container, rows, by, after=after)
            elif block.type in (wire.BlockType.CHILD_PAGE, wire.BlockType.CHILD_DATABASE) or not wire.takes_children(
                block.type, block.content
            ):
                raise wire.invalid(f"A {block.type.value} block cannot have children appended to it.")
            else:
                made = wire.new_blocks(children, "body.children", self)
                changed, top = self._place(page, container, made, by, after=after)
        self._write(changed, Operation.UPDATE)
        return changed, top

    def update_block(self, block_id: str, body: wire.Json, *, by: str) -> tuple[wire.StoredPage, wire.StoredBlock]:
        page = self.world.holding(self.workspace, block_id)
        if page is None:
            raise wire.not_found(wire.Missing.BLOCK, block_id)
        block = page.blocks[block_id]
        if block.type in (wire.BlockType.CHILD_PAGE, wire.BlockType.CHILD_DATABASE):
            raise wire.invalid(f"A {block.type.value} is changed through its page or database.")
        allowed = ["type", "archived", "in_trash", block.type.value]
        named = [k for k in body if k not in allowed]
        if named:
            raise wire.invalid(f"body.{named[0]}: a block's type cannot be changed; this is a {block.type.value}.")
        if "type" in body and body["type"] != block.type.value:
            raise wire.invalid(f"body.type: a block's type cannot be changed; this is a {block.type.value}.")
        archived = block.archived
        for key in ("archived", "in_trash"):
            if key in body:
                archived = wire.as_bool(body[key], f"body.{key}")
        content = block.content
        if block.type.value in body:
            if block.archived and archived:
                raise wire.archived(wire.Missing.BLOCK)
            given = wire.as_object(body[block.type.value], f"body.{block.type.value}")
            if "children" in given:
                raise wire.invalid(f"body.{block.type.value}.children: append children with the children endpoint.")
            if block.type is wire.BlockType.TABLE_ROW:
                content = _row_cells(block, given, self)
            elif block.type is wire.BlockType.TABLE:
                wire.only_keys(given, ["has_column_header", "has_row_header"], "body.table")
                content = {**block.content, **given}
                content = wire.block_content(block.type, content, "body.table", self, creating=False)
            else:
                merged = {**block.content, **given}
                content = wire.block_content(
                    block.type, _rich_back(merged), f"body.{block.type.value}", self, creating=False
                )
        when = wire.stamp(self.now())
        changed_block = block.model_copy(
            update={"content": content, "archived": archived, "stamps": block.stamps.edited(when, by)}
        )
        changed = page.model_copy(
            update={"blocks": {**page.blocks, block_id: changed_block}, "stamps": page.stamps.edited(when, by)}
        )
        self._write(changed, Operation.UPDATE)
        return changed, changed_block

    def archive_block(self, block_id: str, *, by: str) -> tuple[wire.StoredPage, wire.StoredBlock]:
        page = self.world.holding(self.workspace, block_id)
        if page is None:
            raise wire.not_found(wire.Missing.BLOCK, block_id)
        block = page.blocks[block_id]
        if block.type is wire.BlockType.CHILD_PAGE:
            self.archive(block_id, by=by)
            return page, block
        if block.type is wire.BlockType.CHILD_DATABASE:
            self.update_database(block_id, {"archived": True}, by=by)
            return page, block
        return self.update_block(block_id, {"archived": True}, by=by)

    # ------------------------------------------------------------------ databases

    def create_database(
        self,
        database_id: str,
        parent: wire.Parent,
        title: list[JsonValue],
        schema: dict[str, wire.Json],
        *,
        inline: bool,
        by: str,
    ) -> wire.StoredDatabase:
        if parent.type is not wire.ParentType.PAGE_ID or parent.id is None:
            raise wire.invalid("body.parent: a database is made inside a page, with page_id.")
        above = self.page(parent.id)
        if above.archived:
            raise wire.archived(wire.Missing.PAGE)
        titles = [n for n, s in schema.items() if wire.schema_type(s) is wire.PropertyType.TITLE]
        if len(titles) != 1:
            raise wire.invalid("body.properties should have exactly one title property.")
        database = wire.StoredDatabase(
            id=database_id,
            workspace=self.workspace,
            parent=parent,
            stamps=self._stamps(by),
            title=title,
            schema=schema,
            is_inline=inline,
        )
        self.world.write_database(database, operation=Operation.CREATE, actor=self._actor)
        self._write(self._stub(above, wire.BlockType.CHILD_DATABASE, database_id, by), Operation.UPDATE)
        return database

    def update_database(self, database_id: str, body: wire.Json, *, by: str) -> wire.StoredDatabase:
        database = self.database(database_id)
        wire.only_keys(
            body, ["title", "description", "properties", "is_inline", "archived", "in_trash", "icon", "cover"], "body"
        )
        update: dict[str, object] = {}
        for key in ("archived", "in_trash"):
            if key in body:
                update["archived"] = wire.as_bool(body[key], f"body.{key}")
        if database.archived and "archived" not in update:
            raise wire.archived(wire.Missing.DATABASE)
        if "title" in body:
            update["title"] = wire.rich_text(body["title"], "body.title", self)
        if "description" in body:
            update["description"] = wire.rich_text(body["description"], "body.description", self)
        if "is_inline" in body:
            update["is_inline"] = wire.as_bool(body["is_inline"], "body.is_inline")
        if "icon" in body:
            update["icon"] = wire.icon(body["icon"], "body.icon")
        if "properties" in body:
            update["schema_"] = self._schema_after(database, wire.as_object(body["properties"], "body.properties"))
        update["stamps"] = database.stamps.edited(wire.stamp(self.now()), by)
        changed = database.model_copy(update=update)
        self.world.write_database(changed, operation=Operation.UPDATE, actor=self._actor)
        return changed

    def _schema_after(self, database: wire.StoredDatabase, given: wire.Json) -> dict[str, wire.Json]:
        schema = dict(database.schema_)
        by_id = {str(s["id"]): name for name, s in schema.items()}
        for key, raw in given.items():
            name = key if key in schema else by_id[key] if key in by_id else None
            where = f"body.properties.{key}"
            if raw is None:
                if name is None:
                    raise wire.invalid(f"{where}: there is no such property to remove.")
                if wire.schema_type(schema[name]) is wire.PropertyType.TITLE:
                    raise wire.invalid(f"{where}: a database's title property cannot be removed.")
                del schema[name]
                continue
            asked = wire.as_object(raw, where)
            if name is not None and set(asked) <= {"name"}:
                renamed = wire.as_text(asked["name"], f"{where}.name")
                schema[renamed] = {**schema.pop(name), "name": renamed}
                continue
            made = wire.schema_from_request(
                database.id, name or key, raw, where, lambda d: self.world.database(d) is not None
            )
            if name is not None:
                if (
                    wire.schema_type(schema[name]) is wire.PropertyType.TITLE
                    and wire.schema_type(made) is not wire.PropertyType.TITLE
                ):
                    raise wire.invalid(f"{where}: a database's title property cannot change its type.")
                made = {**made, "id": schema[name]["id"]}
                del schema[name]
            elif wire.schema_type(made) is wire.PropertyType.TITLE:
                raise wire.invalid(f"{where}: a database has only one title property.")
            schema[str(made["name"])] = made
        return schema

    # ------------------------------------------------------------------ comments

    def comment(self, page_id: str, discussion: str | None, text: list[JsonValue], *, by: str) -> wire.StoredComment:
        page = self.page(page_id)
        found = wire.StoredComment(
            id=self.mint("comment", within=page.id),
            page=page.id,
            discussion_id=discussion or self.mint("discussion", within=page.id),
            created_time=wire.stamp(self.now()),
            created_by=by,
            rich_text=text,
        )
        self.world.write_comment(found, actor=self._actor, at=self.now())
        return found


def _rich_back(content: wire.Json) -> wire.Json:
    """A stored body handed back through the request checks: its runs are written as a caller would."""
    found = dict(content)
    for key in ("rich_text", "caption"):
        held = found[key] if key in found else None
        if isinstance(held, list):
            found[key] = [_as_request(item) for item in held]
    return found


def _as_request(item: JsonValue) -> JsonValue:
    if not isinstance(item, dict):
        return item
    return {k: v for k, v in item.items() if k not in ("plain_text", "href")}


def _row_cells(block: wire.StoredBlock, given: wire.Json, names: wire.Names) -> wire.Json:
    wire.only_keys(given, ["cells"], "body.table_row")
    cells = wire.as_list(given["cells"] if "cells" in given else None, "body.table_row.cells")
    width = len(wire.as_list(block.content["cells"], "cells"))
    if len(cells) != width:
        raise wire.invalid(f"body.table_row.cells should have {width} cells; got {len(cells)}.")
    return {"cells": [wire.rich_text(c, f"body.table_row.cells[{i}]", names) for i, c in enumerate(cells)]}
