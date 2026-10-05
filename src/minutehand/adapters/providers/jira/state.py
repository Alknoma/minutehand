"""The Jira site as entities in the run's store.

| Jira thing | `EntityKind` | external id | parent |
|---|---|---|---|
| the site (its statuses, types, fields, …) | RECORD | `site`            | `site` |
| user                                      | RECORD | `user:<accountId>` | `users` |
| credential                                | RECORD | `credential:<n>`   | `credentials` |
| project                                   | RECORD | `project:<id>`     | `projects` |
| board                                     | RECORD | `board:<id>`       | `boards` |
| sprint                                    | RECORD | `sprint:<id>`      | `sprints` |
| issue key (`ENG-12`)                      | RECORD | `key:<KEY>`        | the project's id |
| issue link                                | RECORD | `link:<id>`        | `links` |
| a rate limit's spent calls                | RECORD | `fault:<n>`        | `faults` |
| issue                                     | TICKET | its id             | the project's id |
| comment                                   | COMMENT | its id            | the issue's id |

An id Jira hands out (an issue's, a comment's, a project's) is `10000` plus the sequence of the event that
writes the thing, so ids are deterministic, numeric strings as Jira's are, and never repeat within what a run
can see. A key's record is never deleted, so a project never hands a number out twice. An issue's changelog
is carried on the issue itself, one history entry per change.

Nothing here is held between calls: every read is a query of the store.
"""

from __future__ import annotations

from collections.abc import Iterator

from minutehand.adapters.providers.jira import wire
from minutehand.adapters.providers.jira.manifest import MANIFEST
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    MessageSnapshot,
    Operation,
    Stored,
    TicketSnapshot,
    WorldEvent,
)
from minutehand.ports.store import Store

SITE = "site"
USERS = "users"
CREDENTIALS = "credentials"
PROJECTS = "projects"
BOARDS = "boards"
SPRINTS = "sprints"
LINKS = "links"
FAULTS = "faults"

_SCAN = 1000


def _ref(kind: EntityKind, external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=kind, external_id=external_id)


def site_ref() -> EntityRef:
    return _ref(EntityKind.RECORD, SITE)


def user_ref(account: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"user:{account}")


def credential_ref(credential: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"credential:{credential}")


def project_ref(project: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"project:{project}")


def board_ref(board: int) -> EntityRef:
    return _ref(EntityKind.RECORD, f"board:{board}")


def sprint_ref(sprint: int) -> EntityRef:
    return _ref(EntityKind.RECORD, f"sprint:{sprint}")


def key_ref(key: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"key:{key.upper()}")


def link_ref(link: str) -> EntityRef:
    return _ref(EntityKind.RECORD, f"link:{link}")


def fault_ref(index: int) -> EntityRef:
    return _ref(EntityKind.RECORD, f"fault:{index}")


def issue_ref(issue: str) -> EntityRef:
    return _ref(EntityKind.TICKET, issue)


def comment_ref(comment: str) -> EntityRef:
    return _ref(EntityKind.COMMENT, comment)


class JiraWorld:
    """Typed reads and writes of one run's Jira site."""

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

    def seeded(self) -> bool:
        return self._store.get(site_ref()) is not None

    def site(self) -> wire.StoredSite:
        stored = self._store.get(site_ref())
        if stored is None:
            raise LookupError("the Jira site was never seeded")
        return wire.parse(wire.StoredSite, stored.body)

    def users(self) -> list[wire.StoredUser]:
        found = [wire.parse(wire.StoredUser, s.body) for s in self._all(EntityKind.RECORD, USERS)]
        return sorted(found, key=lambda u: u.displayName.lower())

    def user(self, account: str) -> wire.StoredUser | None:
        stored = self._store.get(user_ref(account))
        return None if stored is None else wire.parse(wire.StoredUser, stored.body)

    def user_by_email(self, email: str) -> wire.StoredUser | None:
        wanted = email.strip().lower()
        return next((u for u in self.users() if u.emailAddress is not None and u.emailAddress.lower() == wanted), None)

    def credentials(self) -> list[wire.StoredCredential]:
        return [wire.parse(wire.StoredCredential, s.body) for s in self._all(EntityKind.RECORD, CREDENTIALS)]

    def projects(self) -> list[wire.StoredProject]:
        found = [wire.parse(wire.StoredProject, s.body) for s in self._all(EntityKind.RECORD, PROJECTS)]
        return sorted(found, key=lambda p: int(p.id))

    def project(self, project: str) -> wire.StoredProject | None:
        stored = self._store.get(project_ref(project))
        return None if stored is None else wire.parse(wire.StoredProject, stored.body)

    def find_project(self, reference: str) -> wire.StoredProject | None:
        """A project by its id or its key, as a path or a body names one."""
        by_id = self.project(reference)
        if by_id is not None:
            return by_id
        wanted = reference.strip().upper()
        return next((p for p in self.projects() if p.key == wanted), None)

    def boards(self) -> list[wire.StoredBoard]:
        found = [wire.parse(wire.StoredBoard, s.body) for s in self._all(EntityKind.RECORD, BOARDS)]
        return sorted(found, key=lambda b: b.id)

    def board(self, board: int) -> wire.StoredBoard | None:
        stored = self._store.get(board_ref(board))
        return None if stored is None else wire.parse(wire.StoredBoard, stored.body)

    def sprints(self) -> list[wire.StoredSprint]:
        found = [wire.parse(wire.StoredSprint, s.body) for s in self._all(EntityKind.RECORD, SPRINTS)]
        return sorted(found, key=lambda s: s.id)

    def sprint(self, sprint: int) -> wire.StoredSprint | None:
        stored = self._store.get(sprint_ref(sprint))
        return None if stored is None else wire.parse(wire.StoredSprint, stored.body)

    def issue(self, issue: str) -> wire.StoredIssue | None:
        stored = self._store.get(issue_ref(issue))
        return None if stored is None else wire.parse(wire.StoredIssue, stored.body)

    def find_issue(self, reference: str) -> wire.StoredIssue | None:
        """An issue by its id (`10023`) or its key (`ENG-12`, any case)."""
        if reference.isdigit():
            return self.issue(reference)
        alias = self._store.get(key_ref(reference))
        if alias is None:
            return None
        return self.issue(wire.parse(wire.StoredAlias, alias.body).issue)

    def issues(self, project: str) -> list[wire.StoredIssue]:
        found = [wire.parse(wire.StoredIssue, s.body) for s in self._all(EntityKind.TICKET, project)]
        return sorted(found, key=lambda i: int(i.id))

    def every_issue(self) -> list[wire.StoredIssue]:
        return [issue for project in self.projects() for issue in self.issues(project.id)]

    def subtasks(self, issue: wire.StoredIssue) -> list[wire.StoredIssue]:
        return [i for i in self.issues(issue.project) if i.parent == issue.id] + [
            i for p in self.projects() if p.id != issue.project for i in self.issues(p.id) if i.parent == issue.id
        ]

    def comments(self, issue: str) -> list[wire.StoredComment]:
        found = [wire.parse(wire.StoredComment, s.body) for s in self._all(EntityKind.COMMENT, issue)]
        return sorted(found, key=lambda c: int(c.id))

    def links(self) -> list[wire.StoredLink]:
        found = [wire.parse(wire.StoredLink, s.body) for s in self._all(EntityKind.RECORD, LINKS)]
        return sorted(found, key=lambda link: int(link.id))

    def link(self, link: str) -> wire.StoredLink | None:
        stored = self._store.get(link_ref(link))
        return None if stored is None else wire.parse(wire.StoredLink, stored.body)

    def fault_use(self, index: int) -> int:
        stored = self._store.get(fault_ref(index))
        return 0 if stored is None else wire.parse(wire.StoredFaultUse, stored.body).used

    def next_number(self, project: str) -> int:
        """The number the project's next issue takes: one past every key it has handed out."""
        return sum(1 for _ in self._all(EntityKind.RECORD, project)) + 1

    def next_id(self) -> str:
        """An id from the sequence of the event about to be written."""
        return str(10000 + self._store.head() + 1)

    def snapshot(self, issue: wire.StoredIssue) -> TicketSnapshot:
        """The issue read across every provider: its summary, its description as text, its project's key, its
        assignee's email and what its status means."""
        project = self.project(issue.project)
        if project is None:
            raise LookupError(f"{issue.key} names project {issue.project}, which does not exist")
        assignee = self.user(issue.assignee) if issue.assignee is not None else None
        return TicketSnapshot(
            title=issue.summary,
            body=wire.adf_text(issue.description),
            project=project.key,
            assignee_email=assignee.emailAddress if assignee is not None else None,
            state=self.site().status(issue.status).outcome,
        )

    # ------------------------------------------------------------------ writes

    def _write(self, ref: EntityRef, body: wire.Wire, parent: str, *, actor: Actor, create: bool) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=ref,
                operation=Operation.CREATE if create else Operation.UPDATE,
                actor=actor,
                body=wire.dump(body),
                parent=parent,
            )
        )

    def write_site(self, site: wire.StoredSite, *, actor: Actor) -> WorldEvent:
        return self._write(site_ref(), site, SITE, actor=actor, create=not self.seeded())

    def write_user(self, user: wire.StoredUser, *, actor: Actor) -> WorldEvent:
        create = self.user(user.accountId) is None
        return self._write(user_ref(user.accountId), user, USERS, actor=actor, create=create)

    def write_credential(self, credential: wire.StoredCredential, *, actor: Actor, create: bool) -> WorldEvent:
        return self._write(credential_ref(credential.id), credential, CREDENTIALS, actor=actor, create=create)

    def write_project(self, project: wire.StoredProject, *, actor: Actor) -> WorldEvent:
        create = self.project(project.id) is None
        return self._write(project_ref(project.id), project, PROJECTS, actor=actor, create=create)

    def write_board(self, board: wire.StoredBoard, *, actor: Actor) -> WorldEvent:
        return self._write(board_ref(board.id), board, BOARDS, actor=actor, create=True)

    def write_sprint(self, sprint: wire.StoredSprint, *, actor: Actor) -> WorldEvent:
        return self._write(sprint_ref(sprint.id), sprint, SPRINTS, actor=actor, create=True)

    def spend_fault(self, index: int, used: int) -> WorldEvent:
        return self._write(
            fault_ref(index), wire.StoredFaultUse(used=used), FAULTS, actor=Actor.AGENT, create=used == 1
        )

    def create_issue(self, issue: wire.StoredIssue, *, actor: Actor) -> WorldEvent:
        """Its key, then the issue: a fork between the two has spent the number and holds no issue."""
        after = self.snapshot(issue)
        self._store.apply(
            Change(
                entity=key_ref(issue.key),
                operation=Operation.CREATE,
                actor=actor,
                body=wire.dump(wire.StoredAlias(issue=issue.id)),
                parent=issue.project,
            )
        )
        return self._store.apply(
            Change(
                entity=issue_ref(issue.id),
                operation=Operation.CREATE,
                actor=actor,
                body=wire.dump(issue),
                parent=issue.project,
                after=after,
            )
        )

    def update_issue(self, issue: wire.StoredIssue, *, actor: Actor) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=issue_ref(issue.id),
                operation=Operation.UPDATE,
                actor=actor,
                body=wire.dump(issue),
                parent=issue.project,
                after=self.snapshot(issue),
            )
        )

    def delete_issue(self, issue: wire.StoredIssue, *, actor: Actor) -> WorldEvent:
        return self._store.apply(
            Change(entity=issue_ref(issue.id), operation=Operation.DELETE, actor=actor, parent=issue.project)
        )

    def write_comment(self, comment: wire.StoredComment, *, actor: Actor) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=comment_ref(comment.id),
                operation=Operation.CREATE,
                actor=actor,
                body=wire.dump(comment),
                parent=comment.issue,
                after=MessageSnapshot(text=wire.adf_text(comment.body), channel=comment.issue, thread_of=comment.issue),
            )
        )

    def write_link(self, link: wire.StoredLink, *, actor: Actor) -> WorldEvent:
        return self._write(link_ref(link.id), link, LINKS, actor=actor, create=True)

    def delete_link(self, link: wire.StoredLink, *, actor: Actor) -> WorldEvent:
        return self._store.apply(
            Change(entity=link_ref(link.id), operation=Operation.DELETE, actor=actor, parent=LINKS)
        )

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))
