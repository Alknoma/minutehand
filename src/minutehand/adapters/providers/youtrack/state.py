"""The YouTrack instance as entities in the run's store.

| YouTrack thing | `EntityKind` | external id | parent |
|---|---|---|---|
| user                    | RECORD  | database id, `1-<n>`        | `users` |
| project                 | RECORD  | database id, `0-<n>`        | `projects` |
| issue                   | TICKET  | database id, `2-<seq>`      | the project's id |
| readable id (`DEMO-12`) | RECORD  | the readable id             | the project's id |
| comment                 | COMMENT | database id, `4-<seq>`      | the issue's id |

An issue's and a comment's number is the sequence of the first event written for
it, so ids are deterministic and never repeat within what a run can see. A readable
id is never deleted, so a project never hands out a number twice.

Nothing here is held between calls: every read is a query of the store, so a new
app over the same store sees the same instance, and a fork sees it as of the fork.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from minutehand.adapters.providers.youtrack import wire
from minutehand.adapters.providers.youtrack.manifest import MANIFEST
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, Stored, TicketSnapshot, WorldEvent
from minutehand.ports.store import Store

USERS = "users"
PROJECTS = "projects"
INSTANCE = "instance"
"""What a search across the whole instance reads."""

AGENT_LOGIN = "agent-bot"
"""The agent's own account. A hyphen, so no `Person.key` can ever derive the same login."""
AGENT_NAME = "Agent"
AGENT_EMAIL = "agent-bot@youtrack.invalid"

_SCAN = 1000


def millis(at: datetime) -> int:
    """A moment as YouTrack writes one: epoch milliseconds."""
    return int(at.timestamp() * 1000)


def _ref(kind: EntityKind, external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=kind, external_id=external_id)


def user_ref(user: str) -> EntityRef:
    return _ref(EntityKind.RECORD, user)


def project_ref(project: str) -> EntityRef:
    return _ref(EntityKind.RECORD, project)


def instance_ref() -> EntityRef:
    return _ref(EntityKind.RECORD, INSTANCE)


def issue_ref(issue: str) -> EntityRef:
    return _ref(EntityKind.TICKET, issue)


def alias_ref(readable: str) -> EntityRef:
    return _ref(EntityKind.RECORD, readable)


def comment_ref(comment: str) -> EntityRef:
    return _ref(EntityKind.COMMENT, comment)


def _short_name_of(readable: str) -> tuple[str, int] | None:
    """`DEMO-12` as its project's short name and its number, or None when it is not that shape."""
    short, dash, number = readable.rpartition("-")
    if not dash or not short or not number.isdigit():
        return None
    return short, int(number)


class YouTrackWorld:
    """Typed reads and writes of one run's YouTrack entities."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ reads

    def _all(self, kind: EntityKind, parent: str) -> Iterator[Stored]:
        after: str | None = None
        while True:
            page = self._store.children(MANIFEST.key, kind, parent, after=after, limit=_SCAN)
            yield from page
            if len(page) < _SCAN:
                return
            after = page[-1].entity.external_id

    def users(self) -> list[wire.StoredUser]:
        found = [wire.parse(wire.StoredUser, s.body) for s in self._all(EntityKind.RECORD, USERS)]
        return sorted(found, key=lambda u: _ordinal(u.id))

    def user(self, user: str) -> wire.StoredUser | None:
        stored = self._store.get(user_ref(user))
        if stored is None or stored.parent != USERS:
            return None
        return wire.parse(wire.StoredUser, stored.body)

    def user_by_login(self, login: str) -> wire.StoredUser | None:
        wanted = login.strip().lower()
        return next((u for u in self.users() if u.login.lower() == wanted), None)

    def user_by_email(self, email: str) -> wire.StoredUser | None:
        wanted = email.strip().lower()
        return next((u for u in self.users() if u.email is not None and u.email.lower() == wanted), None)

    def me(self) -> wire.StoredUser:
        found = self.user_by_login(AGENT_LOGIN)
        if found is None:
            raise LookupError("the instance has no agent account: it was never seeded")
        return found

    def projects(self) -> list[wire.StoredProject]:
        found = [wire.parse(wire.StoredProject, s.body) for s in self._all(EntityKind.RECORD, PROJECTS)]
        return sorted(found, key=lambda p: _ordinal(p.id))

    def project(self, project: str) -> wire.StoredProject | None:
        stored = self._store.get(project_ref(project))
        if stored is None or stored.parent != PROJECTS:
            return None
        return wire.parse(wire.StoredProject, stored.body)

    def project_named(self, name: str) -> wire.StoredProject | None:
        """A project by its short name or its name, as a query or a path names one."""
        wanted = name.strip().lower()
        return next(
            (p for p in self.projects() if p.shortName.lower() == wanted or p.name.lower() == wanted), None
        )

    def issue(self, issue: str) -> wire.StoredIssue | None:
        stored = self._store.get(issue_ref(issue))
        return None if stored is None else wire.parse(wire.StoredIssue, stored.body)

    def find_issue(self, reference: str) -> wire.StoredIssue | None:
        """An issue by its database id (`2-17`) or its readable id (`DEMO-12`)."""
        by_id = self.issue(reference)
        if by_id is not None:
            return by_id
        shape = _short_name_of(reference)
        if shape is None:
            return None
        project = next((p for p in self.projects() if p.shortName.lower() == shape[0].lower()), None)
        if project is None:
            return None
        alias = self._store.get(alias_ref(f"{project.shortName}-{shape[1]}"))
        if alias is None or alias.parent != project.id:
            return None
        return self.issue(wire.parse(wire.StoredAlias, alias.body).issue)

    def issues(self, project: str) -> list[wire.StoredIssue]:
        found = [wire.parse(wire.StoredIssue, s.body) for s in self._all(EntityKind.TICKET, project)]
        return sorted(found, key=lambda i: i.numberInProject)

    def every_issue(self) -> list[wire.StoredIssue]:
        return [issue for project in self.projects() for issue in self.issues(project.id)]

    def comments(self, issue: str) -> list[wire.StoredComment]:
        found = [wire.parse(wire.StoredComment, s.body) for s in self._all(EntityKind.COMMENT, issue)]
        return sorted(found, key=lambda c: _ordinal(c.id))

    def next_number(self, project: str) -> int:
        """The number the project's next issue takes: one past every number it has handed out."""
        return sum(1 for _ in self._all(EntityKind.RECORD, project)) + 1

    def next_id(self, prefix: int) -> str:
        """A database id from the sequence of the event about to be written."""
        return f"{prefix}-{self._store.head() + 1}"

    def state_of(self, project: wire.StoredProject, issue: wire.StoredIssue) -> wire.StoredState:
        found = next((s for s in project.states if s.id == issue.state), None)
        if found is None:
            raise LookupError(f"{issue.idReadable} is in state {issue.state}, which {project.shortName} does not have")
        return found

    def snapshot(self, issue: wire.StoredIssue) -> TicketSnapshot:
        project = self.project(issue.project)
        if project is None:
            raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
        assignee = self.user(issue.assignee) if issue.assignee is not None else None
        return TicketSnapshot(
            title=issue.summary, body=issue.description or "", project=project.shortName,
            assignee_email=assignee.email if assignee is not None else None,
            state=self.state_of(project, issue).outcome,
        )

    # ------------------------------------------------------------------ writes

    def write_user(self, user: wire.StoredUser, *, actor: Actor) -> WorldEvent:
        return self._store.apply(Change(
            entity=user_ref(user.id), operation=Operation.CREATE, actor=actor, body=wire.dump(user), parent=USERS,
        ))

    def write_project(self, project: wire.StoredProject, *, actor: Actor) -> WorldEvent:
        return self._store.apply(Change(
            entity=project_ref(project.id), operation=Operation.CREATE, actor=actor, body=wire.dump(project),
            parent=PROJECTS,
        ))

    def create_issue(self, issue: wire.StoredIssue, *, actor: Actor) -> WorldEvent:
        """Its readable id, then the issue: a fork between the two has spent the number and holds no issue."""
        after = self.snapshot(issue)
        self._store.apply(Change(
            entity=alias_ref(issue.idReadable), operation=Operation.CREATE, actor=actor,
            body=wire.dump(wire.StoredAlias(issue=issue.id)), parent=issue.project,
        ))
        return self._store.apply(Change(
            entity=issue_ref(issue.id), operation=Operation.CREATE, actor=actor, body=wire.dump(issue),
            parent=issue.project, after=after,
        ))

    def update_issue(self, issue: wire.StoredIssue, *, actor: Actor) -> WorldEvent:
        return self._store.apply(Change(
            entity=issue_ref(issue.id), operation=Operation.UPDATE, actor=actor, body=wire.dump(issue),
            parent=issue.project, after=self.snapshot(issue),
        ))

    def delete_issue(self, issue: wire.StoredIssue, *, actor: Actor) -> WorldEvent:
        return self._store.apply(Change(
            entity=issue_ref(issue.id), operation=Operation.DELETE, actor=actor, parent=issue.project,
        ))

    def write_comment(self, comment: wire.StoredComment, *, actor: Actor) -> WorldEvent:
        return self._store.apply(Change(
            entity=comment_ref(comment.id), operation=Operation.CREATE, actor=actor, body=wire.dump(comment),
            parent=comment.issue,
        ))

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))

    # ------------------------------------------------------------------ moves

    def moved(
        self, issue: wire.StoredIssue, project: wire.StoredProject, to: wire.StoredState, *, by: str, at: int,
    ) -> wire.StoredIssue:
        """The issue in state `to`: `resolved` is set when it enters a resolved state and cleared when it leaves one."""
        was = self.state_of(project, issue)
        resolved = issue.resolved
        if to.isResolved and not was.isResolved:
            resolved = at
        elif not to.isResolved:
            resolved = None
        return issue.model_copy(update={"state": to.id, "resolved": resolved, "updated": at, "updater": by})

    def state_for(self, project: wire.StoredProject, outcome: TicketState) -> wire.StoredState:
        """The first value of the project's State field that means `outcome`."""
        found = next((s for s in project.states if s.outcome is outcome), None)
        if found is None:
            raise LookupError(f"project {project.shortName} has no State value that is {outcome.value}")
        return found


def _ordinal(entity_id: str) -> tuple[int, int]:
    """A database id (`2-17`) in YouTrack's order: by its type, then its number."""
    kind, _, number = entity_id.partition("-")
    return int(kind), int(number)
