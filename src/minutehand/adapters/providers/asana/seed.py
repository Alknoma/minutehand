"""The workspace a scenario starts in, written as actor SCENARIO and stamped at the scenario's start.

From the shared scenario: a user per person and one for the agent, a project per
project the scenario's Asana tickets name, and those tickets as tasks: a ticket's
`labels` are its tags (a tag is made for a label the seed does not define), and its
`comments` are stories by their people, written as the scenario starts. A ticket's
`key` has no meaning in Asana, and a scenario that sets one is refused at load
(`Manifest.ticket_fields`).

From the scenario's Asana seed (`AsanaSeed`, the scenario's `ProviderSeed` for
`asana`), everything only Asana has: the workspace's plan and whether it is an
organization, teams, custom fields defined at workspace level, tags, projects with
their team, privacy, members, sections and the custom fields settled on them, a
seeded ticket's section, custom field values, tags, due date, parent and comments,
the tokens the workspace accepts, and stretches of time in which Asana throttles
every call. `status` says which fact about a task is its state for the scenario's
checks (see `wire.StatusRule`); a scenario with no Asana seed reads it from the
section, as `To do`, `Done` and `Cancelled` say.

Every name the seed uses (a person, a project, a field, an option, a tag, a ticket)
must name something; one that does not is refused before anything is written.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from minutehand.adapters.providers.asana import state, wire
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.state import AGENT_GID, WORKSPACE_GID, AsanaWorld
from minutehand.domain.scenario import Model, Scenario, SeededTicket, TicketState
from minutehand.domain.world import Actor, Operation
from minutehand.ports.store import Store


class SeedWorkspace(Model):
    name: str = state.WORKSPACE_NAME
    organization: bool = Field(default=True, description="An organization has teams; a project in one needs a team")
    premium: bool = Field(default=True, description="False: search and custom fields answer 402")
    unpaginated_limit: int = Field(
        default=wire.UNPAGINATED_MAX,
        ge=1,
        description="Past this many items a read without `limit` is refused, as Asana refuses a large one; lowered "
        "so a small workspace reaches the refusal",
    )


class SeedAgent(Model):
    """The user the agent acts as."""

    name: str = state.AGENT_NAME
    email: str = state.AGENT_EMAIL


class SeedTeam(Model):
    name: str
    members: list[str] | None = Field(default=None, description="Person keys; None: everyone")
    agent: bool = Field(default=True, description="Whether the agent's user is a member")


class SeedOption(Model):
    name: str
    color: str | None = None
    enabled: bool = True


class SeedField(Model):
    name: str
    kind: wire.FieldKind
    options: list[SeedOption] = Field(default=[], description="For enum and multi_enum only")
    precision: int = Field(default=0, ge=0, le=6, description="Decimal places a number field shows")

    @model_validator(mode="after")
    def _options_fit(self) -> SeedField:
        takes = self.kind in ("enum", "multi_enum")
        if takes and not self.options:
            raise ValueError(f"the {self.kind} field {self.name!r} has no options")
        if not takes and self.options:
            raise ValueError(f"the {self.kind} field {self.name!r} takes no options")
        names = [o.name for o in self.options]
        if len(names) != len(set(names)):
            raise ValueError(f"the field {self.name!r} has two options of one name")
        return self


class SeedSection(Model):
    name: str
    means: TicketState | None = Field(default=None, description="The state a task here is in, when status reads it")


class SeedProject(Model):
    name: str
    team: str | None = Field(default=None, description="SeedTeam.name; in an organization, None means the first team")
    notes: str = ""
    archived: bool = False
    private: bool = Field(default=False, description="Seen only by its members; anyone else is answered 403")
    members: list[str] | None = Field(default=None, description="Person keys; None: everyone")
    agent: bool = Field(default=True, description="Whether the agent's user is a member")
    sections: list[SeedSection] | None = Field(default=None, description="None: To do, Done and Cancelled")
    custom_fields: list[str] = Field(default=[], description="SeedField names settled on the project, in order")


class SeedValue(Model):
    """A custom field's value on a seeded task: the member for the field's kind is set, the rest left out."""

    field: str = Field(description="SeedField.name")
    option: str | None = Field(default=None, description="enum: the option's name")
    options: list[str] = Field(default=[], description="multi_enum: the options' names")
    text: str | None = None
    number: float | None = None
    date_after: timedelta | None = Field(default=None, description="date: an offset from the scenario's start")
    people: list[str] = Field(default=[], description="people: person keys")


class SeedComment(Model):
    person: str | None = Field(description="Person.key of the author; None: the agent")
    text: str = Field(min_length=1)
    ago: timedelta = Field(default=timedelta(0), description="How long before the scenario's start it was written")


class SeedTask(Model):
    """What only Asana says of one seeded ticket."""

    ticket: str = Field(description="SeededTicket.title of an asana ticket")
    section: str | None = Field(default=None, description="SeedSection.name in its project; None: as its state says")
    completed: bool | None = Field(default=None, description="None: completed unless the ticket's state is open")
    due_after: timedelta | None = Field(default=None, description="Due on the day this long after the start")
    due_has_time: bool = Field(default=False, description="Due at that moment (`due_at`) rather than on its day")
    tags: list[str] = Field(default=[], description="Tag names")
    values: list[SeedValue] = []
    parent: str | None = Field(
        default=None, description="SeededTicket.title of its parent; a subtask belongs to no project"
    )
    comments: list[SeedComment] = []


class SeedCompleted(Model):
    """State is the completed box alone: done or open, never cancelled."""

    kind: Literal["completed"] = "completed"


class SeedBySection(Model):
    """State is what the section a task is in means (`SeedSection.means`)."""

    kind: Literal["section"] = "section"


class SeedByField(Model):
    """State is what the value of one enum field means; an option it does not name, or none, is open."""

    kind: Literal["custom_field"] = "custom_field"
    field: str = Field(description="SeedField.name of an enum field")
    means: dict[str, TicketState] = Field(description="Option name to the state it means")


SeedStatus = Annotated[SeedCompleted | SeedBySection | SeedByField, Field(discriminator="kind")]


class SeedToken(Model):
    token: str = Field(min_length=1)
    person: str | None = Field(default=None, description="Person.key it acts as; None: the agent")
    expires_after: timedelta | None = Field(default=None, description="None: it never expires")


class SeedRefreshToken(Model):
    refresh_token: str = Field(min_length=1)
    person: str | None = Field(default=None, description="Person.key whose access it renews; None: the agent")


class SeedRateLimit(Model):
    """From `after` the scenario's start, for `lasts`, every call is answered 429 with `Retry-After`."""

    after: timedelta
    lasts: timedelta = Field(gt=timedelta(0))


class SeedLimits(Model):
    """The workspace's plan and thresholds, each None to leave it as it is: what a test changes on a world already
    open (`AsanaProvider.declare`), and what a seed may set apart from its workspace."""

    premium: bool | None = None
    organization: bool | None = None
    unpaginated_limit: int | None = Field(default=None, ge=1)


def limited(workspace: wire.AsanaWorkspace, limits: SeedLimits | None) -> wire.AsanaWorkspace:
    """The workspace with `limits` over it."""
    if limits is None:
        return workspace
    changed = {
        "premium": limits.premium,
        "is_organization": limits.organization,
        "unpaginated_limit": limits.unpaginated_limit,
    }
    return workspace.model_copy(update={k: v for k, v in changed.items() if v is not None})


class AsanaSeed(Model):
    workspace: SeedWorkspace = SeedWorkspace()
    agent: SeedAgent = SeedAgent()
    teams: list[SeedTeam] = Field(default=[], description="None declared, in an organization: one team of everyone")
    custom_fields: list[SeedField] = []
    tags: list[str] = []
    projects: list[SeedProject] = Field(default=[], description="Projects the asana tickets name are added if absent")
    tasks: list[SeedTask] = []
    status: SeedStatus = SeedBySection()
    tokens: list[SeedToken] = Field(default=[], description="None declared: any bearer token acts as the agent")
    refresh_tokens: list[SeedRefreshToken] = []
    rate_limits: list[SeedRateLimit] = []
    limits: SeedLimits | None = Field(
        default=None, description="The plan and thresholds over what `workspace` says: what an open world changes"
    )


def asana_seed(scenario: Scenario) -> AsanaSeed:
    found = scenario.provider_seed(MANIFEST.key)
    return AsanaSeed() if found is None else AsanaSeed.model_validate_json(found.body)


def seeded_gid(scenario: Scenario, ticket: SeededTicket) -> str:
    """The gid of the task seeded from `ticket`: its position among the scenario's asana tickets."""
    return state.task_gid(_position(scenario, ticket))


def _position(scenario: Scenario, ticket: SeededTicket) -> int:
    """`ticket`'s place among the scenario's asana tickets: a ticket added to an open world comes after them all."""
    mine = [t for t in scenario.tickets if t.provider == MANIFEST.key]
    return next(i for i, t in enumerate(mine) if t is ticket)


def seed(scenario: Scenario, world: Store) -> None:
    _Seeding(scenario, asana_seed(scenario), AsanaWorld(world)).write()


def _by_name[Named: (SeedTeam, SeedField, SeedProject)](items: list[Named], name: str, what: str) -> Named:
    found = next((i for i in items if i.name == name), None)
    if found is None:
        raise ValueError(f"the asana seed names the {what} {name!r}, which it does not define")
    return found


class _Seeding:
    """One pass over the scenario and its Asana seed, refusing any name that names nothing."""

    def __init__(self, scenario: Scenario, seed: AsanaSeed, asana: AsanaWorld) -> None:
        self.scenario = scenario
        self.seed = seed
        self.asana = asana
        self.at = wire.stamp(scenario.starts_at)
        self.people = {p.key: p for p in scenario.people}
        self.tickets = [t for t in scenario.tickets if t.provider == MANIFEST.key]
        declared = [p.name for p in seed.projects]
        extra = [SeedProject(name=n) for n in dict.fromkeys(t.project for t in self.tickets) if n not in declared]
        self.projects = [*seed.projects, *extra]

    def gid_of(self, ticket: SeededTicket) -> str:
        return seeded_gid(self.scenario, ticket)

    def user(self, person: str | None) -> str:
        if person is None:
            return AGENT_GID
        if person not in self.people:
            raise ValueError(f"the asana seed names the person {person!r}, who is not in the scenario")
        return state.user_gid(person)

    def everyone(self, members: list[str] | None, agent: bool) -> list[str]:
        keys = [p.key for p in self.scenario.people] if members is None else members
        return [*([AGENT_GID] if agent else []), *(self.user(k) for k in keys)]

    def write(self) -> None:
        self._workspace()
        self._users()
        self._teams()
        self._fields()
        self._tags()
        for project in self.projects:
            self._project(project)
        details = {t.ticket: t for t in self.seed.tasks}
        titles = [t.title for t in self.tickets]
        for title in details:
            if titles.count(title) != 1:
                raise ValueError(
                    f"the asana seed details the ticket {title!r}; {titles.count(title)} asana tickets have that title"
                )
        for ticket in self.tickets:
            self._task(ticket, details[ticket.title] if ticket.title in details else None)
        for ticket in self.tickets:
            self._comments(ticket, details.get(ticket.title))

    def _workspace(self) -> None:
        start = self.scenario.starts_at
        strict = bool(self.seed.tokens or self.seed.refresh_tokens)
        self.asana.put_record(
            limited(
                wire.AsanaWorkspace(
                    gid=WORKSPACE_GID,
                    name=self.seed.workspace.name,
                    is_organization=self.seed.workspace.organization,
                    email_domains=sorted({p.email.split("@")[-1] for p in self.scenario.people}),
                    premium=self.seed.workspace.premium,
                    unpaginated_limit=self.seed.workspace.unpaginated_limit,
                    strict_tokens=strict,
                    status=self._status(),
                    rate_limits=[
                        wire.RateWindow(start=wire.stamp(start + r.after), end=wire.stamp(start + r.after + r.lasts))
                        for r in self.seed.rate_limits
                    ],
                ),
                self.seed.limits,
            ),
            parent=state.WORKSPACES,
            actor=Actor.SCENARIO,
        )

    def _status(self) -> wire.StatusRule:
        match self.seed.status:
            case SeedCompleted():
                return wire.CompletedStatus()
            case SeedBySection():
                return wire.SectionStatus()
            case SeedByField():
                field = _by_name(self.seed.custom_fields, self.seed.status.field, "custom field")
                if field.kind != "enum":
                    raise ValueError(f"the status field {field.name!r} is {field.kind}, not enum")
                gid = state.field_gid(field.name)
                names = [o.name for o in field.options]
                for option in self.seed.status.means:
                    if option not in names:
                        raise ValueError(f"the status field {field.name!r} has no option {option!r}")
                return wire.FieldStatus(
                    field=gid, means={state.option_gid(gid, n): s for n, s in self.seed.status.means.items()}
                )

    def _users(self) -> None:
        self.asana.put_record(
            wire.AsanaUser(gid=AGENT_GID, name=self.seed.agent.name, email=self.seed.agent.email),
            parent=state.USERS,
            actor=Actor.SCENARIO,
        )
        for person in self.scenario.people:
            self.asana.put_record(
                wire.AsanaUser(gid=state.user_gid(person.key), name=person.name, email=person.email),
                parent=state.USERS,
                actor=Actor.SCENARIO,
            )
        for token in self.seed.tokens:
            expires = self.scenario.starts_at + token.expires_after if token.expires_after is not None else None
            self.asana.put_record(
                wire.AsanaCredential(
                    gid=state.credential_gid(token.token),
                    kind=wire.CredentialKind.ACCESS,
                    user=self.user(token.person),
                    expires_at=wire.stamp(expires) if expires is not None else None,
                ),
                parent=state.CREDENTIALS,
                actor=Actor.SCENARIO,
            )
        for refresh in self.seed.refresh_tokens:
            self.asana.put_record(
                wire.AsanaCredential(
                    gid=state.credential_gid(refresh.refresh_token),
                    kind=wire.CredentialKind.REFRESH,
                    user=self.user(refresh.person),
                ),
                parent=state.CREDENTIALS,
                actor=Actor.SCENARIO,
            )

    def _teams(self) -> None:
        if not self.seed.workspace.organization:
            if self.seed.teams:
                raise ValueError("the asana seed defines teams in a workspace that is not an organization")
            return
        teams = self.seed.teams or [SeedTeam(name=state.TEAM_NAME)]
        for team in teams:
            self.asana.put_record(
                wire.AsanaTeam(
                    gid=state.team_gid(team.name),
                    name=team.name,
                    workspace=WORKSPACE_GID,
                    members=self.everyone(team.members, team.agent),
                ),
                parent=state.TEAMS,
                actor=Actor.SCENARIO,
            )

    def _fields(self) -> None:
        for field in self.seed.custom_fields:
            gid = state.field_gid(field.name)
            self.asana.put_record(
                wire.AsanaCustomField(
                    gid=gid,
                    name=field.name,
                    subtype=field.kind,
                    enum_options=[
                        wire.AsanaEnumOption(
                            gid=state.option_gid(gid, o.name), name=o.name, color=o.color, enabled=o.enabled
                        )
                        for o in field.options
                    ],
                    precision=field.precision,
                    workspace=WORKSPACE_GID,
                ),
                parent=state.CUSTOM_FIELDS,
                actor=Actor.SCENARIO,
            )

    def _tags(self) -> None:
        for name in dict.fromkeys([*self.seed.tags, *(label for t in self.tickets for label in t.labels)]):
            self.asana.put_record(
                wire.AsanaTag(gid=state.tag_gid(name), name=name, workspace=WORKSPACE_GID, created_at=self.at),
                parent=state.TAGS,
                actor=Actor.SCENARIO,
            )

    def _team_of(self, project: SeedProject) -> str | None:
        if not self.seed.workspace.organization:
            if project.team is not None:
                raise ValueError(f"the project {project.name!r} names a team in a workspace with none")
            return None
        if project.team is None:
            return state.team_gid((self.seed.teams or [SeedTeam(name=state.TEAM_NAME)])[0].name)
        return state.team_gid(_by_name(self.seed.teams, project.team, "team").name)

    def _project(self, project: SeedProject) -> None:
        gid = state.project_gid(project.name)
        fields = [
            state.field_gid(_by_name(self.seed.custom_fields, f, "custom field").name) for f in project.custom_fields
        ]
        self.asana.put_record(
            wire.AsanaProject(
                gid=gid,
                name=project.name,
                notes=project.notes,
                archived=project.archived,
                workspace=WORKSPACE_GID,
                team=self._team_of(project),
                private=project.private,
                members=self.everyone(project.members, project.agent),
                custom_fields=fields,
                created_at=self.at,
            ),
            parent=state.PROJECTS,
            actor=Actor.SCENARIO,
        )
        sections = project.sections or [SeedSection(name=n, means=m) for n, m in state.DEFAULT_SECTIONS]
        for position, section in enumerate(sections):
            self.asana.put_record(
                wire.AsanaSection(
                    gid=state.section_gid(gid, position),
                    name=section.name,
                    project=gid,
                    means=section.means,
                    created_at=self.at,
                ),
                parent=gid,
                actor=Actor.SCENARIO,
            )

    def _ticket_by_title(self, title: str) -> SeededTicket:
        found = [t for t in self.tickets if t.title == title]
        if len(found) != 1:
            raise ValueError(f"the asana seed names the ticket {title!r}; {len(found)} asana tickets have that title")
        return found[0]

    def _task(self, ticket: SeededTicket, detail: SeedTask | None) -> None:
        project = state.project_gid(ticket.project)
        sections = self.asana.sections(project)
        parent = self._ticket_by_title(detail.parent) if detail is not None and detail.parent is not None else None
        if parent is ticket:
            raise ValueError(f"the asana seed makes {ticket.title!r} its own parent")
        first = sections[0].gid if sections else None
        if first is None and parent is None:
            raise ValueError(f"the asana project {ticket.project!r} has no section for {ticket.title!r} to be in")
        task = wire.AsanaTask(
            gid=self.gid_of(ticket),
            name=ticket.title,
            notes=ticket.body,
            assignee=self.user(ticket.assignee) if ticket.assignee is not None else None,
            created_by=self.user(self.scenario.owner),
            workspace=WORKSPACE_GID,
            parent=self.gid_of(parent) if parent is not None else None,
            memberships=[]
            if parent is not None or first is None
            else [wire.AsanaMembership(project=project, section=first)],
            created_at=self.at,
            modified_at=self.at,
        )
        task = self.asana.moved(task, ticket.state, now=self.scenario.starts_at)
        if detail is not None:
            task = self._detailed(task, ticket, detail)
        if ticket.labels:
            labelled = [*task.tags, *(state.tag_gid(label) for label in ticket.labels)]
            task = task.model_copy(update={"tags": list(dict.fromkeys(labelled))})
        self.asana.put_task(task, operation=Operation.CREATE, actor=Actor.SCENARIO)

    def _detailed(self, task: wire.AsanaTask, ticket: SeededTicket, detail: SeedTask) -> wire.AsanaTask:
        update: dict[str, object] = {}
        if detail.section is not None:
            if not task.memberships:
                raise ValueError(f"the subtask {ticket.title!r} is in no project, so it has no section")
            project = next(p for p in self.projects if p.name == ticket.project)
            names = [s.name for s in project.sections or [SeedSection(name=n) for n, _ in state.DEFAULT_SECTIONS]]
            if detail.section not in names:
                raise ValueError(f"the asana project {ticket.project!r} has no section {detail.section!r}")
            gid = state.project_gid(ticket.project)
            update["memberships"] = [
                wire.AsanaMembership(project=gid, section=state.section_gid(gid, names.index(detail.section)))
            ]
        if detail.completed is not None:
            update["completed"] = detail.completed
            update["completed_at"] = self.at if detail.completed else None
        if detail.due_after is not None:
            due = self.scenario.starts_at + detail.due_after
            update["due_on"] = due.date().isoformat()
            update["due_at"] = wire.stamp(due) if detail.due_has_time else None
        for name in detail.tags:
            if name not in self.seed.tags:
                raise ValueError(f"the asana seed tags {ticket.title!r} with {name!r}, which it does not define")
        update["tags"] = [state.tag_gid(n) for n in dict.fromkeys(detail.tags)]
        values = [v for v in task.custom_fields if v.field not in {state.field_gid(s.field) for s in detail.values}]
        carried = self.asana.fields_of(task.model_copy(update=update))
        for sent in detail.values:
            value = self._value(sent)
            if value.field not in carried:
                raise ValueError(f"{ticket.title!r} is in no project that carries the field {sent.field!r}")
            values.append(value)
        update["custom_fields"] = values
        return task.model_copy(update=update)

    def _value(self, sent: SeedValue) -> wire.AsanaFieldValue:
        field = _by_name(self.seed.custom_fields, sent.field, "custom field")
        gid = state.field_gid(field.name)
        options = [o.name for o in field.options]
        for name in [*([sent.option] if sent.option is not None else []), *sent.options]:
            if name not in options:
                raise ValueError(f"the field {field.name!r} has no option {name!r}")
        value = wire.AsanaFieldValue(field=gid)
        match field.kind:
            case "enum":
                return value.model_copy(
                    update={"option": state.option_gid(gid, sent.option) if sent.option is not None else None}
                )
            case "multi_enum":
                return value.model_copy(update={"options": [state.option_gid(gid, n) for n in sent.options]})
            case "text":
                return value.model_copy(update={"text": sent.text})
            case "number":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
                return value.model_copy(update={"number": sent.number})
            case "date":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
                on = self.scenario.starts_at + sent.date_after if sent.date_after is not None else None
                return value.model_copy(update={"date": on.date().isoformat() if on is not None else None})
            case "people":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
                return value.model_copy(update={"people": [self.user(k) for k in sent.people]})

    def _comments(self, ticket: SeededTicket, detail: SeedTask | None) -> None:
        """The seed's own comments, oldest first, then the ticket's shared comments, each a story by its person
        written as the scenario starts."""
        task = self.gid_of(ticket)
        place = _position(self.scenario, ticket)
        stories = iter(range(state.STORIES_PER_TASK))
        for comment in sorted(detail.comments if detail is not None else [], key=lambda c: -c.ago):
            written: datetime = self.scenario.starts_at - comment.ago
            self.asana.put_story(
                wire.AsanaStory(
                    gid=state.story_gid(place, next(stories)),
                    text=comment.text,
                    task=task,
                    created_by=self.user(comment.person),
                    created_at=wire.stamp(written),
                ),
                actor=Actor.SCENARIO,
            )
        for shared in ticket.comments:
            self.asana.put_story(
                wire.AsanaStory(
                    gid=state.story_gid(place, next(stories)),
                    text=shared.text,
                    task=task,
                    created_by=self.user(shared.by),
                    created_at=self.at,
                ),
                actor=Actor.SCENARIO,
            )
