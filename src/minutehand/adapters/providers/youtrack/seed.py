"""The instance a scenario starts in: its people and the agent's account, a project per
distinct seeded project, and the seeded issues.

Every project carries a State field (Open, In Progress, Fixed, Won't fix) and an
Assignee field whose team is every user, so the agent can hand an issue to anyone in
the scenario. The agent's account leads every project.
"""

from __future__ import annotations

import re
import uuid

from minutehand.adapters.providers.youtrack import state, wire
from minutehand.adapters.providers.youtrack.manifest import MANIFEST
from minutehand.adapters.providers.youtrack.state import YouTrackWorld
from minutehand.domain.scenario import Scenario, TicketState
from minutehand.domain.world import Actor
from minutehand.ports.store import Store

STATE_DEFINITION = "58-1"
ASSIGNEE_DEFINITION = "58-2"

_STATES: list[tuple[str, bool, TicketState]] = [
    ("Open", False, TicketState.OPEN),
    ("In Progress", False, TicketState.OPEN),
    ("Fixed", True, TicketState.DONE),
    ("Won't fix", True, TicketState.CANCELLED),
]
_SHORT_NAME_MAX = 10
_RING = uuid.UUID("6f1c3a52-6d3b-4b8e-9a51-2f0f6c1d7e40")


def ring_id(login: str) -> str:
    """The user's Hub id: the same for the same login in every run."""
    return str(uuid.uuid5(_RING, login))


def user(user_id: str, login: str, full_name: str, email: str | None) -> wire.StoredUser:
    return wire.StoredUser(id=user_id, login=login, fullName=full_name, email=email, ringId=ring_id(login))


def short_name(name: str, taken: set[str]) -> str:
    """A project key from its name: its letters and digits, upper-cased, made unique with a digit."""
    base = re.sub(r"[^A-Za-z0-9]", "", name).upper()[:_SHORT_NAME_MAX] or "PROJECT"
    if not base[0].isalpha():
        base = ("P" + base)[:_SHORT_NAME_MAX]
    candidate, suffix = base, 2
    while candidate in taken:
        candidate = f"{base[: _SHORT_NAME_MAX - len(str(suffix))]}{suffix}"
        suffix += 1
    return candidate


def project(index: int, name: str, key: str, *, leader: str, team: list[str]) -> wire.StoredProject:
    states = [
        wire.StoredState(
            id=f"62-{index * len(_STATES) + n}", name=label, isResolved=resolved, ordinal=n, outcome=outcome
        )
        for n, (label, resolved, outcome) in enumerate(_STATES)
    ]
    return wire.StoredProject(
        id=f"0-{index}",
        shortName=key,
        name=name,
        leader=leader,
        team=team,
        stateField=f"92-{2 * index + 1}",
        stateFieldDefinition=STATE_DEFINITION,
        stateBundle=f"60-{index}",
        states=states,
        assigneeField=f"92-{2 * index + 2}",
        assigneeFieldDefinition=ASSIGNEE_DEFINITION,
        assigneeBundle=f"61-{index}",
        teamGroup=f"3-{index}",
    )


def seed(scenario: Scenario, world: Store) -> None:
    youtrack = YouTrackWorld(world)
    agent = user("1-0", state.AGENT_LOGIN, state.AGENT_NAME, state.AGENT_EMAIL)
    people = {p.key: user(f"1-{n + 1}", p.key, p.name, p.email) for n, p in enumerate(scenario.people)}
    for account in [agent, *people.values()]:
        youtrack.write_user(account, actor=Actor.SCENARIO)

    team = [agent.id, *(u.id for u in people.values())]
    seeded = [t for t in scenario.tickets if t.provider == MANIFEST.key]
    projects: dict[str, wire.StoredProject] = {}
    for ticket in seeded:
        if ticket.project in projects:
            continue
        key = short_name(ticket.project, {p.shortName for p in projects.values()})
        made = project(len(projects), ticket.project, key, leader=agent.id, team=team)
        projects[ticket.project] = made
        youtrack.write_project(made, actor=Actor.SCENARIO)

    at = state.millis(scenario.starts_at)
    reporter = people[scenario.owner].id
    for ticket in seeded:
        home = projects[ticket.project]
        number = youtrack.next_number(home.id)
        settled = youtrack.state_for(home, ticket.state)
        youtrack.create_issue(
            wire.StoredIssue(
                id=youtrack.next_id(2),
                idReadable=f"{home.shortName}-{number}",
                numberInProject=number,
                project=home.id,
                summary=ticket.title,
                description=ticket.body or None,
                reporter=reporter,
                updater=reporter,
                created=at,
                updated=at,
                resolved=at if settled.isResolved else None,
                state=settled.id,
                assignee=people[ticket.assignee].id if ticket.assignee is not None else None,
            ),
            actor=Actor.SCENARIO,
        )
