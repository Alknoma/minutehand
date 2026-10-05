"""The site a scenario starts in.

Provider-neutral facts come from the scenario: its people become Atlassian accounts, its Jira tickets become
issues. Everything only Jira has comes from the scenario's Jira seed (`Scenario.provider_seed("jira")`), a
`JiraSeed` as JSON: the site's name and cloud id, the credentials that sign in and whose account each is,
projects with their keys, workflows, issue types, screens and members, custom fields, boards and sprints,
other accounts (an app, a deactivated person, a customer), what each seeded issue (named by its ticket's `key`) carries
beyond its title, body, assignee, state, labels and comments, and the calls the site answers 429.

Without a Jira seed the site is `minutehand.atlassian.net` and accepts no credential: every call is a 401
until a seed names one.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import date, timedelta
from typing import Self

from pydantic import Field, JsonValue, model_validator

from minutehand.adapters.providers.jira import wire
from minutehand.adapters.providers.jira.manifest import MANIFEST
from minutehand.adapters.providers.jira.moves import Desk
from minutehand.adapters.providers.jira.state import (
    JiraWorld,
    seeded_issue_id,
    seeded_link_id,
    seeded_project_id,
    ticket_project_id,
)
from minutehand.domain.scenario import Model, Scenario, SeededTicket, TicketState
from minutehand.domain.world import Actor
from minutehand.ports.store import Store

AGENT = "agent"
"""How a seed names the agent's own account."""

_ACCOUNTS = uuid.UUID("2b7f0d4e-91c3-4f55-8f0a-6d2c1e7b9a30")
_CLOUD = uuid.UUID("c41f0f6a-0b7e-4c1e-9d8f-3a5e2b6c7d18")
_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


class SeededAccount(Model):
    """An account that is not one of the scenario's people: an app, someone who left, a portal customer."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    email: str | None = None
    kind: wire.AccountType = wire.AccountType.ATLASSIAN
    active: bool = True
    email_visible: bool = True


class SeededOAuth(Model):
    access_token: str
    refresh_token: str
    client_id: str
    client_secret: str
    expires_in: int = Field(default=3600, ge=1)


class SeededCredential(Model):
    """One way in: an API token (sent as Basic with the account's email) or an OAuth 2.0 (3LO) grant."""

    account: str = Field(description="'agent', a Person.key or a SeededAccount.key")
    api_token: str | None = None
    oauth: SeededOAuth | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.api_token is None) == (self.oauth is None):
            raise ValueError("a credential is an api_token or an oauth grant, exactly one")
        return self


class SeededStatus(Model):
    name: str
    category: wire.Category
    outcome: TicketState | None = Field(
        default=None, description="What an issue in it is across providers; by default, done for a done category"
    )

    def meaning(self) -> TicketState:
        if self.outcome is not None:
            return self.outcome
        return TicketState.DONE if self.category is wire.Category.DONE else TicketState.OPEN


class SeededTransition(Model):
    name: str
    to: str = Field(description="A status name")
    sources: list[str] = Field(default=[], description="Status names it leaves from; empty: from any")
    id: str | None = None
    screen: list[str] = Field(default=[], description="Field ids on its screen ('resolution', 'customfield_…')")
    required: list[str] = []


class SeededIssueType(Model):
    name: str
    subtask: bool = False
    hierarchy: int | None = None
    fields: list[str] | None = Field(default=None, description="Field ids on its screens; None: every field")
    required: list[str] = []


class SeededSprint(Model):
    name: str
    state: wire.SprintState = wire.SprintState.ACTIVE
    goal: str = ""
    starts: timedelta | None = Field(default=None, description="Offset from the scenario's start")
    lasts: timedelta | None = None


class SeededBoard(Model):
    name: str
    type: str = "scrum"
    sprints: list[SeededSprint] = []


class SeededProject(Model):
    name: str = Field(description="As the scenario's tickets name it")
    key: str | None = Field(default=None, description="2-10 capitals and digits; derived from the name if absent")
    description: str = ""
    lead: str = AGENT
    issue_types: list[SeededIssueType] | None = None
    statuses: list[SeededStatus] | None = None
    transitions: list[SeededTransition] | None = Field(
        default=None, description="None: every status is reachable from every other, by a transition named for it"
    )
    administrators: list[str] | None = Field(default=None, description="None: the lead and the agent")
    members: list[str] | None = Field(default=None, description="None: every active Atlassian account")
    viewers: list[str] = []
    boards: list[SeededBoard] = []

    @model_validator(mode="after")
    def _key(self) -> Self:
        if self.key is not None and not _KEY.match(self.key):
            raise ValueError(f"project key {self.key!r} is not 2-10 capitals and digits starting with a letter")
        return self


class SeededField(Model):
    id: str = Field(pattern=r"^customfield_\d+$")
    name: str
    kind: wire.CustomFieldType
    options: list[str] = []


class SeededValue(Model):
    field: str = Field(description="A custom field's id or name")
    value: JsonValue


class SeededComment(Model):
    by: str = Field(description="'agent', a Person.key or a SeededAccount.key")
    text: str
    at: timedelta = Field(default=timedelta(0), description="Offset from the scenario's start; negative is before")


class SeededChange(Model):
    """A line of the issue's history from before the run: shown in its changelog, changing nothing now."""

    by: str
    at: timedelta
    field: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class SeededLink(Model):
    type: str = Field(description="Blocks, Duplicate, Relates or Cloners")
    to: str = Field(description="The key (`SeededTicket.key`) of another seeded Jira ticket")
    outward: bool = Field(default=True, description="This issue does the outward act: it blocks `to`")


class SeededIssue(Model):
    """What one seeded Jira ticket carries beyond the scenario's title, body, assignee, state, labels and comments."""

    ticket: str = Field(description="The key (`SeededTicket.key`) of the scenario's Jira ticket it describes")
    issue_type: str = "Task"
    status: str | None = Field(default=None, description="A status name; by default the first that means its state")
    priority: str | None = None
    due: date | None = None
    parent: str | None = Field(default=None, description="The key of another seeded Jira ticket, seeded before it")
    reporter: str | None = None
    created: timedelta = Field(default=timedelta(0), description="Offset from the scenario's start")
    fields: list[SeededValue] = []
    sprint: str | None = None
    estimate_seconds: int | None = None
    spent_seconds: int | None = None
    comments: list[SeededComment] = Field(
        default=[],
        description="Comments the shared `SeededTicket.comments` cannot say: by the agent or an account that is no "
        "person, or at a moment of their own",
    )
    history: list[SeededChange] = []
    links: list[SeededLink] = []


class JiraSeed(Model):
    site: str = Field(default="minutehand", pattern=r"^[a-z0-9][a-z0-9-]*$", description="<site>.atlassian.net")
    cloud_id: str | None = Field(default=None, description="By default derived from the site's name")
    agent_name: str = "Agent"
    agent_email: str = "agent@minutehand.invalid"
    agent_site_admin: bool = True
    accounts: list[SeededAccount] = []
    credentials: list[SeededCredential] = []
    projects: list[SeededProject] = []
    fields: list[SeededField] = []
    issues: list[SeededIssue] = []
    rate_limits: list[wire.StoredRateLimit] = []


def jira_seed(scenario: Scenario) -> JiraSeed:
    seed = scenario.provider_seed(MANIFEST.key)
    return JiraSeed() if seed is None else JiraSeed.model_validate_json(seed.body)


def account_id(email: str) -> str:
    """An Atlassian accountId for an email: the same in every run."""
    return f"712020:{uuid.uuid5(_ACCOUNTS, email.strip().lower())}"


def cloud_id(site: str) -> str:
    return str(uuid.uuid5(_CLOUD, site))


def project_key(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]", "", name).upper()[:10] or "PROJECT"
    if not base[0].isalpha():
        base = ("P" + base)[:10]
    if len(base) < 2:
        base = (base + "X")[:10]
    candidate, suffix = base, 2
    while candidate in taken:
        candidate = f"{base[: 10 - len(str(suffix))]}{suffix}"
        suffix += 1
    return candidate


DEFAULT_STATUSES = [
    SeededStatus(name="To Do", category=wire.Category.NEW),
    SeededStatus(name="In Progress", category=wire.Category.INDETERMINATE),
    SeededStatus(name="Done", category=wire.Category.DONE),
    SeededStatus(name="Won't Do", category=wire.Category.DONE, outcome=TicketState.CANCELLED),
]
DEFAULT_ISSUE_TYPES = [
    SeededIssueType(name="Epic", hierarchy=1),
    SeededIssueType(name="Task"),
    SeededIssueType(name="Story"),
    SeededIssueType(name="Bug"),
    SeededIssueType(name="Subtask", subtask=True, hierarchy=-1),
]
DEFAULT_FIELDS = [
    SeededField(id="customfield_10014", name="Epic Link", kind=wire.CustomFieldType.EPIC_LINK),
    SeededField(id="customfield_10016", name="Story point estimate", kind=wire.CustomFieldType.NUMBER),
    SeededField(id="customfield_10020", name="Sprint", kind=wire.CustomFieldType.SPRINT),
]
PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]
RESOLUTIONS = [("Done", "Work on this issue is finished."), ("Won't Do", "This will not be worked on."),
               ("Duplicate", "The problem is a duplicate of another issue.")]  # fmt: skip
LINK_TYPES = [
    ("Blocks", "is blocked by", "blocks"),
    ("Cloners", "is cloned by", "clones"),
    ("Duplicate", "is duplicated by", "duplicates"),
    ("Relates", "relates to", "relates to"),
]
ROLES = [
    wire.StoredRole(id="10002", name="Administrators", edits=True, administers=True),
    wire.StoredRole(id="10003", name="Member", edits=True),
    wire.StoredRole(id="10004", name="Viewer", edits=False),
]
SYSTEM_SCREEN = ["summary", "issuetype", "project", "description", "assignee", "reporter", "priority", "labels",
                 "duedate", "parent"]  # fmt: skip


def build_site(seed: JiraSeed) -> wire.StoredSite:
    """The site's own objects: every status a seeded project names (and the defaults), issue types, fields."""
    statuses: list[wire.StoredStatus] = []
    for status in [*DEFAULT_STATUSES, *(s for p in seed.projects for s in p.statuses or [])]:
        if any(s.name.lower() == status.name.lower() for s in statuses):
            continue
        statuses.append(
            wire.StoredStatus(
                id=str(10000 + len(statuses)), name=status.name, category=status.category, outcome=status.meaning()
            )
        )
    types: list[wire.StoredIssueType] = []
    for issue_type in [*DEFAULT_ISSUE_TYPES, *(t for p in seed.projects for t in p.issue_types or [])]:
        if any(t.name.lower() == issue_type.name.lower() for t in types):
            continue
        level = issue_type.hierarchy if issue_type.hierarchy is not None else (-1 if issue_type.subtask else 0)
        types.append(
            wire.StoredIssueType(
                id=str(10001 + len(types)), name=issue_type.name, subtask=issue_type.subtask, hierarchyLevel=level
            )
        )
    fields = {f.id: f for f in DEFAULT_FIELDS} | {f.id: f for f in seed.fields}
    stored_fields = [
        wire.StoredField(
            id=f.id,
            name=f.name,
            kind=f.kind,
            options=[wire.StoredOption(id=str(10100 + n), value=o) for n, o in enumerate(f.options)],
        )
        for f in fields.values()
    ]
    sprint_field = next(f.id for f in stored_fields if f.kind is wire.CustomFieldType.SPRINT)
    return wire.StoredSite(
        name=seed.site,
        cloudId=seed.cloud_id or cloud_id(seed.site),
        agent=account_id(seed.agent_email),
        statuses=statuses,
        issueTypes=types,
        priorities=[wire.StoredPriority(id=str(n + 1), name=p) for n, p in enumerate(PRIORITIES)],
        defaultPriority="3",
        resolutions=[
            wire.StoredResolution(id=str(10000 + n), name=name, description=text)
            for n, (name, text) in enumerate(RESOLUTIONS)
        ],
        fields=stored_fields,
        linkTypes=[
            wire.StoredLinkType(id=str(10000 + n), name=name, inward=inward, outward=outward)
            for n, (name, inward, outward) in enumerate(LINK_TYPES)
        ],
        roles=ROLES,
        sprintField=sprint_field,
        rateLimits=seed.rate_limits,
    )


def default_project(
    site: wire.StoredSite,
    project_id: str,
    key: str,
    name: str,
    *,
    lead: str,
    members: list[str],
    administrators: list[str],
    description: str = "",
    type_names: list[str] | None = None,
) -> wire.StoredProject:
    """A project on the default workflow: To Do, In Progress, Done and Won't Do, each reachable from any."""
    return project_from(
        site,
        SeededProject(name=name, key=key, description=description),
        project_id,
        key,
        lead=lead,
        members=members,
        administrators=administrators,
        viewers=[],
        type_names=type_names,
    )


def project_from(
    site: wire.StoredSite,
    seeded: SeededProject,
    project_id: str,
    key: str,
    *,
    lead: str,
    members: list[str],
    administrators: list[str],
    viewers: list[str],
    type_names: list[str] | None = None,
) -> wire.StoredProject:
    custom = [f.id for f in site.fields]
    types = seeded.issue_types or [t for t in DEFAULT_ISSUE_TYPES if type_names is None or t.name in type_names]
    screens = []
    for issue_type in types:
        stored = next(t for t in site.issueTypes if t.name.lower() == issue_type.name.lower())
        on = issue_type.fields if issue_type.fields is not None else [*SYSTEM_SCREEN, *custom]
        required = ["summary", "issuetype", "project", *issue_type.required]
        if stored.subtask:
            required.append("parent")
        screens.append(wire.StoredScreen(issueType=stored.id, fields=on, required=required))
    names = [s.name for s in seeded.statuses or DEFAULT_STATUSES]
    status_ids = [next(s.id for s in site.statuses if s.name.lower() == n.lower()) for n in names]

    def status_id(name: str) -> str:
        found = next((s.id for s in site.statuses if s.name.lower() == name.lower() and s.id in status_ids), None)
        if found is None:
            raise ValueError(f"project {key} has no status {name!r}")
        return found

    if seeded.transitions is None:
        transitions = [
            wire.StoredTransition(id=str(11 + 10 * n), name=name, to=status_ids[n]) for n, name in enumerate(names)
        ]
    else:
        transitions = [
            wire.StoredTransition(
                id=t.id or str(11 + 10 * n),
                name=t.name,
                to=status_id(t.to),
                sources=[status_id(s) for s in t.sources],
                screen=t.screen,
                required=t.required,
            )
            for n, t in enumerate(seeded.transitions)
        ]
    return wire.StoredProject(
        id=project_id,
        key=key,
        name=seeded.name,
        description=seeded.description,
        lead=lead,
        screens=screens,
        statuses=status_ids,
        transitions=transitions,
        members=[
            wire.StoredMembers(role="10002", accounts=administrators),
            wire.StoredMembers(role="10003", accounts=members),
            wire.StoredMembers(role="10004", accounts=viewers),
        ],
    )


def seed(scenario: Scenario, world: Store) -> None:
    jira = JiraWorld(world)
    spec = jira_seed(scenario)
    site = build_site(spec)
    jira.write_site(site, actor=Actor.SCENARIO)

    accounts: dict[str, wire.StoredUser] = {
        AGENT: wire.StoredUser(
            accountId=site.agent,
            displayName=spec.agent_name,
            emailAddress=spec.agent_email,
            siteAdmin=spec.agent_site_admin,
        )
    }
    for person in scenario.people:
        accounts[person.key] = wire.StoredUser(
            accountId=account_id(person.email), displayName=person.name, emailAddress=person.email
        )
    for extra in spec.accounts:
        email = extra.email or f"{extra.key}@{spec.site}.invalid"
        accounts[extra.key] = wire.StoredUser(
            accountId=account_id(email),
            displayName=extra.name,
            emailAddress=email,
            emailVisible=extra.email_visible,
            accountType=extra.kind,
            active=extra.active,
        )
    for account in accounts.values():
        jira.write_user(account, actor=Actor.SCENARIO)

    def who(name: str) -> str:
        if name not in accounts:
            raise ValueError(f"the Jira seed names {name!r}, who is not the agent, a person or a seeded account")
        return accounts[name].accountId

    for n, credential in enumerate(spec.credentials):
        oauth = credential.oauth
        if oauth is None:
            stored = wire.StoredCredential(
                id=str(n + 1),
                kind=wire.CredentialKind.API_TOKEN,
                account=who(credential.account),
                secret=credential.api_token or "",
            )
        else:
            stored = wire.StoredCredential(
                id=str(n + 1),
                kind=wire.CredentialKind.OAUTH,
                account=who(credential.account),
                secret=oauth.access_token,
                refreshToken=oauth.refresh_token,
                clientId=oauth.client_id,
                clientSecret=oauth.client_secret,
                issued=scenario.starts_at,
                lifetime=oauth.expires_in,
            )
        jira.write_credential(stored, actor=Actor.SCENARIO, create=True)

    everyone = [a.accountId for a in accounts.values() if a.active and a.accountType is wire.AccountType.ATLASSIAN]
    seeded_tickets = [t for t in scenario.tickets if t.provider == MANIFEST.key]
    declared = [p.name for p in spec.projects]
    named = list(dict.fromkeys(t.project for t in seeded_tickets if t.project not in declared))
    wanted = [(name, seeded_project_id(n)) for n, name in enumerate(declared)]
    wanted += [(name, ticket_project_id(n)) for n, name in enumerate(named)]
    projects: dict[str, wire.StoredProject] = {}
    taken: set[str] = set()
    for name, project_id in wanted:
        given = next((p for p in spec.projects if p.name == name), SeededProject(name=name))
        key = given.key or project_key(name, taken)
        if key in taken:
            raise ValueError(f"two seeded Jira projects have the key {key}")
        taken.add(key)
        lead = who(given.lead)
        administrators = (
            [who(a) for a in given.administrators] if given.administrators is not None else [lead, site.agent]
        )
        made = project_from(
            site,
            given,
            project_id,
            key,
            lead=lead,
            members=[who(m) for m in given.members] if given.members is not None else everyone,
            administrators=list(dict.fromkeys(administrators)),
            viewers=[who(v) for v in given.viewers],
        )
        jira.write_project(made, actor=Actor.SCENARIO)
        projects[name] = made
        for board in given.boards:
            board_id = len(jira.boards()) + 1
            jira.write_board(
                wire.StoredBoard(
                    id=board_id, name=board.name, project=made.id, type="kanban" if board.type == "kanban" else "scrum"
                ),
                actor=Actor.SCENARIO,
            )
            for sprint in board.sprints:
                starts = scenario.starts_at + sprint.starts if sprint.starts is not None else None
                jira.write_sprint(
                    wire.StoredSprint(
                        id=len(jira.sprints()) + 1,
                        board=board_id,
                        name=sprint.name,
                        state=sprint.state,
                        goal=sprint.goal,
                        startDate=starts,
                        endDate=starts + sprint.lasts if starts is not None and sprint.lasts is not None else None,
                    ),
                    actor=Actor.SCENARIO,
                )

    desk = Desk(world)
    keys = {t.key for t in seeded_tickets if t.key is not None}
    for detail in spec.issues:
        if detail.ticket not in keys:
            raise ValueError(f"the Jira seed describes {detail.ticket!r}, which is the key of no seeded Jira ticket")
    by_key: dict[str, wire.StoredIssue] = {}
    commented: dict[str, int] = {}
    for position, ticket in enumerate(scenario.tickets):
        if ticket.provider != MANIFEST.key:
            continue
        found = next((i for i in spec.issues if i.ticket == ticket.key), None) if ticket.key is not None else None
        detail = found or SeededIssue(ticket=ticket.key or "")
        made = _issue(desk, site, projects[ticket.project], ticket, detail, scenario, who, by_key, position)
        for n, comment in enumerate(ticket.comments):
            desk.comment(made, wire.adf_from_text(comment.text), by=who(comment.by), at=scenario.starts_at,
                         actor=Actor.SCENARIO, seeded=n)  # fmt: skip
        commented[made.id] = len(ticket.comments)
        if ticket.key is not None:
            by_key[ticket.key] = made
    linked = 0
    for detail in spec.issues:
        issue = by_key[detail.ticket]
        for comment in detail.comments:
            desk.comment(issue, wire.adf_from_text(comment.text), by=who(comment.by), at=scenario.starts_at + comment.at,
                         actor=Actor.SCENARIO, seeded=commented[issue.id])  # fmt: skip
            commented[issue.id] += 1
        for link in detail.links:
            other = by_key.get(link.to)
            if other is None:
                raise ValueError(f"{detail.ticket!r} links to {link.to!r}, which is the key of no seeded Jira ticket")
            link_type = next((t for t in site.linkTypes if t.name.lower() == link.type.lower()), None)
            if link_type is None:
                raise ValueError(f"no issue link type is called {link.type!r}")
            source, destination = (issue, other) if link.outward else (other, issue)
            jira.write_link(
                wire.StoredLink(
                    id=seeded_link_id(linked), type=link_type.id, source=source.id, destination=destination.id
                ),
                actor=Actor.SCENARIO,
            )
            linked += 1


def _issue(
    desk: Desk,
    site: wire.StoredSite,
    project: wire.StoredProject,
    ticket: SeededTicket,
    detail: SeededIssue,
    scenario: Scenario,
    who: Callable[[str], str],
    earlier: dict[str, wire.StoredIssue],
    position: int,
) -> wire.StoredIssue:
    jira = desk.world
    issue_type = next((t for t in site.issueTypes if t.name.lower() == detail.issue_type.lower()), None)
    if issue_type is None or project.screen(issue_type.id) is None:
        raise ValueError(f"project {project.key} has no issue type {detail.issue_type!r}")
    if detail.status is not None:
        status = next(
            (site.status(s) for s in project.statuses if site.status(s).name.lower() == detail.status.lower()), None
        )
        if status is None:
            raise ValueError(f"project {project.key} has no status {detail.status!r}")
    else:
        status = next((site.status(s) for s in project.statuses if site.status(s).outcome is ticket.state), None)
        if status is None:
            raise ValueError(f"project {project.key} has no status that is {ticket.state.value}")
    priority = site.defaultPriority
    if detail.priority is not None:
        found = next((p for p in site.priorities if p.name.lower() == detail.priority.lower()), None)
        if found is None:
            raise ValueError(f"no priority is called {detail.priority!r}")
        priority = found.id
    custom: list[wire.StoredValue] = []
    for value in detail.fields:
        field = next((f for f in site.fields if value.field in (f.id, f.name)), None)
        if field is None:
            raise ValueError(f"no custom field is {value.field!r}")
        custom.append(wire.StoredValue(field=field.id, value=desk.stored_value(field, value.value)))
    if detail.sprint is not None:
        sprint = next((s for s in jira.sprints() if s.name == detail.sprint), None)
        if sprint is None:
            raise ValueError(f"no seeded sprint is called {detail.sprint!r}")
        custom.append(wire.StoredValue(field=site.sprintField, value=[sprint.id]))
    parent: str | None = None
    if detail.parent is not None:
        if detail.parent not in earlier:
            raise ValueError(f"{detail.ticket!r} has parent {detail.parent!r}, which is not seeded before it")
        parent = earlier[detail.parent].id
    created = scenario.starts_at + detail.created
    reporter = who(detail.reporter) if detail.reporter is not None else who(scenario.owner)
    history = [
        wire.StoredHistory(
            id=str(n + 1),
            author=who(change.by),
            created=scenario.starts_at + change.at,
            items=[
                wire.StoredItem(field=change.field, fieldId=change.field, fromString=change.from_, toString=change.to)
            ],
        )
        for n, change in enumerate(detail.history)
    ]
    number = jira.next_number(project.id)
    issue = wire.StoredIssue(
        id=seeded_issue_id(position),
        key=f"{project.key}-{number}",
        project=project.id,
        issuetype=issue_type.id,
        summary=ticket.title,
        description=wire.adf_from_text(ticket.body) if ticket.body else None,
        status=status.id,
        resolution=desk.resolution_for(status) if status.category is wire.Category.DONE else None,
        resolutiondate=created if status.category is wire.Category.DONE else None,
        priority=priority,
        assignee=who(ticket.assignee) if ticket.assignee is not None else None,
        reporter=reporter,
        creator=reporter,
        created=created,
        updated=max([created, *(h.created for h in history)]),
        duedate=detail.due,
        labels=list(dict.fromkeys(ticket.labels)),
        parent=parent,
        custom=custom,
        originalEstimateSeconds=detail.estimate_seconds,
        timeSpentSeconds=detail.spent_seconds,
        history=history,
        seededFrom=position,
    )
    jira.create_issue(issue, actor=Actor.SCENARIO)
    return issue
