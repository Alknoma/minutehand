"""Notion as entities in the run's store.

| Notion thing            | `EntityKind` | external id           | parent (what it is listed under) |
|-------------------------|--------------|-----------------------|----------------------------------|
| workspace               | RECORD       | its id                | `WORKSPACES`                     |
| page (not a row)        | DOCUMENT     | its id                | `pages:<workspace id>`           |
| database row            | RECORD       | its id                | its database's id                |
| database                | RECORD       | its id                | `databases:<workspace id>`       |
| block                   | inside its page's entity, never its own                  |
| comment                 | COMMENT      | its id                | its page's id                    |
| user (person or bot)    | RECORD       | its id                | `users:<workspace id>`           |
| integration             | RECORD       | minted from its bot's | `integrations:<workspace id>`    |
| token, code             | RECORD       | sha256 of the secret  | `TOKENS`                         |
| the seed's faults       | RECORD       | `SCHEDULE`            | None                             |
| a fault's firings       | RECORD       | `fault-<n>`           | `SCHEDULE`                       |
| webhook subscription    | RECORD       | its id                | `WEBHOOKS`                       |
| an event owed to one    | RECORD       | its id                | `owed:<subscription id>`         |

A page's blocks are stored inside the page's own entity (`wire.StoredPage.blocks`), so
every block-level edit is one new version of one page: one event per API call, with the
page as the entity, and its snapshot is the page's whole text after the edit. A child
page's or child database's block in its parent is a stub whose title is read live from
the page or database it stands for.

A change to a page or a comment that a webhook subscription is owed (`owed_for`) is written as an owed event
just before the change itself, so the change is still the last event of the call; `webhooks.deliver` sends and
deletes them.

Every body carries a `kind` literal (`wire.Stored`), so what is read back is told apart by
its type. Nothing is held between calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from minutehand.adapters.providers.notion import wire
from minutehand.adapters.providers.notion.manifest import MANIFEST
from minutehand.domain.world import (
    Actor,
    Change,
    DocumentSnapshot,
    EntityKind,
    EntityRef,
    Operation,
    RecordSnapshot,
    Snapshot,
    WorldEvent,
)
from minutehand.ports.store import Store

WORKSPACES = "workspaces"
TOKENS = "tokens"
SCHEDULE = "schedule"
DECLARED = "schedule-declared"
"""The faults declared on an open world (`provider-faults`), kept apart from the seed's own, so a later seed fragment
that adds to the seed's schedule never rewrites what was declared, and each keeps its own count of firings."""
WEBHOOKS = "webhooks"
_SCAN = 1000


def _ref(kind: EntityKind, external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=kind, external_id=external_id)


def page_ref(page: wire.StoredPage) -> EntityRef:
    return _ref(EntityKind.RECORD if is_row(page) else EntityKind.DOCUMENT, page.id)


def record_ref(external_id: str) -> EntityRef:
    return _ref(EntityKind.RECORD, external_id)


def is_row(page: wire.StoredPage) -> bool:
    return page.parent.type is wire.ParentType.DATABASE_ID


def _pages_of(workspace: str) -> str:
    return f"pages:{workspace}"


def _databases_of(workspace: str) -> str:
    return f"databases:{workspace}"


def _users_of(workspace: str) -> str:
    return f"users:{workspace}"


def _integrations_of(workspace: str) -> str:
    return f"integrations:{workspace}"


def _integration_key(bot: str) -> str:
    """An integration's entity: its bot user holds the bot's own id."""
    return wire.minted_id("integration", bot)


def _owed_of(subscription: str) -> str:
    return f"owed:{subscription}"


def _changes(
    before: wire.StoredPage | None, after: wire.StoredPage
) -> list[tuple[wire.WebhookEvent, list[str], list[str]]]:
    """What a webhook calls this change: created, deleted or undeleted, or properties and content updated, with
    the blocks and properties that changed."""
    if before is None:
        return [(wire.WebhookEvent.PAGE_CREATED, [], [])]
    if after.archived != before.archived:
        return [(wire.WebhookEvent.PAGE_DELETED if after.archived else wire.WebhookEvent.PAGE_UNDELETED, [], [])]
    found: list[tuple[wire.WebhookEvent, list[str], list[str]]] = []
    properties = sorted(
        str(value["id"]) if "id" in value else name
        for name, value in after.properties.items()
        if name not in before.properties or before.properties[name] != value
    )
    if properties:
        found.append((wire.WebhookEvent.PAGE_PROPERTIES_UPDATED, [], properties))
    blocks = [b for b, block in after.blocks.items() if b not in before.blocks or before.blocks[b] != block]
    if blocks or after.children != before.children:
        found.append((wire.WebhookEvent.PAGE_CONTENT_UPDATED, blocks, []))
    return found


def title_of(page: wire.StoredPage) -> str:
    for value in page.properties.values():
        if value["type"] == wire.PropertyType.TITLE.value:
            return wire.plain(value["title"])
    return ""


def database_title(database: wire.StoredDatabase) -> str:
    return wire.plain(database.title)


class NotionWorld:
    """Typed reads and writes of one run's Notion entities."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ ids

    def next_seq(self) -> int:
        return self._store.head() + 1

    def mint(self, *parts: str) -> str:
        """A new object's id: from the event about to be written and what distinguishes it within the call. Never
        from the run, so a fork that does what its parent did makes the same ids, and the two compare alike."""
        return wire.minted_id("minted", str(self.next_seq()), *parts)

    # ------------------------------------------------------------------ reads

    def _read(self, ref: EntityRef) -> wire.Stored | None:
        stored = self._store.get(ref)
        return None if stored is None else wire.parse(stored.body)

    def _list(self, kind: EntityKind, parent: str) -> Iterator[wire.Stored]:
        after: str | None = None
        while True:
            page = self._store.children(MANIFEST.key, kind, parent, after=after, limit=_SCAN)
            for stored in page:
                yield wire.parse(stored.body)
            if len(page) < _SCAN:
                return
            after = page[-1].entity.external_id

    def page(self, page_id: str) -> wire.StoredPage | None:
        for kind in (EntityKind.DOCUMENT, EntityKind.RECORD):
            found = self._read(_ref(kind, page_id))
            if isinstance(found, wire.StoredPage):
                return found
        return None

    def database(self, database_id: str) -> wire.StoredDatabase | None:
        found = self._read(record_ref(database_id))
        return found if isinstance(found, wire.StoredDatabase) else None

    def user(self, user_id: str) -> wire.StoredUser | None:
        found = self._read(record_ref(user_id))
        return found if isinstance(found, wire.StoredUser) else None

    def integration(self, integration_id: str) -> wire.StoredIntegration | None:
        found = self._read(record_ref(_integration_key(integration_id)))
        return found if isinstance(found, wire.StoredIntegration) else None

    def workspace(self, workspace_id: str) -> wire.StoredWorkspace | None:
        found = self._read(record_ref(workspace_id))
        return found if isinstance(found, wire.StoredWorkspace) else None

    def token(self, secret: str) -> wire.StoredToken | None:
        found = self._read(record_ref(wire.digest(secret)))
        return found if isinstance(found, wire.StoredToken) else None

    def schedule(self) -> wire.StoredSchedule:
        found = self._read(record_ref(SCHEDULE))
        return found if isinstance(found, wire.StoredSchedule) else wire.StoredSchedule()

    def declared(self) -> wire.StoredSchedule:
        found = self._read(record_ref(DECLARED))
        return found if isinstance(found, wire.StoredSchedule) else wire.StoredSchedule()

    def armed(self) -> list[tuple[str, wire.PlannedFault]]:
        """Every armed fault with the key its firings are counted under: the seed's in its order, then those
        declared on the open world in theirs."""
        seeded = [(str(n), f) for n, f in enumerate(self.schedule().faults)]
        return seeded + [(f"declared-{n}", f) for n, f in enumerate(self.declared().faults)]

    def fired(self, fault: str) -> int:
        found = self._read(record_ref(f"fault-{fault}"))
        return found.count if isinstance(found, wire.StoredCount) else 0

    def workspaces(self) -> list[wire.StoredWorkspace]:
        return [w for w in self._list(EntityKind.RECORD, WORKSPACES) if isinstance(w, wire.StoredWorkspace)]

    def users(self, workspace: str) -> list[wire.StoredUser]:
        found = self._list(EntityKind.RECORD, _users_of(workspace))
        return [u for u in found if isinstance(u, wire.StoredUser) and not u.removed]

    def integrations(self, workspace: str) -> list[wire.StoredIntegration]:
        found = self._list(EntityKind.RECORD, _integrations_of(workspace))
        return [i for i in found if isinstance(i, wire.StoredIntegration)]

    def databases(self, workspace: str) -> list[wire.StoredDatabase]:
        found = self._list(EntityKind.RECORD, _databases_of(workspace))
        return [d for d in found if isinstance(d, wire.StoredDatabase)]

    def rows(self, database_id: str) -> list[wire.StoredPage]:
        return [p for p in self._list(EntityKind.RECORD, database_id) if isinstance(p, wire.StoredPage)]

    def pages(self, workspace: str) -> list[wire.StoredPage]:
        """Every page of the workspace, rows of its databases included."""
        found = [p for p in self._list(EntityKind.DOCUMENT, _pages_of(workspace)) if isinstance(p, wire.StoredPage)]
        for database in self.databases(workspace):
            found += self.rows(database.id)
        return found

    def _written_order(self, kind: EntityKind, parent: str) -> list[wire.Stored]:
        """Every entity under `parent`, oldest write first: an id is a hash and says nothing of order."""
        found: list[tuple[int, wire.Stored]] = []
        after: str | None = None
        while True:
            batch = self._store.children(MANIFEST.key, kind, parent, after=after, limit=_SCAN)
            found += [(stored.seq, wire.parse(stored.body)) for stored in batch]
            if len(batch) < _SCAN:
                break
            after = batch[-1].entity.external_id
        return [body for _, body in sorted(found, key=lambda pair: pair[0])]

    def comments(self, page_id: str) -> list[wire.StoredComment]:
        """Oldest first."""
        return [c for c in self._written_order(EntityKind.COMMENT, page_id) if isinstance(c, wire.StoredComment)]

    def holding(self, workspace: str, block_id: str) -> wire.StoredPage | None:
        """The page whose tree holds this block."""
        return next((p for p in self.pages(workspace) if block_id in p.blocks), None)

    def title(self, object_id: str) -> str | None:
        """The title of a page or database, or None when there is neither."""
        page = self.page(object_id)
        if page is not None:
            return title_of(page)
        database = self.database(object_id)
        return None if database is None else database_title(database)

    def editor(self, user_id: str) -> str:
        """Who made a change, as the world's snapshot names them: a person's email, a bot's name."""
        user = self.user(user_id)
        if user is None:
            return user_id
        return user.email if user.type is wire.UserType.PERSON and user.email else user.name

    # ------------------------------------------------------------------ snapshots

    def page_text(self, page: wire.StoredPage) -> str:
        """The page's blocks flattened to text, one block a line, nested blocks indented, archived ones left out."""
        lines: list[str] = []

        def walk(container: str, depth: int) -> None:
            for block_id in page.children[container] if container in page.children else []:
                block = page.blocks[block_id]
                if block.archived or self._stub_archived(block):
                    continue
                stub = (
                    self.title(block.id)
                    if block.type in (wire.BlockType.CHILD_PAGE, wire.BlockType.CHILD_DATABASE)
                    else None
                )
                lines.append("  " * depth + wire.block_text(block.type, block.content, stub))
                walk(block.id, depth + 1)

        walk(page.id, 0)
        return "\n".join(lines)

    def _stub_archived(self, block: wire.StoredBlock) -> bool:
        if block.type is wire.BlockType.CHILD_PAGE:
            page = self.page(block.id)
            return page is None or page.archived
        if block.type is wire.BlockType.CHILD_DATABASE:
            database = self.database(block.id)
            return database is None or database.archived
        return False

    def _parent_title(self, parent: wire.Parent) -> str | None:
        if parent.id is None:
            return None
        if parent.type is wire.ParentType.BLOCK_ID:
            return None
        return self.title(parent.id)

    def snapshot(self, page: wire.StoredPage, at: datetime) -> Snapshot:
        if is_row(page):
            words = [title_of(page)]
            for name, value in page.properties.items():
                if value["type"] != wire.PropertyType.TITLE.value:
                    words.append(f"{name}: {wire.value_text(value)}")
            words.append(self.page_text(page))
            return RecordSnapshot(resource="database_row", text="\n".join(w for w in words if w))
        return DocumentSnapshot(
            title=title_of(page),
            text=self.page_text(page),
            parent=self._parent_title(page.parent),
            last_edited_by=self.editor(page.stamps.last_edited_by),
            last_edited_at=at,
            owner=self.editor(page.stamps.created_by),
            space=space.name if (space := self.workspace(page.workspace)) is not None else None,
        )

    # ------------------------------------------------------------------ writes

    def write_page(self, page: wire.StoredPage, *, operation: Operation, actor: Actor, at: datetime) -> WorldEvent:
        before = None if operation is Operation.CREATE else self.page(page.id)
        for kind, blocks, properties in _changes(before, page):
            self._owe(page, kind, at, actor, blocks=blocks, properties=properties)
        return self._store.apply(
            Change(
                entity=page_ref(page),
                operation=operation,
                actor=actor,
                body=wire.dump(page),
                parent=page.parent.id if is_row(page) else _pages_of(page.workspace),
                after=self.snapshot(page, at),
            )
        )

    def write_database(self, database: wire.StoredDatabase, *, operation: Operation, actor: Actor) -> WorldEvent:
        names = " ".join(database.schema_)
        return self._store.apply(
            Change(
                entity=record_ref(database.id),
                operation=operation,
                actor=actor,
                body=wire.dump(database),
                parent=_databases_of(database.workspace),
                after=RecordSnapshot(resource="database", text=f"{database_title(database)}\n{names}"),
            )
        )

    def write_comment(self, comment: wire.StoredComment, *, actor: Actor, at: datetime) -> WorldEvent:
        page = self.page(comment.page)
        if page is not None:
            self._owe(page, wire.WebhookEvent.COMMENT_CREATED, at, actor, comment=comment)
        return self._store.apply(
            Change(
                entity=_ref(EntityKind.COMMENT, comment.id),
                operation=Operation.CREATE,
                actor=actor,
                body=wire.dump(comment),
                parent=comment.page,
                after=RecordSnapshot(resource="comment", text=wire.plain(comment.rich_text)),
            )
        )

    def _setup(self, external_id: str, body: wire.Model, parent: str | None, operation: Operation) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=record_ref(external_id),
                operation=operation,
                actor=Actor.SCENARIO,
                body=wire.dump(body),
                parent=parent,
            )
        )

    def write_workspace(self, workspace: wire.StoredWorkspace) -> WorldEvent:
        return self._setup(workspace.id, workspace, WORKSPACES, Operation.CREATE)

    def write_user(self, user: wire.StoredUser, *, operation: Operation = Operation.CREATE) -> WorldEvent:
        return self._setup(user.id, user, _users_of(user.workspace), operation)

    def write_integration(self, integration: wire.StoredIntegration, *, operation: Operation) -> WorldEvent:
        return self._setup(
            _integration_key(integration.id), integration, _integrations_of(integration.workspace), operation
        )

    def write_token(self, secret: str, token: wire.StoredToken, *, operation: Operation, actor: Actor) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=record_ref(wire.digest(secret)),
                operation=operation,
                actor=actor,
                body=wire.dump(token),
                parent=TOKENS,
            )
        )

    def write_schedule(self, schedule: wire.StoredSchedule, *, declared: bool = False) -> WorldEvent:
        name = DECLARED if declared else SCHEDULE
        found = self._read(record_ref(name))
        return self._setup(name, schedule, None, Operation.CREATE if found is None else Operation.UPDATE)

    def count_fault(self, fault: str, count: int) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=record_ref(f"fault-{fault}"),
                operation=Operation.UPDATE if count > 1 else Operation.CREATE,
                actor=Actor.SCENARIO,
                body=wire.dump(wire.StoredCount(count=count)),
                parent=SCHEDULE,
                after=RecordSnapshot(resource="fault", text=f"fault {fault} fired {count} time(s)"),
            )
        )

    # ------------------------------------------------------------------ webhooks

    def write_webhook(self, webhook: wire.StoredWebhook, *, operation: Operation) -> WorldEvent:
        return self._setup(webhook.id, webhook, WEBHOOKS, operation)

    def webhooks(self) -> list[wire.StoredWebhook]:
        return [w for w in self._list(EntityKind.RECORD, WEBHOOKS) if isinstance(w, wire.StoredWebhook)]

    def owed(self, subscription: str) -> list[wire.StoredOwedEvent]:
        found = self._written_order(EntityKind.RECORD, _owed_of(subscription))
        return [o for o in found if isinstance(o, wire.StoredOwedEvent)]

    def sent(self, owed: wire.StoredOwedEvent) -> WorldEvent:
        """The event has been sent (whatever the subscriber answered): it is owed no longer."""
        return self._store.apply(
            Change(
                entity=record_ref(owed.id),
                operation=Operation.DELETE,
                actor=Actor.SCENARIO,
                parent=_owed_of(owed.subscription),
            )
        )

    def ancestry(self, object_id: str, workspace: str) -> list[str] | None:
        """The object and the pages and databases above it, nearest first; None when it is not in the workspace."""
        chain: list[str] = []
        current: str | None = object_id
        while current is not None and current not in chain:
            page = self.page(current)
            database = None if page is not None else self.database(current)
            if page is not None:
                if page.workspace != workspace:
                    return None
                chain.append(current)
                current = page.parent.id
                continue
            if database is not None:
                if database.workspace != workspace:
                    return None
                chain.append(current)
                current = database.parent.id
                continue
            holder = self.holding(workspace, current)
            if holder is None:
                return chain or None
            current = holder.id
        return chain

    def _owe(
        self,
        page: wire.StoredPage,
        kind: wire.WebhookEvent,
        at: datetime,
        actor: Actor,
        *,
        blocks: list[str] | None = None,
        properties: list[str] | None = None,
        comment: wire.StoredComment | None = None,
    ) -> None:
        """Owe `kind` to every subscription in the page's workspace that asked for it and whose integration can see
        the page, authored by whoever last edited it (or wrote the comment)."""
        hooks = [w for w in self.webhooks() if w.workspace == page.workspace and kind in w.events]
        if not hooks:
            return
        above = self.ancestry(page.parent.id, page.workspace) if page.parent.id is not None else None
        chain = [page.id, *(above or [])]
        author = comment.created_by if comment is not None else page.stamps.last_edited_by
        user = self.user(author)
        when = at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for hook in hooks:
            integration = self.integration(hook.integration)
            if integration is None or not any(c in integration.shared for c in chain):
                continue
            owed_id = self.mint("owed", hook.id, kind.value)
            self._store.apply(
                Change(
                    entity=record_ref(owed_id),
                    operation=Operation.CREATE,
                    actor=actor,
                    body=wire.dump(
                        wire.StoredOwedEvent(
                            id=owed_id,
                            subscription=hook.id,
                            type=kind,
                            timestamp=when,
                            entity_id=comment.id if comment is not None else page.id,
                            entity_type="comment" if comment is not None else "page",
                            author=author,
                            author_type=user.type if user is not None else wire.UserType.PERSON,
                            parent=page.parent
                            if comment is None
                            else wire.Parent(type=wire.ParentType.PAGE_ID, id=page.id),
                            page_id=page.id if comment is not None else None,
                            updated_blocks=blocks or [],
                            updated_properties=properties or [],
                        )
                    ),
                    parent=_owed_of(hook.id),
                )
            )

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))
