"""The Asana workspace as entities in the run's store.

| Asana thing | `EntityKind` | external id | parent |
|---|---|---|---|
| workspace   | RECORD  | workspace gid | `WORKSPACES` |
| user        | RECORD  | user gid      | `USERS` |
| project     | RECORD  | project gid   | `PROJECTS` |
| section     | RECORD  | section gid   | its project's gid |
| task        | TICKET  | task gid      | its first project's gid, or the workspace's when it has none |
| story       | COMMENT | story gid     | its task's gid |

A RECORD is parsed by the `resource_type` it carries, so a user's gid looked up as a
project answers nothing. Gids are digits of one width, so the store's order by
external id is Asana's order by gid. A seeded gid is derived from the scenario's own
key; a gid the API mints is derived from the sequence of the event that writes it.

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
    Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation, Snapshot, Stored, TicketSnapshot, WorldEvent,
)
from minutehand.ports.store import Store

WORKSPACE_NAME = "Simulated Workspace"
AGENT_NAME = "Agent"
AGENT_EMAIL = "agent@workspace.example"

WORKSPACES = "workspaces"
USERS = "users"
PROJECTS = "projects"

_WIDTH = 16
_MINTED = 1_200_000_000_000_000
_SCAN = 1000

SECTIONS: list[tuple[wire.SectionRole, str]] = [
    (wire.SectionRole.TODO, "To do"),
    (wire.SectionRole.DONE, "Done"),
    (wire.SectionRole.CANCELLED, "Cancelled"),
]


def _derived(lead: str, *parts: str) -> str:
    """A sixteen-digit gid starting with `lead`, the same for the same parts in every run."""
    digest = int(hashlib.sha256("\x1f".join(parts).encode()).hexdigest(), 16)
    return lead + str(digest % 10 ** (_WIDTH - len(lead))).zfill(_WIDTH - len(lead))


WORKSPACE_GID = _derived("10", "workspace")
AGENT_GID = _derived("11", "user", "agent")


def user_gid(person_key: str) -> str:
    return _derived("11", "user", "person", person_key)


def project_gid(name: str) -> str:
    return _derived("13", "project", name)


def section_gid(project: str, role: wire.SectionRole) -> str:
    return _derived("14", "section", project, role.value)


def _ref(kind: EntityKind, gid: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=kind, external_id=gid)


def record_ref(gid: str) -> EntityRef:
    return _ref(EntityKind.RECORD, gid)


def task_ref(gid: str) -> EntityRef:
    return _ref(EntityKind.TICKET, gid)


def story_ref(gid: str) -> EntityRef:
    return _ref(EntityKind.COMMENT, gid)


def task_state(task: wire.AsanaTask, sections: list[wire.AsanaSection]) -> TicketState:
    """Open until completed; completed in a Cancelled section is cancelled, anywhere else done."""
    if not task.completed:
        return TicketState.OPEN
    if any(s.role is wire.SectionRole.CANCELLED for s in sections):
        return TicketState.CANCELLED
    return TicketState.DONE


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

    def _record(self, gid: str) -> wire.AsanaWorkspace | wire.AsanaUser | wire.AsanaProject | wire.AsanaSection | None:
        stored = self._store.get(record_ref(gid))
        return None if stored is None else wire.parse_record(stored.body)

    def workspace(self, gid: str) -> wire.AsanaWorkspace | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaWorkspace) else None

    def workspaces(self) -> list[wire.AsanaWorkspace]:
        found = [wire.parse_record(s.body) for s in self._pages(EntityKind.RECORD, WORKSPACES)]
        return [w for w in found if isinstance(w, wire.AsanaWorkspace)]

    def user(self, gid: str) -> wire.AsanaUser | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaUser) else None

    def users(self) -> list[wire.AsanaUser]:
        found = [wire.parse_record(s.body) for s in self._pages(EntityKind.RECORD, USERS)]
        return [u for u in found if isinstance(u, wire.AsanaUser)]

    def user_by_email(self, email: str) -> wire.AsanaUser | None:
        wanted = email.strip().lower()
        return next((u for u in self.users() if u.email.lower() == wanted), None)

    def resolve_user(self, identifier: str) -> wire.AsanaUser | None:
        """A user the way Asana names one: `me`, an email address, or a gid."""
        if identifier == "me":
            return self.user(AGENT_GID)
        if "@" in identifier:
            return self.user_by_email(identifier)
        return self.user(identifier)

    def project(self, gid: str) -> wire.AsanaProject | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaProject) else None

    def projects(self) -> list[wire.AsanaProject]:
        found = [wire.parse_record(s.body) for s in self._pages(EntityKind.RECORD, PROJECTS)]
        return [p for p in found if isinstance(p, wire.AsanaProject)]

    def section(self, gid: str) -> wire.AsanaSection | None:
        found = self._record(gid)
        return found if isinstance(found, wire.AsanaSection) else None

    def sections(self, project: str) -> list[wire.AsanaSection]:
        found = [wire.parse_record(s.body) for s in self._pages(EntityKind.RECORD, project)]
        return [s for s in found if isinstance(s, wire.AsanaSection)]

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

    def story(self, gid: str) -> wire.AsanaStory | None:
        stored = self._store.get(story_ref(gid))
        return None if stored is None else wire.parse(wire.AsanaStory, stored.body)

    def stories(self, task: str) -> list[wire.AsanaStory]:
        return [wire.parse(wire.AsanaStory, s.body) for s in self._pages(EntityKind.COMMENT, task)]

    def next_gid(self) -> str:
        """The gid of the entity the next event writes: no two events share a sequence number."""
        return str(_MINTED + self._store.head() + 1)

    def snapshot(self, task: wire.AsanaTask) -> TicketSnapshot:
        assignee = self.user(task.assignee) if task.assignee is not None else None
        first = self.project(task.memberships[0].project) if task.memberships else None
        sections = [s for s in (self.section(m.section) for m in task.memberships) if s is not None]
        return TicketSnapshot(
            title=task.name, body=task.notes, project=first.name if first is not None else None,
            assignee_email=assignee.email if assignee is not None else None, state=task_state(task, sections),
        )

    # ------------------------------------------------------------------ writes

    def put_record(
        self, record: wire.AsanaWorkspace | wire.AsanaUser | wire.AsanaProject | wire.AsanaSection, *,
        parent: str, actor: Actor,
    ) -> WorldEvent:
        return self._store.apply(Change(
            entity=record_ref(record.gid), operation=Operation.CREATE, actor=actor, body=wire.dump(record),
            parent=parent,
        ))

    def put_task(self, task: wire.AsanaTask, *, operation: Operation, actor: Actor) -> WorldEvent:
        parent = task.memberships[0].project if task.memberships else task.workspace
        after: Snapshot = self.snapshot(task)
        return self._store.apply(Change(
            entity=task_ref(task.gid), operation=operation, actor=actor, body=wire.dump(task), parent=parent,
            after=after,
        ))

    def delete_task(self, task: wire.AsanaTask, *, actor: Actor) -> WorldEvent:
        parent = task.memberships[0].project if task.memberships else task.workspace
        return self._store.apply(Change(entity=task_ref(task.gid), operation=Operation.DELETE, actor=actor,
                                        parent=parent))

    def put_story(self, story: wire.AsanaStory, *, actor: Actor) -> WorldEvent:
        """A comment reaches nobody: Asana pushes nothing to the agent, so no person is asked by one."""
        return self._store.apply(Change(
            entity=story_ref(story.gid), operation=Operation.CREATE, actor=actor, body=wire.dump(story),
            parent=story.task, after=MessageSnapshot(text=story.text, channel=story.task, thread_of=story.task),
        ))

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))

    def moved(self, task: wire.AsanaTask, to: TicketState, *, now: datetime) -> wire.AsanaTask:
        """The task as a person leaves it after moving it to `to`: ticked or not, and in the matching section."""
        role = {TicketState.OPEN: wire.SectionRole.TODO, TicketState.DONE: wire.SectionRole.DONE,
                TicketState.CANCELLED: wire.SectionRole.CANCELLED}[to]
        completed = to is not TicketState.OPEN
        at = wire.stamp(now)
        return task.model_copy(update={
            "completed": completed,
            "completed_at": (task.completed_at if task.completed else at) if completed else None,
            "memberships": [wire.AsanaMembership(project=m.project, section=section_gid(m.project, role))
                            for m in task.memberships],
            "modified_at": at,
        })
