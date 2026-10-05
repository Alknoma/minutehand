"""The YouTrack instance as entities in the run's store.

| YouTrack thing | `EntityKind` | external id | parent |
|---|---|---|---|
| user                         | RECORD  | database id, `1-<n>`              | `users` |
| custom field of the instance | RECORD  | database id, `58-<n>`             | `customFields` |
| project, with its fields     | RECORD  | database id, `0-<n>`              | `projects` |
| issue                        | TICKET  | database id, `2-<n>`              | the project's id |
| readable id (`DEMO-12`)      | RECORD  | the readable id                   | the project's id |
| comment                      | COMMENT | database id, `4-<n>`              | the issue's id |
| tag                          | RECORD  | database id, `6-<n>`              | `tags` |
| link type                    | RECORD  | database id, `106-<n>`            | `linkTypes` |
| link                         | RECORD  | `link:<source>:<type>:<target>`   | `links` |
| token                        | RECORD  | `token:<sha256 of the token>`     | `tokens` |
| Hub service                  | RECORD  | `service:<client id>`             | `services` |
| grant                        | RECORD  | `grant:<n>`                       | `grants` |
| fault                        | RECORD  | `fault:<n>`                       | `faults` |
| the instance's settings      | RECORD  | `settings`                        | `instance` |

An issue, a comment or a tag the agent or a person makes is numbered by the sequence of the first event written for
it, so ids are deterministic and never repeat within what a run can see. One the scenario seeds is numbered by what
it is (`seeded_id`: its ticket's position, its title, its index on its issue, its name), in a range of its own above
every sequence a run reaches, so seeding the same thing later in the log, or after an addition, gives it the same id;
such a thing carries `seededFrom`, and it is listed before anything made in the run, in the order it was seeded.
A user the YouTrack seed adds is numbered from `EXTRA_USERS` on, so a person added to the scenario moves none, and a
project the YouTrack seed describes from `DESCRIBED_PROJECTS` on, its fields from a range of its own, so a project
only seeded tickets name (numbered from 0, in the order the tickets name them) moves none, and is moved by none. A readable id is never deleted, so a project never hands
out a number twice. A link is deleted when it is removed, and is read from both of its ends.

Nothing here is held between calls: every read is a query of the store, so a new app over the same store sees the
same instance, and a fork sees it as of the fork.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime

from minutehand.adapters.providers.youtrack import wire
from minutehand.adapters.providers.youtrack.manifest import MANIFEST
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, Stored, TicketSnapshot, WorldEvent
from minutehand.ports.store import Store

USERS = "users"
DEFINITIONS = "customFields"
PROJECTS = "projects"
TAGS = "tags"
LINK_TYPES = "linkTypes"
LINKS = "links"
TOKENS = "tokens"
SERVICES = "services"
GRANTS = "grants"
FAULTS = "faults"
INSTANCE = "instance"
"""What a search across the whole instance reads, and the parent of its settings."""
SETTINGS = "settings"

AGENT_LOGIN = "agent-bot"
"""The agent's own account. A hyphen, so no `Person.key` can ever derive the same login."""
AGENT_NAME = "Agent"
AGENT_EMAIL = "agent-bot@youtrack.invalid"

STATE_FIELD = "State"
ASSIGNEE_FIELD = "Assignee"

_SCAN = 1000

SEEDED = 100_000_000
"""Where a seeded issue's, comment's or tag's number starts: above every sequence number a run reaches."""
_SEEDED_SPAN = 900_000_000
EXTRA_USERS = 10_000
"""The number of the first user the YouTrack seed adds beyond the agent and the scenario's people."""
DESCRIBED_PROJECTS = 1_000
"""The number of the first project the YouTrack seed describes; a project only seeded tickets name counts from 0."""
DESCRIBED_FIELDS = 1_000_000
FIELDS_PER_PROJECT = 10_000
"""A described project's fields, bundles and values are numbered from `DESCRIBED_FIELDS` plus this per project."""


def seeded_id(prefix: int, *identity: str, taken: Callable[[str], bool]) -> str:
    """A seeded thing's database id from what it is: the same `identity` gives the same id wherever in the log it is
    seeded. `taken` says whether an id is in use; a clash with another seeded thing moves on to the next number."""
    digest = int(hashlib.sha256("\x1f".join((str(prefix), *identity)).encode()).hexdigest()[:12], 16)
    number = digest % _SEEDED_SPAN
    while taken(f"{prefix}-{SEEDED + number}"):
        number = (number + 1) % _SEEDED_SPAN
    return f"{prefix}-{SEEDED + number}"


def issue_rank(issue: wire.StoredIssue) -> float:
    """`seeded_order` as one number, for a sort by issue id: seeded issues first, in the order they were seeded."""
    order = seeded_order(issue.seededFrom, issue.id)
    return float(order[1]) if order[0] == 0 else float(_RUN_RANK + order[2])


_RUN_RANK = 2**40


def seeded_order(seeded_from: int | None, entity_id: str) -> tuple[int, int, int]:
    """The order a client sees: what was seeded first, in the order it was seeded, then what the run made, by id."""
    if seeded_from is not None:
        return 0, seeded_from, 0
    kind, number = ordinal(entity_id)
    return 1, kind, number


def millis(at: datetime) -> int:
    """A moment as YouTrack writes one: epoch milliseconds."""
    return int(at.timestamp() * 1000)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


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


def link_ref(link: wire.StoredLink) -> EntityRef:
    return _ref(EntityKind.RECORD, f"link:{link.source}:{link.linkType}:{link.target}")


def _short_name_of(readable: str) -> tuple[str, int] | None:
    """`DEMO-12` as its project's short name and its number, or None when it is not that shape."""
    short, dash, number = readable.rpartition("-")
    if not dash or not short or not number.isdigit():
        return None
    return short, int(number)


def ordinal(entity_id: str) -> tuple[int, int]:
    """A database id (`2-17`) in YouTrack's order: by its type, then its number."""
    kind, _, number = entity_id.partition("-")
    return int(kind), int(number)


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

    def _one(self, external_id: str, parent: str, kind: EntityKind = EntityKind.RECORD) -> Stored | None:
        stored = self._store.get(_ref(kind, external_id))
        return stored if stored is not None and stored.parent == parent else None

    def settings(self) -> wire.StoredInstance:
        stored = self._one(SETTINGS, INSTANCE)
        if stored is None:
            raise LookupError("the instance has no settings: it was never seeded")
        return wire.parse(wire.StoredInstance, stored.body)

    def users(self) -> list[wire.StoredUser]:
        found = [wire.parse(wire.StoredUser, s.body) for s in self._all(EntityKind.RECORD, USERS)]
        return sorted(found, key=lambda u: ordinal(u.id))

    def user(self, user: str) -> wire.StoredUser | None:
        stored = self._one(user, USERS)
        return None if stored is None else wire.parse(wire.StoredUser, stored.body)

    def user_by_login(self, login: str) -> wire.StoredUser | None:
        wanted = login.strip().lower()
        return next((u for u in self.users() if u.login.lower() == wanted), None)

    def user_by_email(self, email: str) -> wire.StoredUser | None:
        wanted = email.strip().lower()
        return next((u for u in self.users() if u.email is not None and u.email.lower() == wanted), None)

    def user_by_ring_id(self, ring_id: str) -> wire.StoredUser | None:
        return next((u for u in self.users() if u.ringId == ring_id), None)

    def user_named(self, reference: str) -> wire.StoredUser | None:
        """A user the way a query names one: by login, full name or email, whole values only."""
        wanted = reference.strip().lower()
        return next(
            (
                u
                for u in self.users()
                if wanted in (u.login.lower(), u.fullName.lower(), (u.email or "").lower()) and wanted
            ),
            None,
        )

    def agent(self) -> wire.StoredUser:
        found = self.user_by_login(AGENT_LOGIN)
        if found is None:
            raise LookupError("the instance has no agent account: it was never seeded")
        return found

    def definitions(self) -> list[wire.StoredFieldDefinition]:
        found = [wire.parse(wire.StoredFieldDefinition, s.body) for s in self._all(EntityKind.RECORD, DEFINITIONS)]
        return sorted(found, key=lambda d: ordinal(d.id))

    def definition(self, definition: str) -> wire.StoredFieldDefinition | None:
        stored = self._one(definition, DEFINITIONS)
        return None if stored is None else wire.parse(wire.StoredFieldDefinition, stored.body)

    def definition_named(self, name: str) -> wire.StoredFieldDefinition | None:
        wanted = name.strip().lower()
        return next((d for d in self.definitions() if d.name.lower() == wanted), None)

    def projects(self) -> list[wire.StoredProject]:
        found = [wire.parse(wire.StoredProject, s.body) for s in self._all(EntityKind.RECORD, PROJECTS)]
        return sorted(found, key=lambda p: ordinal(p.id))

    def project(self, project: str) -> wire.StoredProject | None:
        stored = self._one(project, PROJECTS)
        return None if stored is None else wire.parse(wire.StoredProject, stored.body)

    def project_named(self, name: str) -> wire.StoredProject | None:
        """A project by its short name or its name, as a query or a path names one."""
        wanted = name.strip().lower()
        return next((p for p in self.projects() if p.shortName.lower() == wanted or p.name.lower() == wanted), None)

    def issue(self, issue: str) -> wire.StoredIssue | None:
        stored = self._store.get(issue_ref(issue))
        return None if stored is None else wire.parse(wire.StoredIssue, stored.body)

    def issue_history(self, issue: str) -> list[tuple[int, wire.StoredIssue]]:
        """Every version of the issue with the seq that wrote it, oldest first: what its activity feed is read from."""
        return [(v.seq, wire.parse(wire.StoredIssue, v.body)) for v in self._store.versions(issue_ref(issue))]

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
        alias = self._one(f"{project.shortName}-{shape[1]}", project.id)
        if alias is None:
            return None
        return self.issue(wire.parse(wire.StoredAlias, alias.body).issue)

    def seeded_issue(self, position: int) -> wire.StoredIssue | None:
        """The issue seeded from `Scenario.tickets[position]`, while it is still there."""
        return next((i for i in self.every_issue() if i.seededFrom == position), None)

    def issues(self, project: str) -> list[wire.StoredIssue]:
        found = [wire.parse(wire.StoredIssue, s.body) for s in self._all(EntityKind.TICKET, project)]
        return sorted(found, key=lambda i: i.numberInProject)

    def every_issue(self) -> list[wire.StoredIssue]:
        return [issue for project in self.projects() for issue in self.issues(project.id)]

    def comments(self, issue: str) -> list[wire.StoredComment]:
        found = [wire.parse(wire.StoredComment, s.body) for s in self._all(EntityKind.COMMENT, issue)]
        return sorted(found, key=lambda c: seeded_order(c.seededFrom, c.id))

    def tags(self) -> list[wire.StoredTag]:
        found = [wire.parse(wire.StoredTag, s.body) for s in self._all(EntityKind.RECORD, TAGS)]
        return sorted(found, key=lambda t: seeded_order(t.seededFrom, t.id))

    def tag(self, tag: str) -> wire.StoredTag | None:
        stored = self._one(tag, TAGS)
        return None if stored is None else wire.parse(wire.StoredTag, stored.body)

    def tag_named(self, name: str) -> wire.StoredTag | None:
        wanted = name.strip().lower()
        return next((t for t in self.tags() if t.name.lower() == wanted), None)

    def link_types(self) -> list[wire.StoredLinkType]:
        found = [wire.parse(wire.StoredLinkType, s.body) for s in self._all(EntityKind.RECORD, LINK_TYPES)]
        return sorted(found, key=lambda t: ordinal(t.id))

    def links(self) -> list[wire.StoredLink]:
        """The links that hold now."""
        return [link for link in self.every_link() if link.removed is None]

    def every_link(self) -> list[wire.StoredLink]:
        """Every link ever made, those since removed too, in the order they were made: what a link's history is read
        from."""
        found = [(s.seq, wire.parse(wire.StoredLink, s.body)) for s in self._all(EntityKind.RECORD, LINKS)]
        return [link for _, link in sorted(found, key=lambda pair: (pair[1].created, pair[0]))]

    def link_history(self, link: wire.StoredLink) -> list[tuple[int, wire.StoredLink]]:
        """Every version of one link with the seq that wrote it, oldest first."""
        return [(v.seq, wire.parse(wire.StoredLink, v.body)) for v in self._store.versions(link_ref(link))]

    def comment_seqs(self, issue: str) -> dict[str, int]:
        """The seq that wrote each comment on the issue."""
        return {s.entity.external_id: s.seq for s in self._all(EntityKind.COMMENT, issue)}

    def token(self, token: str) -> wire.StoredToken | None:
        stored = self._one(f"token:{digest(token)}", TOKENS)
        return None if stored is None else wire.parse(wire.StoredToken, stored.body)

    def service(self, client_id: str) -> wire.StoredService | None:
        stored = self._one(f"service:{client_id}", SERVICES)
        return None if stored is None else wire.parse(wire.StoredService, stored.body)

    def grants(self) -> list[wire.StoredGrant]:
        stored = sorted(self._all(EntityKind.RECORD, GRANTS), key=lambda s: s.seq)
        return [wire.parse(wire.StoredGrant, s.body) for s in stored]

    def faults(self) -> list[wire.StoredFault]:
        return [wire.parse(wire.StoredFault, s.body) for s in self._all(EntityKind.RECORD, FAULTS)]

    def head(self) -> int:
        return self._store.head()

    def next_number(self, project: str) -> int:
        """The number the project's next issue takes: one past every number it has handed out."""
        return sum(1 for _ in self._all(EntityKind.RECORD, project)) + 1

    def taken(self, entity_id: str) -> bool:
        """Whether an issue, a comment or a tag already has this id."""
        return any(
            self._store.get(_ref(kind, entity_id)) is not None
            for kind in (EntityKind.TICKET, EntityKind.COMMENT, EntityKind.RECORD)
        )

    def next_id(self, prefix: int) -> str:
        """A database id from the sequence of the event about to be written."""
        return f"{prefix}-{self._store.head() + 1}"

    # ------------------------------------------------------------------ what an issue is

    def project_field(self, project: wire.StoredProject, name: str) -> wire.StoredProjectField | None:
        """The project's field of that name, as the instance names it."""
        wanted = name.strip().lower()
        for field in project.fields:
            definition = self.definition(field.field)
            if definition is not None and definition.name.lower() == wanted:
                return field
        return None

    def typed_field(
        self, project: wire.StoredProject, kind: wire.FieldType, name: str
    ) -> wire.StoredProjectField | None:
        """The field a provider-neutral fact is read from: the one named `name` if it has the type, else the first
        field of the type."""
        typed = [f for f in project.fields if self._type_of(f) is kind]
        named = self.project_field(project, name)
        if named is not None and named in typed:
            return named
        return typed[0] if typed else None

    def _type_of(self, field: wire.StoredProjectField) -> wire.FieldType | None:
        definition = self.definition(field.field)
        return None if definition is None else definition.fieldType

    def state_field(self, project: wire.StoredProject) -> wire.StoredProjectField | None:
        return self.typed_field(project, wire.FieldType.STATE, STATE_FIELD)

    def assignee_field(self, project: wire.StoredProject) -> wire.StoredProjectField | None:
        return self.typed_field(project, wire.FieldType.USER, ASSIGNEE_FIELD)

    def state_of(self, project: wire.StoredProject, issue: wire.StoredIssue) -> wire.StoredBundleValue | None:
        field = self.state_field(project)
        if field is None or field.id not in issue.values:
            return None
        return next((v for v in field.values if v.id == issue.values[field.id]), None)

    def assignee_of(self, project: wire.StoredProject, issue: wire.StoredIssue) -> wire.StoredUser | None:
        field = self.assignee_field(project)
        if field is None or field.id not in issue.values:
            return None
        return self.user(str(issue.values[field.id]))

    def is_resolved(self, project: wire.StoredProject, issue: wire.StoredIssue) -> bool:
        state = self.state_of(project, issue)
        return state is not None and state.isResolved

    def snapshot(self, issue: wire.StoredIssue) -> TicketSnapshot:
        project = self.project(issue.project)
        if project is None:
            raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
        assignee = self.assignee_of(project, issue)
        state = self.state_of(project, issue)
        return TicketSnapshot(
            title=issue.summary,
            body=issue.description or "",
            project=project.shortName,
            assignee_email=assignee.email if assignee is not None else None,
            state=state.outcome if state is not None else TicketState.OPEN,
        )

    def moved(
        self,
        issue: wire.StoredIssue,
        project: wire.StoredProject,
        to: wire.StoredBundleValue,
        *,
        by: str,
        at: int,
    ) -> wire.StoredIssue:
        """The issue in state `to`: `resolved` is set when it enters a resolved state and cleared when it leaves one."""
        field = self.state_field(project)
        if field is None:
            raise LookupError(f"project {project.shortName} has no State field")
        changed = issue.model_copy(update={"values": {**issue.values, field.id: to.id}})
        return self.settled(changed, project, was=issue, by=by, at=at)

    def settled(
        self, issue: wire.StoredIssue, project: wire.StoredProject, *, was: wire.StoredIssue, by: str, at: int
    ) -> wire.StoredIssue:
        """`issue` after a write: stamped with who wrote it and when, and its `resolved` moved with its state."""
        resolved = issue.resolved
        if self.is_resolved(project, issue) and not self.is_resolved(project, was):
            resolved = at
        elif not self.is_resolved(project, issue):
            resolved = None
        return issue.model_copy(update={"resolved": resolved, "updated": at, "updater": by})

    def state_for(self, project: wire.StoredProject, outcome: TicketState) -> wire.StoredBundleValue:
        """The first value of the project's State field that means `outcome`."""
        field = self.state_field(project)
        found = None if field is None else next((s for s in field.values if s.outcome is outcome), None)
        if found is None:
            raise LookupError(f"project {project.shortName} has no State value that is {outcome.value}")
        return found

    # ------------------------------------------------------------------ writes

    def _write(self, ref: EntityRef, parent: str, body: wire.Wire, *, actor: Actor, create: bool) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=ref,
                operation=Operation.CREATE if create else Operation.UPDATE,
                actor=actor,
                body=wire.dump(body),
                parent=parent,
            )
        )

    def write_settings(self, settings: wire.StoredInstance, *, actor: Actor) -> WorldEvent:
        ref = _ref(EntityKind.RECORD, SETTINGS)
        return self._write(ref, INSTANCE, settings, actor=actor, create=self._store.get(ref) is None)

    def write_user(self, user: wire.StoredUser, *, actor: Actor) -> WorldEvent:
        ref = user_ref(user.id)
        return self._write(ref, USERS, user, actor=actor, create=self._store.get(ref) is None)

    def write_definition(self, definition: wire.StoredFieldDefinition, *, actor: Actor) -> WorldEvent:
        ref = _ref(EntityKind.RECORD, definition.id)
        return self._write(ref, DEFINITIONS, definition, actor=actor, create=self._store.get(ref) is None)

    def write_project(self, project: wire.StoredProject, *, actor: Actor) -> WorldEvent:
        ref = project_ref(project.id)
        return self._write(ref, PROJECTS, project, actor=actor, create=self._store.get(ref) is None)

    def write_tag(self, tag: wire.StoredTag, *, actor: Actor) -> WorldEvent:
        return self._write(_ref(EntityKind.RECORD, tag.id), TAGS, tag, actor=actor, create=True)

    def write_link_type(self, link_type: wire.StoredLinkType, *, actor: Actor) -> WorldEvent:
        return self._write(_ref(EntityKind.RECORD, link_type.id), LINK_TYPES, link_type, actor=actor, create=True)

    def write_link(self, link: wire.StoredLink, *, actor: Actor) -> WorldEvent:
        """Make the link, or make a removed one hold again."""
        ref = link_ref(link)
        return self._write(ref, LINKS, link, actor=actor, create=self._store.get(ref) is None)

    def remove_link(self, link: wire.StoredLink, *, by: str, at: int, actor: Actor) -> WorldEvent:
        removed = link.model_copy(update={"removed": at, "remover": by})
        return self._write(link_ref(link), LINKS, removed, actor=actor, create=False)

    def write_token(self, token: str, record: wire.StoredToken, *, actor: Actor) -> WorldEvent:
        return self._write(_ref(EntityKind.RECORD, f"token:{digest(token)}"), TOKENS, record, actor=actor, create=True)

    def write_service(self, service: wire.StoredService, *, actor: Actor) -> WorldEvent:
        ref = _ref(EntityKind.RECORD, f"service:{service.clientId}")
        return self._write(ref, SERVICES, service, actor=actor, create=True)

    def write_grant(self, number: int, grant: wire.StoredGrant, *, actor: Actor) -> WorldEvent:
        return self._write(_ref(EntityKind.RECORD, f"grant:{number}"), GRANTS, grant, actor=actor, create=True)

    def write_fault(self, number: int, fault: wire.StoredFault, *, actor: Actor) -> WorldEvent:
        return self._write(_ref(EntityKind.RECORD, f"fault:{number}"), FAULTS, fault, actor=actor, create=True)

    def create_issue(self, issue: wire.StoredIssue, *, actor: Actor) -> WorldEvent:
        """Its readable id, then the issue: a fork between the two has spent the number and holds no issue."""
        after = self.snapshot(issue)
        self._store.apply(
            Change(
                entity=alias_ref(issue.idReadable),
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

    def delete_issue(self, issue: wire.StoredIssue, *, by: str, at: int, actor: Actor) -> WorldEvent:
        """The issue and every link it is an end of: YouTrack keeps no link to an issue that is gone."""
        for link in self.links():
            if issue.id in (link.source, link.target):
                self.remove_link(link, by=by, at=at, actor=actor)
        return self._store.apply(
            Change(
                entity=issue_ref(issue.id),
                operation=Operation.DELETE,
                actor=actor,
                parent=issue.project,
            )
        )

    def write_comment(self, comment: wire.StoredComment, *, actor: Actor) -> WorldEvent:
        return self._store.apply(
            Change(
                entity=comment_ref(comment.id),
                operation=Operation.CREATE,
                actor=actor,
                body=wire.dump(comment),
                parent=comment.issue,
            )
        )

    def saw(self, ref: EntityRef, operation: Operation) -> WorldEvent:
        """Record that the agent read or searched something. It changes nothing."""
        return self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))


def placed(additions: Sequence[Change], world: Store) -> list[Change]:
    """`PlacesAdditions`: each issue a further seed adds to a project the world holds takes the project's next
    number, as the agent's next issue there would, so one the agent has filed since the world was seeded keeps its
    readable id. The readable-id record and the issue it names are renamed together."""
    youtrack = YouTrackWorld(world)
    found = list(additions)
    issues = {c.entity.external_id: n for n, c in enumerate(found) if c.entity.kind is EntityKind.TICKET}
    following: dict[str, int] = {}
    for n, change in enumerate(found):
        if change.entity.kind is not EntityKind.RECORD or change.parent is None or change.body is None:
            continue
        project = youtrack.project(change.parent)
        if project is None:
            continue  # a project the addition itself brings: nothing in the world is numbered in it
        alias = wire.parse(wire.StoredAlias, change.body)
        if alias.issue not in issues:
            raise ValueError(
                f"the readable-id record {change.entity.external_id} names issue {alias.issue}, which is not added"
            )
        number = following[change.parent] if change.parent in following else youtrack.next_number(change.parent)
        following[change.parent] = number + 1
        readable = f"{project.shortName}-{number}"
        held = found[issues[alias.issue]]
        issue = wire.parse(wire.StoredIssue, held.body or "")
        if issue.idReadable == readable:
            continue
        found[n] = change.model_copy(update={"entity": alias_ref(readable)})
        renumbered = issue.model_copy(update={"idReadable": readable, "numberInProject": number})
        found[issues[alias.issue]] = held.model_copy(update={"body": wire.dump(renumbered)})
    return found
