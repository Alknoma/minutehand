"""The instance a scenario starts in.

From the provider-neutral scenario: a user per person and one for the agent; a project per distinct seeded project
name, each carrying the standard field set with every user on its team and the agent as its leader; an issue per
seeded ticket with its title, body, assignee and state, its labels as tags and its comments.

From the scenario's YouTrack seed (`ProviderSeed` with provider `youtrack`, its text a `YouTrackSeed` as JSON):
more users, the tokens and Hub services that act as them, more instance fields, projects with their own field sets,
bundles, defaults, teams and leaders, any field value on a seeded issue, links between seeded issues, an issue's
history before the run, permissions given or taken, and refusals put in the way of calls for a while.

A seeded issue is created as the scenario and its seed describe it, `created_ago` before the start; each history
entry is then a change after that, oldest first, by the user it names. The run starts from where the history ends.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

from pydantic import Field, model_validator

from minutehand.adapters.providers.youtrack import fields, state, wire
from minutehand.adapters.providers.youtrack.manifest import MANIFEST
from minutehand.adapters.providers.youtrack.state import YouTrackWorld
from minutehand.domain.scenario import Model, Scenario, SeededTicket, TicketState
from minutehand.domain.world import Actor
from minutehand.ports.store import Store

_SHORT_NAME_MAX = 10
_RING = uuid.UUID("6f1c3a52-6d3b-4b8e-9a51-2f0f6c1d7e40")
DAY_AT = time(12, 0)
"""The instant YouTrack's date picker stores for a day: noon UTC, which reads as that day from UTC-12 to UTC+11."""

LINK_TYPES = [
    wire.StoredLinkType(
        id="106-0",
        name="Subtask",
        sourceToTarget="parent for",
        targetToSource="subtask of",
        directed=True,
        aggregation=True,
    ),
    wire.StoredLinkType(
        id="106-1", name="Depend", sourceToTarget="is required for", targetToSource="depends on", directed=True
    ),
    wire.StoredLinkType(
        id="106-2", name="Duplicate", sourceToTarget="duplicates", targetToSource="is duplicated by", directed=True
    ),
    wire.StoredLinkType(
        id="106-3", name="Relates", sourceToTarget="relates to", targetToSource="relates to", directed=False
    ),
]
"""YouTrack's four default link types."""


# --------------------------------------------------------------------------- the YouTrack seed

Login = str


class UserSeed(Model):
    login: Login = Field(pattern=r"^[A-Za-z0-9._-]+$")
    name: str
    email: str | None = None
    banned: bool = False


class TokenSeed(Model):
    """A permanent token (`perm:…`) that acts as a user. Once any is seeded, only these and issued ones are let in."""

    token: str = Field(min_length=1)
    login: Login


class ServiceSeed(Model):
    """A Hub service that may ask `/hub/api/rest/oauth2/token` for a token with its secret, and acts as a user."""

    client_id: str = Field(min_length=1)
    secret: str = Field(min_length=1)
    login: Login


class FieldSeed(Model):
    """A field the instance defines beyond the standard set."""

    name: str
    type: wire.FieldType


class ValueSeed(Model):
    name: str
    resolved: bool = False
    means: TicketState | None = Field(default=None, description="Default: done when resolved, open otherwise")

    def value(self) -> fields.Value:
        means = self.means or (TicketState.DONE if self.resolved else TicketState.OPEN)
        return fields.Value(name=self.name, resolved=self.resolved, outcome=means)


class ProjectFieldSeed(Model):
    """One field a project carries. Values and defaults default to the standard field's."""

    name: str
    values: list[ValueSeed] | None = None
    can_be_empty: bool | None = None
    default: str | None = None


class ProjectSeed(Model):
    name: str = Field(description="The project a seeded ticket names, or a project no ticket is in yet")
    short_name: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    description: str = ""
    leader: Login | None = Field(default=None, description="Default: the agent")
    team: list[Login] | None = Field(default=None, description="Default: every user")
    fields: list[ProjectFieldSeed] | None = Field(default=None, description="Default: the standard set")
    created_through_api: bool = Field(
        default=False, description="As if the agent made it: no Hub project, and nobody holds Update Project on it"
    )


Scalar = str | int | float


class FieldValueSeed(Model):
    """A value as a person would write it: a bundle value's name, a login, an ISO date or epoch milliseconds, a
    period (`1h 30m`) or minutes, a number or text. None leaves the field empty."""

    field: str
    value: Scalar | None


class LinkSeed(Model):
    phrase: str = Field(description="How this issue relates to the other: `subtask of`, `depends on`, `relates to`")
    ticket: str = Field(description="SeededTicket.key of the other issue")


class HistorySeed(Model):
    ago: timedelta = Field(gt=timedelta(0), description="Before the scenario's start")
    by: Login
    fields: list[FieldValueSeed]


class IssueSeed(Model):
    ticket: str = Field(description="SeededTicket.key")
    fields: list[FieldValueSeed] = []
    links: list[LinkSeed] = []
    created_ago: timedelta = Field(default=timedelta(0), ge=timedelta(0))
    history: list[HistorySeed] = []

    @model_validator(mode="after")
    def _history_after_creation(self) -> IssueSeed:
        late = [h.ago for h in self.history if h.ago > self.created_ago]
        if late:
            raise ValueError(f"history of {self.ticket} reaches back before it was created ({self.created_ago})")
        return self


class GrantSeed(Model):
    """A permission given to, or taken from, one user, in one project or all of them."""

    login: Login
    permission: wire.Permission
    project: str | None = Field(default=None, description="A short name or a name; None for every project")
    held: bool


class FaultSeed(Model):
    """Calls to one path are refused with `status` from `after` the start for `lasts` (for good when None)."""

    method: str = "GET"
    path: str = Field(
        pattern=r"^/",
        description="As the API names it without `/api` (`/issues/*/customFields/*`), or Hub's `/hub/api/rest/…`",
    )
    status: int = Field(ge=400, le=599)
    after: timedelta = timedelta(0)
    lasts: timedelta | None = None


class YouTrackSeed(Model):
    users: list[UserSeed] = []
    tokens: list[TokenSeed] = []
    services: list[ServiceSeed] = []
    fields: list[FieldSeed] = []
    projects: list[ProjectSeed] = []
    issues: list[IssueSeed] = []
    grants: list[GrantSeed] = []
    faults: list[FaultSeed] = []
    count_unknown: bool = Field(default=False, description="issuesGetter/count answers -1, as while it still counts")


def youtrack_seed(scenario: Scenario) -> YouTrackSeed:
    found = next((s for s in scenario.provider_seeds if s.provider == MANIFEST.key), None)
    return YouTrackSeed() if found is None else YouTrackSeed.model_validate_json(found.body)


# --------------------------------------------------------------------------- ids


def ring_id(kind: str, name: str) -> str:
    """A Hub id: the same for the same thing in every run."""
    return str(uuid.uuid5(_RING, f"{kind}:{name}"))


def user(user_id: str, login: str, full_name: str, email: str | None, *, banned: bool = False) -> wire.StoredUser:
    return wire.StoredUser(
        id=user_id, login=login, fullName=full_name, email=email, ringId=ring_id("user", login), banned=banned
    )


def short_name(name: str, taken: set[str]) -> str:
    """A project key from its name: its letters and digits, upper-cased, made unique with a digit."""
    base = "".join(c for c in name if c.isascii() and c.isalnum()).upper()[:_SHORT_NAME_MAX] or "PROJECT"
    if not base[0].isalpha():
        base = ("P" + base)[:_SHORT_NAME_MAX]
    candidate, suffix = base, 2
    while candidate in taken:
        candidate = f"{base[: _SHORT_NAME_MAX - len(str(suffix))]}{suffix}"
        suffix += 1
    return candidate


def new_project(
    number: int,
    name: str,
    key: str,
    *,
    leader: str,
    created_by: str,
    team: list[str],
    project_fields: list[wire.StoredProjectField],
    description: str = "",
    created_through_api: bool = False,
) -> wire.StoredProject:
    project_id = f"0-{number}"
    return wire.StoredProject(
        id=project_id,
        shortName=key,
        name=name,
        description=description,
        leader=leader,
        createdBy=created_by,
        team=team,
        teamGroup=f"3-{number}",
        ringId=ring_id("project", key),
        teamRingId=ring_id("team", key),
        createdThroughApi=created_through_api,
        fields=project_fields,
    )


# --------------------------------------------------------------------------- values as a person writes them


def seeded_value(
    world: YouTrackWorld, project: wire.StoredProject, written: FieldValueSeed
) -> tuple[wire.StoredProjectField, wire.FieldValue | None]:
    """A seed's value for one of the project's fields, as the store keeps it."""
    field = world.project_field(project, written.field)
    definition = None if field is None else world.definition(field.field)
    if field is None or definition is None:
        raise ValueError(f"project {project.shortName} has no field {written.field!r}")
    value = written.value
    if value is None:
        return field, None
    kind = definition.fieldType
    if kind in wire.BUNDLED:
        found = next((v for v in field.values if v.name.lower() == str(value).lower()), None)
        if found is None:
            raise ValueError(f"{written.field} of {project.shortName} has no value {value!r}")
        return field, found.id
    if kind is wire.FieldType.USER:
        named = world.user_by_login(str(value))
        if named is None or named.id not in project.team:
            raise ValueError(f"{value!r} is no user on the team of {project.shortName}")
        return field, named.id
    if kind in wire.DATED:
        return field, _moment(value)
    if kind is wire.FieldType.PERIOD:
        minutes = value if isinstance(value, int) else fields.period_minutes(str(value))
        if minutes is None:
            raise ValueError(f"{written.field} takes a period such as '1h 30m', not {value!r}")
        return field, minutes
    if kind is wire.FieldType.FLOAT and isinstance(value, int | float):
        return field, float(value)
    if kind is wire.FieldType.INTEGER and isinstance(value, int):
        return field, value
    if kind is wire.FieldType.STRING and isinstance(value, str):
        return field, value
    raise ValueError(f"{written.field} is a {kind.value} field and cannot hold {value!r}")


def _moment(value: Scalar) -> int:
    if isinstance(value, int):
        return value
    text = str(value)
    if len(text) == len("2026-08-24"):
        day = date.fromisoformat(text)
        return state.millis(datetime.combine(day, DAY_AT, tzinfo=UTC))
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"a moment needs its offset: {text!r}")
    return state.millis(moment)


# --------------------------------------------------------------------------- seeding


def seed(scenario: Scenario, world: Store) -> None:
    youtrack = YouTrackWorld(world)
    extra = youtrack_seed(scenario)
    youtrack.write_settings(
        wire.StoredInstance(tokensRequired=bool(extra.tokens), countUnknown=extra.count_unknown), actor=Actor.SCENARIO
    )

    agent = user("1-0", state.AGENT_LOGIN, state.AGENT_NAME, state.AGENT_EMAIL)
    accounts = [agent]
    accounts += [user(f"1-{n + 1}", p.key, p.name, p.email) for n, p in enumerate(scenario.people)]
    accounts += [
        user(f"1-{len(accounts) + n}", u.login, u.name, u.email, banned=u.banned) for n, u in enumerate(extra.users)
    ]
    logins = [a.login.lower() for a in accounts]
    if len(logins) != len(set(logins)):
        raise ValueError("two YouTrack users share a login")
    for account in accounts:
        youtrack.write_user(account, actor=Actor.SCENARIO)
    by_login = {a.login: a for a in accounts}

    def login(name: Login) -> wire.StoredUser:
        if name not in by_login:
            raise ValueError(f"the YouTrack seed names {name!r}, who is no user")
        return by_login[name]

    for token in extra.tokens:
        youtrack.write_token(token.token, wire.StoredToken(user=login(token.login).id), actor=Actor.SCENARIO)
    for service in extra.services:
        youtrack.write_service(
            wire.StoredService(
                clientId=service.client_id, secretDigest=state.digest(service.secret), user=login(service.login).id
            ),
            actor=Actor.SCENARIO,
        )

    definitions = [fields.stored_definition(d) for d in fields.STANDARD]
    for n, added in enumerate(extra.fields):
        if any(d.name.lower() == added.name.lower() for d in definitions):
            raise ValueError(f"the instance already defines a field {added.name!r}")
        definitions.append(wire.StoredFieldDefinition(id=f"58-{100 + n}", name=added.name, fieldType=added.type))
    for definition in definitions:
        youtrack.write_definition(definition, actor=Actor.SCENARIO)
    for link_type in LINK_TYPES:
        youtrack.write_link_type(link_type, actor=Actor.SCENARIO)

    seeded = [t for t in scenario.tickets if t.provider == MANIFEST.key]
    named = list(dict.fromkeys([*(p.name for p in extra.projects), *(t.project for t in seeded)]))
    described = {p.name: p for p in extra.projects}
    everyone = [a.id for a in accounts]
    projects: dict[str, wire.StoredProject] = {}
    ids = fields.Ids([])
    for name in named:
        detail = described.get(name, ProjectSeed(name=name))
        taken = {p.shortName for p in projects.values()}
        key = detail.short_name or short_name(name, taken)
        if key in taken:
            raise ValueError(f"two YouTrack projects share the short name {key}")
        team = everyone if detail.team is None else [login(m).id for m in detail.team]
        leader = agent if detail.leader is None else login(detail.leader)
        made = new_project(
            len(projects),
            name,
            key,
            leader=leader.id,
            created_by=agent.id,
            team=list(dict.fromkeys([leader.id, *team])),
            project_fields=_project_fields(ids, definitions, detail),
            description=detail.description,
            created_through_api=detail.created_through_api,
        )
        projects[name] = made
        youtrack.write_project(made, actor=Actor.SCENARIO)

    for number, grant in enumerate(extra.grants):
        project = None
        if grant.project is not None:
            project = next(
                (p for p in projects.values() if grant.project.lower() in (p.shortName.lower(), p.name.lower())), None
            )
            if project is None:
                raise ValueError(f"a grant names project {grant.project!r}, which the instance has not got")
        youtrack.write_grant(
            number,
            wire.StoredGrant(
                user=login(grant.login).id,
                permission=grant.permission,
                project=None if project is None else project.id,
                held=grant.held,
            ),
            actor=Actor.SCENARIO,
        )

    start = scenario.starts_at
    write_faults(youtrack, extra.faults, start)

    details = {i.ticket: i for i in extra.issues}
    keys = {t.key for t in seeded if t.key is not None}
    stray = sorted(set(details) - keys)
    if stray:
        raise ValueError(f"the YouTrack seed describes tickets no seeded YouTrack ticket is: {', '.join(stray)}")
    people = {p.key: by_login[p.key] for p in scenario.people}
    reporter = people[scenario.owner]
    made_issues: dict[str, wire.StoredIssue] = {}
    for position, ticket in enumerate(scenario.tickets):
        if ticket.provider != MANIFEST.key:
            continue
        detail = details.get(ticket.key or "", IssueSeed(ticket=ticket.key or "-"))
        issue = _seed_issue(youtrack, projects[ticket.project], ticket, position, detail, reporter, people, start)
        if ticket.key is not None:
            made_issues[ticket.key] = issue
    for detail in extra.issues:
        for link in detail.links:
            if link.ticket not in made_issues:
                raise ValueError(f"{detail.ticket} links to {link.ticket}, which is no seeded YouTrack ticket")
            _seed_link(youtrack, made_issues[detail.ticket], made_issues[link.ticket], link.phrase, reporter, start)
    for detail in sorted(extra.issues, key=lambda d: d.ticket):
        _seed_history(youtrack, made_issues[detail.ticket], detail, start)


def _project_fields(
    ids: fields.Ids, definitions: list[wire.StoredFieldDefinition], detail: ProjectSeed
) -> list[wire.StoredProjectField]:
    if detail.fields is None:
        return [fields.standard_field(ids, d, template=False) for d in fields.SEEDED_SET]
    standard = {d.name.lower(): d for d in fields.STANDARD}
    made: list[wire.StoredProjectField] = []
    for wanted in detail.fields:
        definition = next((d for d in definitions if d.name.lower() == wanted.name.lower()), None)
        if definition is None:
            raise ValueError(f"project {detail.name} carries {wanted.name!r}, which the instance does not define")
        known = standard.get(definition.name.lower())
        values = (
            [v.value() for v in wanted.values]
            if wanted.values is not None
            else fields.STANDARD_VALUES.get(definition.name, [])
        )
        made.append(
            fields.project_field(
                ids,
                definition,
                values=values,
                can_be_empty=wanted.can_be_empty
                if wanted.can_be_empty is not None
                else (known.can_be_empty if known is not None else True),
                empty_text=known.empty_text if known is not None else "No value",
                default=wanted.default
                if wanted.default is not None
                else (fields.DEFAULTS.get(definition.name) if wanted.values is None else None),
            )
        )
    return made


def _seed_issue(
    youtrack: YouTrackWorld,
    home: wire.StoredProject,
    ticket: SeededTicket,
    position: int,
    detail: IssueSeed,
    reporter: wire.StoredUser,
    people: dict[str, wire.StoredUser],
    start: datetime,
) -> wire.StoredIssue:
    at = state.millis(start - detail.created_ago)
    number = youtrack.next_number(home.id)
    values: dict[str, wire.FieldValue] = {f.id: f.defaultValue for f in home.fields if f.defaultValue is not None}
    state_field = youtrack.state_field(home)
    if state_field is not None:
        values[state_field.id] = youtrack.state_for(home, ticket.state).id
    assignee_field = youtrack.assignee_field(home)
    if ticket.assignee is not None:
        if assignee_field is None:
            raise ValueError(f"{ticket.title!r} has an assignee and project {home.shortName} no user field")
        values[assignee_field.id] = people[ticket.assignee].id
    for written in detail.fields:
        field, value = seeded_value(youtrack, home, written)
        if value is None:
            values.pop(field.id, None)
        else:
            values[field.id] = value
    tags: list[str] = []
    for label in dict.fromkeys(ticket.labels):
        tag = youtrack.tag_named(label)
        if tag is None:
            tag = wire.StoredTag(id=youtrack.next_id(6), name=label, owner=reporter.id)
            youtrack.write_tag(tag, actor=Actor.SCENARIO)
        tags.append(tag.id)
    issue = wire.StoredIssue(
        id=youtrack.next_id(2),
        idReadable=f"{home.shortName}-{number}",
        numberInProject=number,
        project=home.id,
        summary=ticket.title,
        description=ticket.body or None,
        reporter=reporter.id,
        updater=reporter.id,
        created=at,
        updated=at,
        values=values,
        tags=tags,
        seededFrom=position,
    )
    issue = issue.model_copy(update={"resolved": at if youtrack.is_resolved(home, issue) else None})
    youtrack.create_issue(issue, actor=Actor.SCENARIO)
    for comment in ticket.comments:
        youtrack.write_comment(
            wire.StoredComment(
                id=youtrack.next_id(4),
                issue=issue.id,
                text=comment.text,
                author=people[comment.by].id,
                created=state.millis(start),
            ),
            actor=Actor.SCENARIO,
        )
    return issue


def _seed_link(
    youtrack: YouTrackWorld,
    issue: wire.StoredIssue,
    other: wire.StoredIssue,
    phrase: str,
    author: wire.StoredUser,
    start: datetime,
) -> None:
    wanted = phrase.strip().lower()
    for link_type in youtrack.link_types():
        if wanted == link_type.sourceToTarget.lower():
            source, target = issue, other
        elif wanted == link_type.targetToSource.lower():
            source, target = other, issue
        else:
            continue
        youtrack.write_link(
            wire.StoredLink(
                source=source.id,
                linkType=link_type.id,
                target=target.id,
                created=state.millis(start),
                author=author.id,
            ),
            actor=Actor.SCENARIO,
        )
        return
    raise ValueError(f"no link type of the instance reads {phrase!r}")


def _seed_history(youtrack: YouTrackWorld, issue: wire.StoredIssue, detail: IssueSeed, start: datetime) -> None:
    project = youtrack.project(issue.project)
    if project is None:
        raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
    current = youtrack.issue(issue.id) or issue
    for entry in sorted(detail.history, key=lambda h: h.ago, reverse=True):
        author = youtrack.user_by_login(entry.by)
        if author is None:
            raise ValueError(f"the history of {detail.ticket} names {entry.by!r}, who is no user")
        values = dict(current.values)
        for written in entry.fields:
            field, value = seeded_value(youtrack, project, written)
            if value is None:
                values.pop(field.id, None)
            else:
                values[field.id] = value
        changed = current.model_copy(update={"values": values})
        changed = youtrack.settled(changed, project, was=current, by=author.id, at=state.millis(start - entry.ago))
        youtrack.update_issue(changed, actor=Actor.SCENARIO)
        current = changed


def write_faults(youtrack: YouTrackWorld, faults: list[FaultSeed], start: datetime) -> None:
    """Record each fault after those already recorded, from `start` plus its own offset."""
    first = len(youtrack.faults())
    for number, fault in enumerate(faults, start=first):
        youtrack.write_fault(
            number,
            wire.StoredFault(
                method=fault.method.upper(),
                path=fault.path,
                status=fault.status,
                starts=state.millis(start + fault.after),
                ends=None if fault.lasts is None else state.millis(start + fault.after + fault.lasts),
            ),
            actor=Actor.SCENARIO,
        )
