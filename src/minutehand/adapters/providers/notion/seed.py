"""The Notion a scenario starts with, from the scenario's people and documents and the provider's own seed.

The provider's own seed is `NotionSeed`, read out of `Scenario.provider_seed("notion")`:
workspaces, each with its members, its integrations (each with its tokens, its capabilities
and what is shared with it), its page trees with their blocks, and its databases with their
schema and rows; and the faults the seed arms. Every key is unique across the whole seed,
and every id is derived from its workspace and key, so the same seed mints the same ids in
every run.

- Every person of the scenario is a member of every workspace unless the workspace names
  its members.
- Each `SeededDocument` for this provider is a page in the first workspace, under a
  top-level page named by its `folder` (made the first time a document names it) or at the
  top, shared with every integration of that workspace.
- With no `NotionSeed`, there is one workspace with the scenario's people and documents and
  no integration, so every call is refused as unauthorized: a token must be seeded.

Everything is written as actor SCENARIO, stamped with the scenario's start.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from minutehand.adapters.providers.notion import wire
from minutehand.adapters.providers.notion.edits import Editor
from minutehand.adapters.providers.notion.manifest import MANIFEST
from minutehand.adapters.providers.notion.state import NotionWorld
from minutehand.domain.scenario import Model, Person, Scenario, SeededDocument
from minutehand.domain.world import Actor, Operation
from minutehand.ports.store import Store

SeedValue = str | int | float | bool | list[str] | None
"""A row's value as a seed writes it: text, a number, a checkbox, a date as ISO text, a select option's name,
the names of multi-select options, person keys for people, row keys for a relation."""


class SeedBlock(Model):
    type: wire.BlockType
    text: str = ""
    checked: bool = False
    language: str = "plain text"
    url: str | None = None
    emoji: str | None = None
    rows: list[list[str]] = Field(default=[], description="A table's rows, each a list of cell texts")
    header: bool = Field(default=False, description="A table's first row is its column header")
    children: list[SeedBlock] = []

    @model_validator(mode="after")
    def _writable(self) -> Self:
        if self.type in (wire.BlockType.CHILD_PAGE, wire.BlockType.CHILD_DATABASE, wire.BlockType.TABLE_ROW):
            raise ValueError(f"a seed writes a {self.type.value} as a page, a database or a table's rows")
        if self.type is wire.BlockType.TABLE and (not self.rows or len({len(r) for r in self.rows}) != 1):
            raise ValueError("a table needs at least one row, and every row as wide as the first")
        if self.type in (wire.BlockType.BOOKMARK, wire.BlockType.IMAGE, wire.BlockType.LINK_PREVIEW) and not self.url:
            raise ValueError(f"a {self.type.value} needs a url")
        return self


class SeedPage(Model):
    key: str
    title: str
    document: str | None = Field(
        default=None,
        description="The title of a seeded Notion document this page is (the same as `title`): with no blocks, its "
        "body is the document's text, and a happening names it by that title",
    )
    parent: str | None = Field(default=None, description="The key of the page it sits under; None at the top")
    blocks: list[SeedBlock] = []
    created_by: str | None = Field(default=None, description="Person.key; the scenario's owner when None")
    emoji: str | None = None
    archived: bool = False


class SeedProperty(Model):
    name: str
    type: wire.PropertyType
    choices: list[str] = Field(default=[], description="A select's, multi-select's or status's options, in order")
    relates_to: str | None = Field(default=None, description="For a relation: the key of the related database")


class SeedRow(Model):
    key: str
    document: str | None = Field(
        default=None,
        description="The title of a seeded Notion document this row is: its title property is the document's title, "
        "its body the document's text, and a happening names it by that title",
    )
    values: dict[str, SeedValue] = Field(description="By property name")
    blocks: list[SeedBlock] = []
    created_by: str | None = None


class SeedDatabase(Model):
    key: str
    title: str
    parent: str = Field(description="The key of the page it sits in")
    properties: list[SeedProperty] = Field(description="Exactly one has type title")
    rows: list[SeedRow] = []
    inline: bool = False

    @model_validator(mode="after")
    def _one_title(self) -> Self:
        if sum(p.type is wire.PropertyType.TITLE for p in self.properties) != 1:
            raise ValueError(f"database {self.key} needs exactly one title property")
        return self


class SeedAuthorization(Model):
    """A person approved a public integration in their browser; the code is what Notion handed the redirect."""

    code: str
    redirect_uri: str | None = None


class SeedIntegration(Model):
    key: str
    name: str
    type: wire.IntegrationKind = wire.IntegrationKind.INTERNAL
    tokens: list[str] = Field(default=[], description="An internal integration's secrets")
    client_id: str | None = None
    client_secret: str | None = None
    redirect_uris: list[str] = []
    installed_by: str | None = Field(default=None, description="Person.key who installed a public integration")
    authorizations: list[SeedAuthorization] = []
    capabilities: list[wire.Capability] = wire.ALL_CAPABILITIES
    shared: list[str] = Field(default=[], description="Keys of the pages and databases shared with it")

    @model_validator(mode="after")
    def _signs_in(self) -> Self:
        if self.type is wire.IntegrationKind.PUBLIC:
            if not (self.client_id and self.client_secret and self.installed_by):
                raise ValueError(f"public integration {self.key} needs client_id, client_secret and installed_by")
        elif self.authorizations or self.client_id:
            raise ValueError(f"internal integration {self.key} signs in with tokens, not OAuth")
        return self


class SeedWebhook(Model):
    """A webhook subscription set up in an integration's settings: where Notion sends its events, the token it
    signs them with, and which events it asked for."""

    integration: str = Field(description="The key of the integration it belongs to")
    url: str
    verification_token: str = Field(min_length=1, description="Notion's own shape is secret_ and 43 characters")
    events: list[wire.WebhookEvent] = [
        wire.WebhookEvent.PAGE_CREATED,
        wire.WebhookEvent.PAGE_CONTENT_UPDATED,
        wire.WebhookEvent.PAGE_PROPERTIES_UPDATED,
        wire.WebhookEvent.PAGE_DELETED,
    ]
    verified: bool = Field(
        default=False,
        description="Whether the subscriber has verified it already; when not, it is sent the verification request "
        "first, and counts as verified once that is answered 2xx",
    )


class SeedWorkspace(Model):
    key: str
    name: str
    members: list[str] | None = Field(default=None, description="Person keys; every person when None")
    integrations: list[SeedIntegration] = []
    pages: list[SeedPage] = []
    databases: list[SeedDatabase] = []


class RateLimited(Model):
    """The next `times` matching calls are answered 429 `rate_limited` with `Retry-After`."""

    kind: Literal["rate_limited"] = "rate_limited"
    times: int = Field(default=1, ge=1)
    retry_after: int = Field(default=1, ge=0, description="Seconds")
    method: str | None = None
    path: str | None = Field(default=None, description="A path prefix, e.g. /v1/search")
    integration: str | None = Field(default=None, description="Only this integration's calls; any when None")


class Conflicting(Model):
    """The next `times` block edits (update, delete, append) are answered 409 `conflict_error`, as when
    someone else saved the same content first."""

    kind: Literal["conflict"] = "conflict"
    times: int = Field(default=1, ge=1)
    integration: str | None = None


SeedFault = Annotated[RateLimited | Conflicting, Field(discriminator="kind")]


class NotionSeed(Model):
    workspaces: list[SeedWorkspace] = []
    webhooks: list[SeedWebhook] = []
    faults: list[SeedFault] = []

    @model_validator(mode="after")
    def _keys(self) -> Self:
        keys: list[str] = []
        for workspace in self.workspaces:
            keys.append(workspace.key)
            keys += [i.key for i in workspace.integrations]
            keys += [p.key for p in workspace.pages]
            keys += [d.key for d in workspace.databases]
            keys += [r.key for d in workspace.databases for r in d.rows]
        twice = sorted({k for k in keys if keys.count(k) > 1})
        if twice:
            raise ValueError(f"a key is used twice in the Notion seed: {', '.join(twice)}")
        for workspace in self.workspaces:
            content = {p.key for p in workspace.pages} | {d.key for d in workspace.databases}
            for integration in workspace.integrations:
                missing = sorted(set(integration.shared) - content)
                if missing:
                    raise ValueError(f"{integration.key} is shared {', '.join(missing)}, not in {workspace.key}")
        integrations = {i.key for w in self.workspaces for i in w.integrations}
        for fault in self.faults:
            if fault.integration is not None and fault.integration not in integrations:
                raise ValueError(f"a fault names no integration: {fault.integration}")
        for hook in self.webhooks:
            if hook.integration not in integrations:
                raise ValueError(f"a webhook names no integration: {hook.integration}")
        return self


def read(scenario: Scenario) -> NotionSeed:
    found = scenario.provider_seed(MANIFEST.key)
    return NotionSeed() if found is None else NotionSeed.model_validate_json(found.body)


def object_id(workspace: str, key: str) -> str:
    """The id of a seeded workspace, page, database, row or integration's bot."""
    return wire.minted_id("seed", workspace, key)


def person_id(workspace: str, email: str) -> str:
    """A person's user id in a workspace: one per address."""
    return wire.minted_id("person", workspace, email.lower())


DEFAULT_WORKSPACE = SeedWorkspace(key="workspace", name="Workspace")


def _block_json(block: SeedBlock) -> JsonValue:
    """A seeded block as a caller would send it, so it is checked by the same rules."""
    kind = block.type
    run = wire.text_run(block.text) if block.text else []
    body: wire.Json
    if kind is wire.BlockType.TABLE:
        width = len(block.rows[0])
        body = {
            "table_width": width,
            "has_column_header": block.header,
            "children": [
                {"type": "table_row", "table_row": {"cells": [wire.text_run(c) for c in row]}} for row in block.rows
            ],
        }
    elif kind in (wire.BlockType.BOOKMARK, wire.BlockType.LINK_PREVIEW):
        body = {"url": block.url}
    elif kind is wire.BlockType.IMAGE:
        body = {"type": "external", "external": {"url": block.url}}
    elif kind is wire.BlockType.DIVIDER:
        body = {}
    else:
        body = {"rich_text": run}
        if kind is wire.BlockType.TO_DO:
            body["checked"] = block.checked
        if kind is wire.BlockType.CODE:
            body["language"] = block.language
        if kind is wire.BlockType.CALLOUT and block.emoji:
            body["icon"] = {"emoji": block.emoji}
        if block.children and kind in wire.HEADINGS:
            body["is_toggleable"] = True
    if block.children and kind is not wire.BlockType.TABLE:
        body["children"] = [_block_json(c) for c in block.children]
    return {"type": kind.value, kind.value: body}


class _At:
    """The clock a seed writes by: the scenario's start, in setup."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def wake(self) -> int:
        return 0


def seed(scenario: Scenario, world: Store) -> None:
    notion = NotionWorld(world)
    found = read(scenario)
    workspaces = found.workspaces or [DEFAULT_WORKSPACE]
    now = scenario.starts_at
    owner = next(p for p in scenario.people if p.key == scenario.owner)
    people = {p.key: p for p in scenario.people}
    documents = {d.title: d for d in scenario.documents if d.provider == MANIFEST.key}
    claimed = [r.document for w in workspaces for d in w.databases for r in d.rows if r.document is not None]
    claimed += [p.document for w in workspaces for p in w.pages if p.document is not None]
    unknown = sorted(set(claimed) - set(documents))
    if unknown:
        raise ValueError(f"a Notion row is the seeded document {unknown[0]!r}, which is no seeded Notion document")
    for workspace in workspaces:
        _seed_workspace(notion, workspace, people, owner, now, documents)
    _seed_documents(notion, workspaces[0], scenario, owner, now, set(claimed))
    for n, hook in enumerate(found.webhooks):
        where = next(w for w in workspaces for i in w.integrations if i.key == hook.integration)
        notion.write_webhook(
            wire.StoredWebhook(
                id=wire.minted_id("webhook", where.key, str(n)),
                workspace=object_id(where.key, where.key),
                integration=object_id(where.key, hook.integration),
                url=hook.url,
                verification_token=hook.verification_token,
                events=hook.events,
                verified=hook.verified,
            ),
            operation=Operation.CREATE,
        )
    planned = planned_faults(found.faults, workspaces)
    if planned:
        notion.write_schedule(wire.StoredSchedule(faults=planned))


def planned_faults(faults: list[SeedFault], workspaces: list[SeedWorkspace]) -> list[wire.PlannedFault]:
    """The faults as the schedule holds them, each integration named by its bot's id."""
    bots = {i.key: object_id(w.key, i.key) for w in workspaces for i in w.integrations}
    return plan(faults, bots)


def plan(faults: list[SeedFault], bots: dict[str, str]) -> list[wire.PlannedFault]:
    """The faults as the schedule holds them, `bots` naming each integration key's bot id."""
    planned: list[wire.PlannedFault] = []
    for fault in faults:
        bot = None
        if fault.integration is not None:
            if fault.integration not in bots:
                raise ValueError(f"a fault names no integration this world holds: {fault.integration}")
            bot = bots[fault.integration]
        if isinstance(fault, RateLimited):
            planned.append(
                wire.PlannedFault(
                    kind=wire.FaultKind.RATE_LIMITED,
                    times=fault.times,
                    retry_after=fault.retry_after,
                    method=fault.method.upper() if fault.method else None,
                    path=fault.path,
                    integration=bot,
                )
            )
        else:
            planned.append(wire.PlannedFault(kind=wire.FaultKind.CONFLICT, times=fault.times, integration=bot))
    return planned


def _seed_workspace(
    notion: NotionWorld,
    workspace: SeedWorkspace,
    people: dict[str, Person],
    owner: Person,
    now: datetime,
    documents: dict[str, SeededDocument],
) -> None:
    ws = object_id(workspace.key, workspace.key)
    notion.write_workspace(wire.StoredWorkspace(id=ws, name=workspace.name))
    members = workspace.members if workspace.members is not None else list(people)
    unknown = sorted(set(members) - set(people))
    if unknown:
        raise ValueError(f"workspace {workspace.key} names nobody: {', '.join(unknown)}")
    for key in members:
        person = people[key]
        notion.write_user(
            wire.StoredUser(
                id=person_id(ws, person.email),
                workspace=ws,
                type=wire.UserType.PERSON,
                name=person.name,
                email=person.email,
            )
        )
    by_person = {k: person_id(ws, people[k].email) for k in members}
    creator = by_person[owner.key] if owner.key in by_person else next(iter(by_person.values()), ws)

    for integration in workspace.integrations:
        bot = object_id(workspace.key, integration.key)
        notion.write_user(
            wire.StoredUser(id=bot, workspace=ws, type=wire.UserType.BOT, name=integration.name, integration=bot)
        )
    editor = Editor(notion, ws, _At(now), actor=Actor.SCENARIO, seeding=True)
    ids: dict[str, str] = {}
    for page in workspace.pages:
        ids[page.key] = object_id(workspace.key, page.key)
    for database in workspace.databases:
        ids[database.key] = object_id(workspace.key, database.key)
        for row in database.rows:
            ids[row.key] = object_id(workspace.key, row.key)

    def who(key: str | None) -> str:
        if key is None:
            return creator
        if key not in by_person:
            raise ValueError(f"{key} is not a member of workspace {workspace.key}")
        return by_person[key]

    for page in _parents_first(workspace.pages):
        parent = (
            wire.Parent(type=wire.ParentType.WORKSPACE)
            if page.parent is None
            else wire.Parent(type=wire.ParentType.PAGE_ID, id=ids[page.parent])
        )
        if page.parent is not None and page.parent not in ids:
            raise ValueError(f"page {page.key} sits under {page.parent}, which is no page")
        if page.document is not None and page.document != page.title:
            raise ValueError(f"page {page.key} is the document {page.document!r}, and is titled {page.title!r}")
        blocks = [_block_json(b) for b in page.blocks]
        if page.document is not None and not blocks and documents[page.document].text:
            blocks = _paragraphs(documents[page.document].text)[: wire.MAX_CHILDREN]
        editor.create_page(
            ids[page.key],
            parent,
            {"title": {"title": wire.text_run(page.title)}},
            blocks,
            by=who(page.created_by),
            icon={"emoji": page.emoji} if page.emoji else None,
        )
    for database in workspace.databases:
        if database.parent not in {p.key for p in workspace.pages}:
            raise ValueError(f"database {database.key} sits in {database.parent}, which is no page")
        schema: dict[str, wire.Json] = {}
        for prop in database.properties:
            related = None
            if prop.relates_to is not None:
                if prop.relates_to not in {d.key for d in workspace.databases}:
                    raise ValueError(f"{prop.name} relates to {prop.relates_to}, which is no database")
                related = ids[prop.relates_to]
            schema[prop.name] = wire.new_schema(ids[database.key], prop.name, prop.type, prop.choices, related)
        editor.create_database(
            ids[database.key],
            wire.Parent(type=wire.ParentType.PAGE_ID, id=ids[database.parent]),
            wire.text_run(database.title),
            schema,
            inline=database.inline,
            by=creator,
        )
    for database in workspace.databases:
        made = notion.database(ids[database.key])
        assert made is not None
        title = next(p.name for p in database.properties if p.type is wire.PropertyType.TITLE)
        for row in database.rows:
            values: wire.Json = {}
            for name, value in row.values.items():
                if name not in made.schema_:
                    raise ValueError(f"row {row.key} sets {name}, which database {database.key} does not have")
                values[name] = value_request(made.schema_[name], value, ids, by_person)
            blocks = [_block_json(b) for b in row.blocks]
            if row.document is not None:
                if title in row.values:
                    raise ValueError(f"row {row.key} is the document {row.document!r}, whose title is its {title}")
                document = documents[row.document]
                values[title] = {"title": wire.text_run(document.title)}
                blocks = blocks or (_paragraphs(document.text) if document.text else [])
            editor.create_page(
                ids[row.key],
                wire.Parent(type=wire.ParentType.DATABASE_ID, id=ids[database.key]),
                values,
                blocks[: wire.MAX_CHILDREN],
                by=who(row.created_by),
            )
    for page in workspace.pages:
        if page.archived:
            editor.archive(ids[page.key], by=who(page.created_by))
    for integration in workspace.integrations:
        _seed_integration(notion, workspace, integration, ws, ids, by_person)


def _parents_first(pages: list[SeedPage]) -> list[SeedPage]:
    placed: list[SeedPage] = []
    done: set[str] = set()
    pending = list(pages)
    keys = {p.key for p in pages}
    while pending:
        ready = [p for p in pending if p.parent is None or p.parent in done or p.parent not in keys]
        if not ready:
            raise ValueError(f"pages sit under each other in a loop: {', '.join(p.key for p in pending)}")
        for page in ready:
            placed.append(page)
            done.add(page.key)
            pending.remove(page)
    return placed


def value_request(
    schema: wire.Json, value: SeedValue, rows: dict[str, str], people: dict[str, str], *, by_id: bool = False
) -> JsonValue:
    """A value as a seed or a person states it, as the property value a caller would send.

    `people` maps what names a person (a key, or an email) to their user id; `rows` maps a row's key to its id,
    or, with `by_id`, a relation names rows by id."""
    kind = wire.schema_type(schema)
    name = str(schema["name"])
    if kind in (wire.PropertyType.TITLE, wire.PropertyType.RICH_TEXT):
        return {kind.value: wire.text_run(str(value)) if value not in (None, "") else []}
    if kind in (wire.PropertyType.SELECT, wire.PropertyType.STATUS):
        return {kind.value: None if value is None else {"name": str(value)}}
    if kind is wire.PropertyType.MULTI_SELECT:
        return {kind.value: [{"name": n} for n in _names(value)]}
    if kind is wire.PropertyType.DATE:
        return {kind.value: None if value is None else {"start": str(value)}}
    if kind is wire.PropertyType.PEOPLE:
        missing = [k for k in _names(value) if k not in people]
        if missing:
            raise ValueError(f"{name} names nobody in the workspace: {', '.join(missing)}")
        return {kind.value: [{"object": "user", "id": people[k]} for k in _names(value)]}
    if kind is wire.PropertyType.RELATION:
        if by_id:
            return {kind.value: [{"id": k} for k in _names(value)]}
        missing = [k for k in _names(value) if k not in rows]
        if missing:
            raise ValueError(f"{name} relates to no row: {', '.join(missing)}")
        return {kind.value: [{"id": rows[k]} for k in _names(value)]}
    if kind is wire.PropertyType.CHECKBOX:
        return {kind.value: bool(value)}
    if kind is wire.PropertyType.NUMBER:
        return {kind.value: value if isinstance(value, int | float) and not isinstance(value, bool) else None}
    return {kind.value: None if value is None else str(value)}


def _names(value: SeedValue) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _seed_integration(
    notion: NotionWorld,
    workspace: SeedWorkspace,
    integration: SeedIntegration,
    ws: str,
    ids: dict[str, str],
    by_person: dict[str, str],
) -> None:
    bot = object_id(workspace.key, integration.key)
    owner = None
    if integration.installed_by is not None:
        if integration.installed_by not in by_person:
            raise ValueError(f"{integration.key} was installed by {integration.installed_by}, no member")
        owner = by_person[integration.installed_by]
    notion.write_integration(
        wire.StoredIntegration(
            id=bot,
            key=integration.key,
            workspace=ws,
            name=integration.name,
            type=integration.type,
            capabilities=integration.capabilities,
            shared=[ids[k] for k in integration.shared],
            owner=owner,
            client_id=integration.client_id,
            client_secret_digest=wire.digest(integration.client_secret) if integration.client_secret else None,
            redirect_uris=integration.redirect_uris,
        ),
        operation=Operation.CREATE,
    )
    for secret in integration.tokens:
        notion.write_token(
            secret,
            wire.StoredToken(type=wire.TokenKind.INTERNAL, integration=bot),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
        )
    for granted in integration.authorizations:
        notion.write_token(
            granted.code,
            wire.StoredToken(type=wire.TokenKind.CODE, integration=bot, redirect_uri=granted.redirect_uri),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
        )


def _seed_documents(
    notion: NotionWorld,
    workspace: SeedWorkspace,
    scenario: Scenario,
    owner: Person,
    now: datetime,
    rows: set[str],
) -> None:
    documents = [d for d in scenario.documents if d.provider == MANIFEST.key]
    if not documents:
        return
    ws = object_id(workspace.key, workspace.key)
    editor = Editor(notion, ws, _At(now), actor=Actor.SCENARIO, seeding=True)
    users = notion.users(ws)
    by = next((u.id for u in users if u.email == owner.email), users[0].id if users else ws)
    made: list[str] = []
    folders: dict[str, str] = {}
    for n, document in enumerate(documents):
        if document.title in rows:
            continue
        parent = wire.Parent(type=wire.ParentType.WORKSPACE)
        if document.folder is not None:
            if document.folder not in folders:
                folder_id = wire.minted_id("seed", workspace.key, "folder", document.folder)
                editor.create_page(folder_id, parent, {"title": {"title": wire.text_run(document.folder)}}, [], by=by)
                folders[document.folder] = folder_id
                made.append(folder_id)
            parent = wire.Parent(type=wire.ParentType.PAGE_ID, id=folders[document.folder])
        page_id = _document_page(workspace.key, n)
        editor.create_page(
            page_id,
            parent,
            {"title": {"title": wire.text_run(document.title)}},
            _paragraphs(document.text)[: wire.MAX_CHILDREN],
            by=by,
        )
        if document.folder is None:
            made.append(page_id)
    for integration in notion.integrations(ws):
        notion.write_integration(
            integration.model_copy(update={"shared": [*integration.shared, *made]}), operation=Operation.UPDATE
        )


def _paragraphs(text: str) -> list[JsonValue]:
    return [
        {"type": "paragraph", "paragraph": {"rich_text": wire.text_run(line) if line else []}}
        for line in text.split("\n")
    ]


def _document_page(workspace: str, position: int) -> str:
    return wire.minted_id("seed", workspace, "document", str(position))


def document_page(scenario: Scenario, title: str) -> str:
    """The id of the page or row a seeded Notion document was written as."""
    workspaces = read(scenario).workspaces or [DEFAULT_WORKSPACE]
    for workspace in workspaces:
        bridged = [p.key for p in workspace.pages if p.document == title]
        bridged += [r.key for d in workspace.databases for r in d.rows if r.document == title]
        if bridged:
            return object_id(workspace.key, bridged[0])
    documents = [d for d in scenario.documents if d.provider == MANIFEST.key]
    position = next(n for n, d in enumerate(documents) if d.title == title)
    return _document_page(workspaces[0].key, position)
