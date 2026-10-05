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
from datetime import datetime

from minutehand.adapters.providers.asana import wire
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.domain.scenario import TicketState
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

_WIDTH = 16
_MINTED = 1_200_000_000_000_000
_SEEDED_TASKS = 1_190_000_000_000_000
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


def project_gid(name: str) -> str:
    return _derived("13", "project", name)


def section_gid(project: str, name: str) -> str:
    return _derived("14", "section", project, name)


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
        return [u for u in self._records(USERS) if isinstance(u, wire.AsanaUser)]

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
    ) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=record_ref(record.gid),
                operation=operation,
                actor=actor,
                body=wire.dump(record),
                parent=parent,
            )
        )

    def put_task(self, task: wire.AsanaTask, *, operation: Operation, actor: Actor) -> WorldEvent:
        parent = task.memberships[0].project if task.memberships else task.workspace
        after: Snapshot = self.snapshot(task)
        return self._store.apply(
            Change(
                entity=task_ref(task.gid),
                operation=operation,
                actor=actor,
                body=wire.dump(task),
                parent=parent,
                after=after,
            )
        )

    def delete_task(self, task: wire.AsanaTask, *, actor: Actor) -> None:
        """Delete the task and, as Asana does, every subtask under it."""
        for child in self.subtasks(task.gid):
            self.delete_task(child, actor=actor)
        parent = task.memberships[0].project if task.memberships else task.workspace
        self._store.apply(Change(entity=task_ref(task.gid), operation=Operation.DELETE, actor=actor, parent=parent))

    def put_story(self, story: wire.AsanaStory, *, actor: Actor) -> WorldEvent:
        """A comment reaches nobody: Asana pushes nothing to the agent, so no person is asked by one."""
        return self._store.apply(
            Change(
                entity=story_ref(story.gid),
                operation=Operation.CREATE,
                actor=actor,
                body=wire.dump(story),
                parent=story.task,
                after=MessageSnapshot(text=story.text, channel=story.task, thread_of=story.task),
            )
        )

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))
