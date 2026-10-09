"""The Asana workspace as entities in the run's store.

| Asana thing   | `EntityKind` | external id                | parent |
|---|---|---|---|
| workspace     | RECORD  | workspace gid              | `WORKSPACES` |
| user          | RECORD  | user gid                   | `USERS` |
| team          | RECORD  | team gid                   | `TEAMS` |
| project       | RECORD  | project gid                | `PROJECTS` |
| section       | RECORD  | section gid                | its project's gid |
| custom field  | RECORD  | custom field gid           | `CUSTOM_FIELDS` |
| tag           | RECORD  | tag gid                    | `TAGS` |
| credential    | RECORD  | gid derived from the token | `CREDENTIALS` |
| task          | TICKET  | task gid                   | its first project's gid, or the workspace's when it has none |
| story         | COMMENT | story gid                  | its task's gid |

A project's members and the custom fields settled on it are held on the project
record; a task's tags, custom field values and parent on the task. A RECORD is
parsed by the `resource_type` it carries, so a user's gid looked up as a project
answers nothing. Gids are digits of one width, so the store's order by external id
is Asana's order by gid. A seeded gid is derived from the scenario's own names; a
gid the API mints is derived from the sequence of the event that writes it.

A token is never stored: its credential's gid is derived from it, so a token is
looked up by deriving the gid again.

Nothing here is held between calls: every read is a query of the store, so a new app
over the same store sees the same workspace, and a fork sees it as of the fork.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from minutehand.adapters.providers.asana import wire
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.domain.scenario import TicketState
from minutehand.domain.transitions import Transition, transition_change
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    MessageSnapshot,
    Operation,
    Snapshot,
    Stored,
    TicketSnapshot,
    WorldEvent,
)
from minutehand.ports.store import Store

WORKSPACE_NAME = "Simulated Workspace"
TEAM_NAME = "Simulated Team"
AGENT_NAME = "Agent"
AGENT_EMAIL = "agent@workspace.example"
NEW_SECTION = "Untitled section"

WORKSPACES = "workspaces"
USERS = "users"
TEAMS = "teams"
PROJECTS = "projects"
CUSTOM_FIELDS = "custom_fields"
TAGS = "tags"
CREDENTIALS = "credentials"
EVENTS = "events"
WEBHOOKS = "webhooks"

SYNC_LIFETIME = timedelta(hours=24)
"""https://developers.asana.com/docs/events: "Tokens expire after 24 hours"."""

_WIDTH = 16
_MINTED = 1_200_000_000_000_000
_SEEDED_TASKS = 1_190_000_000_000_000
_SEEDED_STORIES = 1_195_000_000_000_000
STORIES_PER_TASK = 10_000
_SCAN = 1000

DEFAULT_SECTIONS: list[tuple[str, TicketState]] = [
    ("To do", TicketState.OPEN),
    ("Done", TicketState.DONE),
    ("Cancelled", TicketState.CANCELLED),
]


def _derived(lead: str, *parts: str) -> str:
    """A sixteen-digit gid starting with `lead`, the same for the same parts in every run."""
    digest = int(hashlib.sha256("\x1f".join(parts).encode()).hexdigest(), 16)
    return lead + str(digest % 10 ** (_WIDTH - len(lead))).zfill(_WIDTH - len(lead))


WORKSPACE_GID = _derived("10", "workspace")
AGENT_GID = _derived("11", "user", "agent")


def user_gid(person_key: str) -> str:
    return _derived("11", "user", "person", person_key)


def task_gid(position: int) -> str:
    """A seeded task's gid, from its ticket's position among the scenario's asana tickets: below every gid the
    API mints, and in the scenario's order, as tasks made before the run would be."""
    return str(_SEEDED_TASKS + position)


def story_gid(task_position: int, position: int) -> str:
    """A seeded story's gid, from its task's position among the scenario's asana tickets and its own place among
    that task's seeded stories: never from where seeding reached in the log, so a person or ticket added to an open
    world moves none. Every one sits below every gid the API mints, so a task's stories list seeded ones first, in
    the order the scenario gives them, then those written while the world runs, even at the same instant."""
    if position >= STORIES_PER_TASK:
        raise ValueError(f"a seeded asana task holds at most {STORIES_PER_TASK} stories")
    return str(_SEEDED_STORIES + task_position * STORIES_PER_TASK + position)


def project_gid(name: str) -> str:
    return _derived("13", "project", name)


def section_gid(project: str, position: int) -> str:
    """A seeded section's gid: its project's, then its place on the board, so a project's sections list in
    board order, before any the API adds."""
    return f"14{project[-10:]}{position:04d}"


def team_gid(name: str) -> str:
    return _derived("16", "team", name)


def field_gid(name: str) -> str:
    return _derived("17", "field", name)


def option_gid(field: str, name: str) -> str:
    return _derived("17", "option", field, name)


def tag_gid(name: str) -> str:
    return _derived("18", "tag", name)


def credential_gid(token: str) -> str:
    return _derived("19", "credential", token)


def membership_gid(project: str, user: str) -> str:
    return _derived("20", "membership", project, user)


def setting_gid(project: str, field: str) -> str:
    return _derived("21", "setting", project, field)


def _ref(kind: EntityKind, gid: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=kind, external_id=gid)


def record_ref(gid: str) -> EntityRef:
    return _ref(EntityKind.RECORD, gid)


def task_ref(gid: str) -> EntityRef:
    return _ref(EntityKind.TICKET, gid)


def story_ref(gid: str) -> EntityRef:
    return _ref(EntityKind.COMMENT, gid)


class StateUnexpressible(Exception):
    """The workspace's status source has no way to say this state for this task."""


def ticket_state(completed: bool, says: TicketState | None) -> TicketState:
    """What the status source says, with the `completed` box as the floor: a task ticked done is never open."""
    if says is None or says is TicketState.OPEN:
        return TicketState.DONE if completed else TicketState.OPEN
    return says


@dataclass(frozen=True)
class Doer:
    """Who made a change, as an event reports it: the kind of actor, the user's gid (None: no user, as for an event
    Asana makes itself) and the moment."""

    actor: Actor
    by: str | None
    at: datetime


def _named(kind: str, gid: str, name: str | None, subtype: str | None = None) -> wire.EventRef:
    return wire.EventRef(gid=gid, resource_type=kind, resource_subtype=subtype, name=name)


def _task_ref(task: wire.AsanaTask) -> wire.EventRef:
    return _named("task", task.gid, task.name, task.resource_subtype.value)


def _changed(field: str) -> wire.EventChange:
    return wire.EventChange(field=field, action=wire.EventAction.CHANGED)


def _valued(field: str, action: wire.EventAction, value: wire.EventRef) -> wire.EventChange:
    """A field changed by a value that is an Asana resource: it is the `new_value`, `added_value` or `removed_value`
    by the change's own action."""
    match action:
        case wire.EventAction.ADDED:
            return wire.EventChange(field=field, action=action, added_value=value)
        case wire.EventAction.REMOVED:
            return wire.EventChange(field=field, action=action, removed_value=value)
        case _:
            return wire.EventChange(field=field, action=action, new_value=value)


class AsanaWorld:
    """Typed reads and writes of one run's Asana entities."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ reads

    def _pages(self, kind: EntityKind, parent: str) -> Iterator[Stored]:
        after: str | None = None
        while True:
            found = self._store.children(MANIFEST.key, kind, parent, after=after, limit=_SCAN)
            yield from found
            if len(found) < _SCAN:
                return
            after = found[-1].entity.external_id

    def _record(self, gid: str) -> wire.AnyRecord | None:
        stored = self._store.get(record_ref(gid))
        return None if stored is None else wire.parse_record(stored.body)

    def _records(self, parent: str) -> list[wire.AnyRecord]:
        return [wire.parse_record(s.body) for s in self._pages(EntityKind.RECORD, parent)]

    def workspace(self, gid: str) -> wire.AsanaWorkspace | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaWorkspace) else None

    def workspaces(self) -> list[wire.AsanaWorkspace]:
        return [w for w in self._records(WORKSPACES) if isinstance(w, wire.AsanaWorkspace)]

    def home(self) -> wire.AsanaWorkspace:
        """The workspace this provider seeds: the one whose rules (status, plan, tokens) apply."""
        found = self.workspace(WORKSPACE_GID)
        if found is None:
            raise LookupError("the asana workspace was never seeded")
        return found

    def user(self, gid: str) -> wire.AsanaUser | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaUser) else None

    def users(self) -> list[wire.AsanaUser]:
        """The workspace's members: every user but those an administrator removed."""
        return [u for u in self._records(USERS) if isinstance(u, wire.AsanaUser) and not u.removed]

    def user_by_email(self, email: str) -> wire.AsanaUser | None:
        wanted = email.strip().lower()
        return next((u for u in self.users() if u.email.lower() == wanted), None)

    def resolve_user(self, identifier: str, *, me: str) -> wire.AsanaUser | None:
        """A user the way Asana names one: `me` (the caller, whose gid is `me`), an email address, or a gid."""
        if identifier == "me":
            return self.user(me)
        if "@" in identifier:
            return self.user_by_email(identifier)
        return self.user(identifier)

    def team(self, gid: str) -> wire.AsanaTeam | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaTeam) else None

    def teams(self) -> list[wire.AsanaTeam]:
        return [t for t in self._records(TEAMS) if isinstance(t, wire.AsanaTeam)]

    def project(self, gid: str) -> wire.AsanaProject | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaProject) else None

    def projects(self) -> list[wire.AsanaProject]:
        return [p for p in self._records(PROJECTS) if isinstance(p, wire.AsanaProject)]

    def section(self, gid: str) -> wire.AsanaSection | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaSection) else None

    def sections(self, project: str) -> list[wire.AsanaSection]:
        return [s for s in self._records(project) if isinstance(s, wire.AsanaSection)]

    def custom_field(self, gid: str) -> wire.AsanaCustomField | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaCustomField) else None

    def custom_fields(self) -> list[wire.AsanaCustomField]:
        return [f for f in self._records(CUSTOM_FIELDS) if isinstance(f, wire.AsanaCustomField)]

    def tag(self, gid: str) -> wire.AsanaTag | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaTag) else None

    def tags(self) -> list[wire.AsanaTag]:
        return [t for t in self._records(TAGS) if isinstance(t, wire.AsanaTag)]

    def credential(self, token: str) -> wire.AsanaCredential | None:
        found = self._record(credential_gid(token))
        return found if isinstance(found, wire.AsanaCredential) else None

    def task(self, gid: str) -> wire.AsanaTask | None:
        stored = self._store.get(task_ref(gid))
        return None if stored is None else wire.parse(wire.AsanaTask, stored.body)

    def tasks(self) -> list[wire.AsanaTask]:
        """Every live task in the workspace, by gid."""
        found: list[wire.AsanaTask] = []
        for parent in [*(p.gid for p in self.projects()), WORKSPACE_GID]:
            found += [wire.parse(wire.AsanaTask, s.body) for s in self._pages(EntityKind.TICKET, parent)]
        return sorted(found, key=lambda t: t.gid)

    def project_tasks(self, project: str) -> list[wire.AsanaTask]:
        """A project's tasks, including those whose first project is another one."""
        return [t for t in self.tasks() if any(m.project == project for m in t.memberships)]

    def subtasks(self, parent: str) -> list[wire.AsanaTask]:
        return [t for t in self.tasks() if t.parent == parent]

    def story(self, gid: str) -> wire.AsanaStory | None:
        stored = self._store.get(story_ref(gid))
        return None if stored is None else wire.parse(wire.AsanaStory, stored.body)

    def stories(self, task: str) -> list[wire.AsanaStory]:
        return [wire.parse(wire.AsanaStory, s.body) for s in self._pages(EntityKind.COMMENT, task)]

    def attachments(self, task: str) -> list[wire.AsanaAttachment]:
        return [a for a in self._records(task) if isinstance(a, wire.AsanaAttachment)]

    def attachment(self, gid: str) -> wire.AsanaAttachment | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaAttachment) else None

    def webhooks(self) -> list[wire.AsanaWebhook]:
        return [w for w in self._records(WEBHOOKS) if isinstance(w, wire.AsanaWebhook)]

    def webhook(self, gid: str) -> wire.AsanaWebhook | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaWebhook) else None

    def events_after(self, position: str, resource: str | None = None) -> Iterator[wire.AsanaEvent]:
        """Every event with a gid above `position`, in order, that bubbles up to `resource` (any, when None)."""
        after = position
        while True:
            page = self._store.children(MANIFEST.key, EntityKind.RECORD, EVENTS, after=after, limit=_SCAN)
            for stored in page:
                event = wire.parse_record(stored.body)
                if isinstance(event, wire.AsanaEvent) and (resource is None or resource in event.scope):
                    yield event
            if len(page) < _SCAN:
                return
            after = page[-1].entity.external_id

    def head(self) -> int:
        return self._store.head()

    def position(self) -> str:
        """The gid below every event not yet written: a sync token or a webhook made now hears of what follows."""
        return str(_MINTED + self._store.head())

    def sync_token(self, position: str, now: datetime) -> str:
        """An opaque token for `position` made at `now`: 32 hex digits, as Asana's example."""
        return f"{int(position):016x}{int(now.timestamp()):016x}"

    def read_sync(self, token: str, now: datetime) -> str | None:
        """The position a sync token names; None when it is not one this made or has expired."""
        if len(token) != 32:
            return None
        try:
            position, made = int(token[:16], 16), int(token[16:], 16)
        except ValueError:
            return None
        if now - datetime.fromtimestamp(made, UTC) > SYNC_LIFETIME:
            return None
        return str(position)

    def next_gid(self) -> str:
        """The gid of the entity the next event writes: no two events share a sequence number."""
        return str(_MINTED + self._store.head() + 1)

    # ------------------------------------------------------------------ status

    def says(self, task: wire.AsanaTask) -> TicketState | None:
        """What the workspace's status source says of the task, before the `completed` box is read."""
        rule = self.home().status
        match rule:
            case wire.CompletedStatus():
                return None
            case wire.SectionStatus():
                if not task.memberships:
                    return None
                section = self.section(task.memberships[0].section)
                return section.means if section is not None else None
            case wire.FieldStatus():
                value = next((v for v in task.custom_fields if v.field == rule.field), None)
                if value is None or value.option is None or value.option not in rule.means:
                    return None
                return rule.means[value.option]

    def state_of(self, task: wire.AsanaTask) -> TicketState:
        return ticket_state(task.completed, self.says(task))

    def moved(self, task: wire.AsanaTask, to: TicketState, *, now: datetime) -> wire.AsanaTask:
        """The task as a person leaves it after moving it to `to`: the box ticked unless `to` is open, and the
        status source set to say `to` where it can (where it is, else the first section of its first project or
        the first option of the status field that means it), else left where the box alone makes it `to`.
        Raises when nothing the source can hold makes it `to`."""
        at = wire.stamp(now)
        completed = to is not TicketState.OPEN
        ticked = task.model_copy(
            update={
                "completed": completed,
                "completed_at": (task.completed_at if task.completed else at) if completed else None,
                "modified_at": at,
            }
        )
        rule = self.home().status
        candidates = [ticked, *self._placements(ticked, rule)]
        said = next((c for c in candidates if self.says(c) is to), None)
        if said is not None:
            return said
        floor = next((c for c in candidates if self.state_of(c) is to), None)
        if floor is not None:
            return floor
        raise StateUnexpressible(
            f"asana task {task.gid} cannot be made {to}: "
            + {
                "completed": "the workspace's status is the completed box alone, which says open or done",
                "section": "no section of its first project means it",
                "custom_field": "no value of the status field it can carry means it",
            }[rule.kind]
        )

    def _placements(self, task: wire.AsanaTask, rule: wire.StatusRule) -> Iterator[wire.AsanaTask]:
        """Every other place the status source could read the task from, in the order a person would pick."""
        match rule:
            case wire.CompletedStatus():
                return
            case wire.SectionStatus():
                if not task.memberships:
                    return
                first = task.memberships[0]
                for section in self.sections(first.project):
                    moved = first.model_copy(update={"section": section.gid})
                    yield task.model_copy(update={"memberships": [moved, *task.memberships[1:]]})
            case wire.FieldStatus():
                if rule.field not in self.fields_of(task):
                    return
                kept = [v for v in task.custom_fields if v.field != rule.field]
                for option in [*rule.means, None]:
                    value = wire.AsanaFieldValue(field=rule.field, option=option)
                    yield task.model_copy(update={"custom_fields": [*kept, value]})

    def fields_of(self, task: wire.AsanaTask) -> list[str]:
        """The custom fields a task carries: every field settled on any of its projects, in order."""
        found: list[str] = []
        for membership in task.memberships:
            project = self.project(membership.project)
            if project is not None:
                found += [f for f in project.custom_fields if f not in found]
        return found

    def snapshot(self, task: wire.AsanaTask) -> TicketSnapshot:
        assignee = self.user(task.assignee) if task.assignee is not None else None
        first = self.project(task.memberships[0].project) if task.memberships else None
        return TicketSnapshot(
            title=task.name,
            body=task.notes,
            project=first.name if first is not None else None,
            assignee_email=assignee.email if assignee is not None else None,
            state=ticket_state(task.completed, self.says(task)),
        )

    # ------------------------------------------------------------------ writes

    def put_record(
        self,
        record: wire.AnyRecord,
        *,
        parent: str,
        actor: Actor,
        operation: Operation = Operation.CREATE,
        by: str | None = None,
    ) -> WorldEvent:
        """The record's new version; a project, section or tag written by `by` (a user's gid, None for no user) is
        reported to whoever subscribed to it (`events`)."""
        was = self._record(record.gid) if operation is Operation.UPDATE else None
        written = self._store.apply(
            Change(
                entity=record_ref(record.gid),
                operation=operation,
                actor=actor,
                body=wire.dump(record),
                parent=parent,
            )
        )
        self._record_events(was, record, Doer(actor, by, written.sim_time))
        return written

    def delete_record(
        self, record: wire.AsanaSection | wire.AsanaTag | wire.AsanaAttachment, *, parent: str, actor: Actor,
        by: str | None = None,
    ) -> None:  # fmt: skip
        written = self._store.apply(
            Change(entity=record_ref(record.gid), operation=Operation.DELETE, actor=actor, parent=parent)
        )
        doer = Doer(actor, by, written.sim_time)
        match record:
            case wire.AsanaSection():
                self._emit(wire.EventAction.DELETED, _named("section", record.gid, record.name), doer,
                           scope=[record.gid, record.project])  # fmt: skip
            case wire.AsanaTag():
                self._emit(wire.EventAction.DELETED, _named("tag", record.gid, record.name), doer, scope=[record.gid])
            case wire.AsanaAttachment():
                task = self.task(record.task)
                if task is not None:
                    self._emit(wire.EventAction.DELETED, _named("attachment", record.gid, record.name), doer,
                               scope=self.scope(task), parent=_task_ref(task))  # fmt: skip

    def delete_webhook(self, hook: wire.AsanaWebhook, *, actor: Actor) -> None:
        self._store.apply(Change(entity=record_ref(hook.gid), operation=Operation.DELETE, actor=actor, parent=WEBHOOKS))

    def _emit(
        self,
        action: wire.EventAction,
        resource: wire.EventRef,
        doer: Doer,
        *,
        scope: list[str],
        parent: wire.EventRef | None = None,
        change: wire.EventChange | None = None,
    ) -> None:
        """Keep one event, as the log's own record of it: `GET /events` and the webhooks read it from there. Seeding
        (actor SCENARIO) is not an event: the world began as it was."""
        if doer.actor is Actor.SCENARIO:
            return
        event = wire.AsanaEvent(
            gid=self.next_gid(),
            action=action,
            resource=resource,
            user=doer.by,
            parent=parent,
            change=change,
            created_at=wire.stamp(doer.at),
            scope=list(dict.fromkeys(scope)),
        )
        self._store.apply(
            Change(
                entity=record_ref(event.gid),
                operation=Operation.CREATE,
                actor=Actor.SYSTEM,
                body=wire.dump(event),
                parent=EVENTS,
            )
        )

    def scope(self, task: wire.AsanaTask) -> list[str]:
        """The resources whose subscribers hear of a change to the task: itself, its projects, and likewise each
        task above it ("Change events bubble up ... subscribing to a project receives events for tasks within it,
        including modifications to subtasks")."""
        found: list[str] = []
        above: wire.AsanaTask | None = task
        while above is not None:
            found += [above.gid, *(m.project for m in above.memberships)]
            above = self.task(above.parent) if above.parent is not None else None
        return found

    def _record_events(self, was: wire.AnyRecord | None, now: wire.AnyRecord, doer: Doer) -> None:
        match now:
            case wire.AsanaSection():
                where = [now.gid, now.project]
                if was is None:
                    self._emit(wire.EventAction.ADDED, _named("section", now.gid, now.name), doer, scope=where,
                               parent=_named("project", now.project, None))  # fmt: skip
                elif isinstance(was, wire.AsanaSection) and was.name != now.name:
                    self._emit(wire.EventAction.CHANGED, _named("section", now.gid, now.name), doer, scope=where,
                               change=_changed("name"))  # fmt: skip
            case wire.AsanaAttachment() if was is None:
                task = self.task(now.task)
                if task is not None:
                    self._emit(wire.EventAction.ADDED, _named("attachment", now.gid, now.name), doer,
                               scope=self.scope(task), parent=_task_ref(task))  # fmt: skip
            case wire.AsanaTag() if isinstance(was, wire.AsanaTag):
                for field, before, after in (("name", was.name, now.name), ("color", was.color, now.color),
                                             ("notes", was.notes, now.notes)):  # fmt: skip
                    if before != after:
                        self._emit(wire.EventAction.CHANGED, _named("tag", now.gid, now.name), doer, scope=[now.gid],
                                   change=_changed(field))  # fmt: skip
            case wire.AsanaProject() if isinstance(was, wire.AsanaProject):
                for field, before, after in (("name", was.name, now.name), ("notes", was.notes, now.notes),
                                             ("archived", was.archived, now.archived)):  # fmt: skip
                    if before != after:
                        self._emit(wire.EventAction.CHANGED, _named("project", now.gid, now.name), doer,
                                   scope=[now.gid], change=_changed(field))  # fmt: skip
                self._members_events(_named("project", now.gid, now.name), [now.gid], doer, "members",
                                     was.members, now.members, "user")  # fmt: skip
            case _:
                return

    def _members_events(
        self, resource: wire.EventRef, scope: list[str], doer: Doer, field: str, before: list[str], after: list[str],
        kind: str,
    ) -> None:  # fmt: skip
        """A list field changed by what it gained and lost: "When a collaborator is added to the task ... `Event.action`
        will be `changed`, `Event.change.action` will be `added`, and `added_value` will be an object with the
        user's `id` and `type`" (`EventResponse` in the OpenAPI subset)."""
        for action, gids in ((wire.EventAction.ADDED, [g for g in after if g not in before]),
                             (wire.EventAction.REMOVED, [g for g in before if g not in after])):  # fmt: skip
            for gid in gids:
                self._emit(wire.EventAction.CHANGED, resource, doer, scope=scope,
                           change=_valued(field, action, _named(kind, gid, None)))  # fmt: skip

    def put_task(
        self,
        task: wire.AsanaTask,
        *,
        operation: Operation,
        actor: Actor,
        who: str | None = None,
        content: str = "{}",
        by: str | None = None,
    ) -> WorldEvent:
        """The task's new version; when what its status says changed (`words`), the move recorded once as a
        transition, by whoever made it (`who`, a person's key), carrying `content`; and each change reported to
        whoever subscribed (`events`) as made by the user `by` (None: no user)."""
        was = self.task(task.gid) if operation is Operation.UPDATE else None
        parent = task.memberships[0].project if task.memberships else task.workspace
        after: Snapshot = self.snapshot(task)
        written = self._store.apply(
            Change(
                entity=task_ref(task.gid),
                operation=operation,
                actor=actor,
                body=wire.dump(task),
                parent=parent,
                after=after,
            )
        )
        self._task_events(was, task, Doer(actor, by, written.sim_time))
        if was is not None and self.words(was) != self.words(task):
            moved = Transition(
                provider=MANIFEST.key,
                item=task_ref(task.gid),
                name=self.words(task),
                from_state=self.words(was),
                to_state=self.words(task),
                by=actor,
                who=who,
                content=content,
                at=written.sim_time,
            )
            self._store.apply(transition_change(moved, at_seq=self._store.head() + 1))
        return written

    def words(self, task: wire.AsanaTask) -> str:
        """What the workspace's status source shows of the task, in Asana's own words: `completed` or
        `incomplete` for the box alone; the section it is in, or the status field's option, else; a ticked task
        under either is `completed` as well. An approval task says its `approval_status`."""
        if task.approval_status is not None:
            return task.approval_status.value
        rule = self.home().status
        box = "completed" if task.completed else "incomplete"
        match rule:
            case wire.CompletedStatus():
                return box
            case wire.SectionStatus():
                section = self.section(task.memberships[0].section) if task.memberships else None
                where = section.name if section is not None else "(no section)"
            case wire.FieldStatus():
                value = next((v for v in task.custom_fields if v.field == rule.field), None)
                field = self.custom_field(rule.field)
                option = (
                    next((o for o in field.enum_options if o.gid == value.option), None)
                    if field is not None and value is not None and value.option is not None
                    else None
                )
                where = option.name if option is not None else "(none)"
        return f"{where} (completed)" if task.completed else where

    def delete_task(self, task: wire.AsanaTask, *, actor: Actor, by: str | None = None) -> None:
        """Delete the task and, as Asana does, every subtask under it."""
        for child in self.subtasks(task.gid):
            self.delete_task(child, actor=actor, by=by)
        parent = task.memberships[0].project if task.memberships else task.workspace
        written = self._store.apply(
            Change(entity=task_ref(task.gid), operation=Operation.DELETE, actor=actor, parent=parent)
        )
        self._emit(wire.EventAction.DELETED, _task_ref(task), Doer(actor, by, written.sim_time),
                   scope=self.scope(task))  # fmt: skip

    def _task_events(self, was: wire.AsanaTask | None, now: wire.AsanaTask, doer: Doer) -> None:
        """What a write to a task is to those subscribed to it: a task made or put in a project or under a task is
        `added` there, one taken out is `removed`, and a field that differs is `changed` (`EventResponse` in the
        OpenAPI subset), the new value named where it is an Asana resource."""
        here, where = _task_ref(now), self.scope(now)
        if was is None:
            for membership in now.memberships:
                self._emit(wire.EventAction.ADDED, here, doer, scope=where,
                           parent=_named("project", membership.project, None))  # fmt: skip
            if now.parent is not None:
                self._emit(wire.EventAction.ADDED, here, doer, scope=where, parent=_named("task", now.parent, None))
            return
        before = {m.project: m.section for m in was.memberships}
        after = {m.project: m.section for m in now.memberships}
        for project in after:
            if project not in before:
                self._emit(wire.EventAction.ADDED, here, doer, scope=where, parent=_named("project", project, None))
            elif before[project] != after[project]:
                self._emit(wire.EventAction.ADDED, here, doer, scope=where,
                           parent=_named("section", after[project], None))  # fmt: skip
        for project in before:
            if project not in after:
                self._emit(wire.EventAction.REMOVED, here, doer, scope=[*where, project],
                           parent=_named("project", project, None))  # fmt: skip
        scalars: list[tuple[str, object, object]] = [
            ("name", was.name, now.name),
            ("notes", was.notes, now.notes),
            ("completed", was.completed, now.completed),
            ("approval_status", was.approval_status, now.approval_status),
            ("due_on", was.due_on, now.due_on),
            ("due_at", was.due_at, now.due_at),
            ("custom_fields", was.custom_fields, now.custom_fields),
        ]
        for field, old, new in scalars:
            if old != new:
                self._emit(wire.EventAction.CHANGED, here, doer, scope=where, change=_changed(field))
        if was.assignee != now.assignee:
            change = (_valued("assignee", wire.EventAction.CHANGED, _named("user", now.assignee, None))
                      if now.assignee is not None else _changed("assignee"))  # fmt: skip
            self._emit(wire.EventAction.CHANGED, here, doer, scope=where, change=change)
        if was.parent != now.parent:
            change = (_valued("parent", wire.EventAction.CHANGED, _named("task", now.parent, None))
                      if now.parent is not None else _changed("parent"))  # fmt: skip
            self._emit(wire.EventAction.CHANGED, here, doer, scope=where, change=change)
        for field, old_list, new_list, kind in (("followers", was.followers, now.followers, "user"),
                                                ("tags", was.tags, now.tags, "tag"),
                                                ("dependencies", was.dependencies, now.dependencies, "task")):  # fmt: skip
            self._members_events(here, where, doer, field, old_list, new_list, kind)

    def put_story(
        self,
        story: wire.AsanaStory,
        *,
        actor: Actor,
        operation: Operation = Operation.CREATE,
        by: str | None = None,
    ) -> WorldEvent:
        """A comment written or edited, reported to whoever subscribed to its task (`events`): `added` to the task
        when it is written, `changed` in its text when it is edited."""
        written = self._store.apply(
            Change(
                entity=story_ref(story.gid),
                operation=operation,
                actor=actor,
                body=wire.dump(story),
                parent=story.task,
                after=MessageSnapshot(text=story.text, channel=story.task, thread_of=story.task),
            )
        )
        task = self.task(story.task)
        if task is not None:
            here = _named("story", story.gid, None, "comment_added")
            scope = [story.gid, *self.scope(task)]
            doer = Doer(actor, by, written.sim_time)
            if operation is Operation.CREATE:
                self._emit(wire.EventAction.ADDED, here, doer, scope=scope, parent=_task_ref(task))
            else:
                self._emit(wire.EventAction.CHANGED, here, doer, scope=scope, change=_changed("text"))
        return written

    def delete_story(self, story: wire.AsanaStory, *, actor: Actor, by: str | None = None) -> None:
        written = self._store.apply(
            Change(entity=story_ref(story.gid), operation=Operation.DELETE, actor=actor, parent=story.task)
        )
        task = self.task(story.task)
        if task is not None:
            self._emit(wire.EventAction.DELETED, _named("story", story.gid, None, "comment_added"),
                       Doer(actor, by, written.sim_time), scope=[story.gid, *self.scope(task)],
                       parent=_task_ref(task))  # fmt: skip

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))
