"""A person is the account their entry declares (`Person.accounts`): a login such as `john.smith` no person key can
spell, a database id, a hidden or absent email. Acting, assigning, deactivating and granting find that account by who
the person is. A seeded issue may declare its number (`BACKEND-142`) and its database id (`2-5`); the next issue
takes the number after the highest declared."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.youtrack import state
from minutehand.adapters.providers.youtrack.provider import YouTrackProvider, build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.people import PermissionGrant
from minutehand.domain.provider import PersonChange
from minutehand.domain.scenario import (
    Account,
    Comments,
    Person,
    PersonAccount,
    ProviderSeed,
    Reassigns,
    Scenario,
    SeededTicket,
    TicketHappening,
)
from minutehand.domain.world import Change, TicketSnapshot

START = datetime(2026, 8, 24, 10, 50, 3, tzinfo=UTC)
TOKEN = "perm:am9obi5zbWl0aA==.dGVzdA==.dG9rZW4="
JOHN = Person(
    key="john",
    name="John Smith",
    email="john.smith@example.com",
    accounts=[PersonAccount(provider="youtrack", login="john.smith", id="1-77", name="J. Smith")],
)
ROBOT = Person(key="robot", name="Build Robot")
SHY = Person(
    key="shy",
    name="Shy Person",
    email="shy@example.com",
    accounts=[PersonAccount(provider="youtrack", email_visible=False)],
)


def _scenario(tickets: list[SeededTicket], *, people: list[Person] | None = None) -> Scenario:
    return Scenario(
        name="people_and_ids",
        goal="g",
        owner="john",
        starts_at=START,
        people=people or [JOHN, ROBOT, SHY],
        tickets=tickets,
        provider_seeds=[
            ProviderSeed(provider="youtrack", body=json.dumps({"tokens": [{"token": TOKEN, "login": "john.smith"}]}))
        ],
    )


SCENARIO = _scenario(
    [
        SeededTicket(provider="youtrack", project="Backend", title="Old crash", number=142, id="2-5", assignee="john"),
        SeededTicket(provider="youtrack", project="Backend", title="Unnumbered"),
    ]
)


class World:
    def __init__(self, path: Path, scenario: Scenario = SCENARIO) -> None:
        self.clock = RunClock(START)
        self.store = SqliteStore(path, "root", self.clock)
        self.provider: YouTrackProvider = build()
        self.provider.seed(scenario, self.store)
        self.scenario = scenario


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "world.db")


@pytest.fixture
async def yt(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.provider.app(world.store, world.clock)),
        base_url="https://acme.youtrack.cloud",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield client


async def _read(client: httpx.AsyncClient, path: str, fields: str) -> dict[str, object]:
    answered = await client.get(path, params={"fields": fields})
    assert answered.status_code == 200, answered.text
    found = json.loads(answered.text)
    assert isinstance(found, dict)
    return found


async def _users(client: httpx.AsyncClient) -> dict[str, dict[str, object]]:
    answered = await client.get("/api/users", params={"fields": "id,login,fullName,email"})
    assert answered.status_code == 200, answered.text
    return {u["login"]: u for u in json.loads(answered.text)}


def _issue(world: World, readable: str) -> str:
    found = state.YouTrackWorld(world.store).find_issue(readable)
    assert found is not None
    return found.id


async def test_a_person_is_the_login_id_and_name_their_entry_declares(yt: httpx.AsyncClient) -> None:
    users = await _users(yt)

    assert users["john.smith"] == {
        "id": "1-77",
        "login": "john.smith",
        "fullName": "J. Smith",
        "email": "john.smith@example.com",
        "$type": "User",
    }
    assert "john" not in users


async def test_a_person_without_an_email_or_with_it_hidden_shows_none(yt: httpx.AsyncClient) -> None:
    users = await _users(yt)

    assert users["robot"]["email"] is None
    assert users["shy"]["email"] is None
    found = await yt.get("/api/users", params={"fields": "login", "query": "shy@example.com"})
    assert json.loads(found.text) == []


async def test_a_declared_number_and_id_are_the_issues_and_the_next_issue_follows_the_highest(
    yt: httpx.AsyncClient, world: World
) -> None:
    declared = await _read(yt, "/api/issues/BACKEND-142", "id,idReadable,numberInProject")
    unnumbered = await _read(yt, "/api/issues/BACKEND-1", "summary")
    project = state.YouTrackWorld(world.store).project_named("BACKEND")
    assert project is not None
    made = await yt.post(
        "/api/issues", params={"fields": "idReadable"}, json={"project": {"id": project.id}, "summary": "New"}
    )

    assert declared == {"id": "2-5", "idReadable": "BACKEND-142", "numberInProject": 142, "$type": "Issue"}
    assert unnumbered["summary"] == "Unnumbered"
    assert json.loads(made.text)["idReadable"] == "BACKEND-143"


def test_the_snapshot_names_the_assignee_by_key(world: World) -> None:
    found = state.YouTrackWorld(world.store).find_issue("BACKEND-142")
    assert found is not None

    assert state.YouTrackWorld(world.store).snapshot(found) == TicketSnapshot(
        title="Old crash", project="BACKEND", assignee_email="john.smith@example.com", assignee="john"
    )


async def test_a_person_with_a_declared_login_acts_is_assigned_and_deactivated(
    yt: httpx.AsyncClient, world: World
) -> None:
    issue = _issue(world, "BACKEND-1")
    world.provider.edit(state.issue_ref(issue), state=None, assignee=JOHN, world=world.store, clock=world.clock)
    assigned = world.store.events()[-1].after
    happening = TicketHappening(person="john", ticket="Unnumbered", after=timedelta(0), action=Comments(text="On it"))
    world.provider.act(happening, world.scenario, world.store, world.clock)
    reassigned = TicketHappening(person="john", ticket="Unnumbered", after=timedelta(0), action=Reassigns(to="shy"))
    world.provider.act(reassigned, world.scenario, world.store, world.clock)
    after_reassign = world.store.events()[-1].after
    comments = await yt.get(f"/api/issues/{issue}/comments", params={"fields": "text,author(login)"})
    world.provider.permit(
        PermissionGrant(person="john", permission="jetbrains.youtrack.createIssue", held=False),
        JOHN,
        world.store,
        world.clock,
    )
    users = await _users(yt)
    world.provider.change_person(PersonChange.DEACTIVATED, JOHN, world.store, world.clock)
    banned = state.YouTrackWorld(world.store).user("1-77")
    refused = await yt.get("/api/users/me", params={"fields": "login"})

    assert isinstance(assigned, TicketSnapshot) and assigned.assignee == "john"
    assert isinstance(after_reassign, TicketSnapshot) and after_reassign.assignee == "shy"
    assert json.loads(comments.text)[-1]["author"]["login"] == "john.smith"
    assert state.YouTrackWorld(world.store).grants()[-1].user == "1-77"
    assert banned is not None and banned.banned and "john.smith" in users
    assert refused.status_code == 401


def test_a_seed_users_entry_sharing_a_persons_login_is_refused(tmp_path: Path) -> None:
    scenario = _scenario([]).model_copy(
        update={
            "provider_seeds": [
                ProviderSeed(provider="youtrack", body=json.dumps({"users": [{"login": "john.smith", "name": "J"}]}))
            ]
        }
    )
    with pytest.raises(ValueError, match="declare it once, as a person's account"):
        World(tmp_path / "world.db", scenario)


@pytest.mark.parametrize(
    ("account", "match"),
    [
        (PersonAccount(provider="youtrack", login="john smith"), "is not a login"),
        (PersonAccount(provider="youtrack", id="2-77"), "is not a user's database id"),
        (PersonAccount(provider="youtrack", id="1-2"), "share the id"),
    ],
)
def test_a_login_or_id_not_in_youtracks_format_or_taken_is_refused(
    tmp_path: Path, account: PersonAccount, match: str
) -> None:
    person = JOHN.model_copy(update={"accounts": [account]})
    with pytest.raises(ValueError, match=match):
        World(tmp_path / "world.db", _scenario([], people=[person, ROBOT, SHY]))


@pytest.mark.parametrize(("declared", "match"), [("5", "not an issue's"), ("1-5", "not an issue's")])
def test_a_declared_issue_id_not_in_youtracks_format_is_refused(tmp_path: Path, declared: str, match: str) -> None:
    ticket = SeededTicket(provider="youtrack", project="Backend", title="Bad", id=declared)
    with pytest.raises(ValueError, match=match):
        World(tmp_path / "world.db", _scenario([ticket]))


def test_an_undeclared_ticket_skips_the_number_another_declares(tmp_path: Path) -> None:
    scenario = _scenario(
        [
            SeededTicket(provider="youtrack", project="Backend", title="First"),
            SeededTicket(provider="youtrack", project="Backend", title="Second"),
            SeededTicket(provider="youtrack", project="Backend", title="Two", number=2),
        ]
    )
    found = World(tmp_path / "world.db", scenario)
    youtrack = state.YouTrackWorld(found.store)

    assert sorted((i.summary, i.numberInProject) for i in youtrack.every_issue()) == [
        ("First", 1),
        ("Second", 3),
        ("Two", 2),
    ]


def _further(tmp_path: Path, world: World, ticket: SeededTicket) -> list[Change]:
    """What seeding the scenario grown by `ticket` writes that seeding it before did not, placed in `world`."""
    before = SqliteStore(tmp_path / "before.db", "before", RunClock(START))
    after = SqliteStore(tmp_path / "after.db", "after", RunClock(START))
    build().seed(world.scenario, before)
    build().seed(world.scenario.model_copy(update={"tickets": [*world.scenario.tickets, ticket]}), after)
    held = {(e.entity, e.after) for e in before.events()}
    added = [
        Change(entity=s.entity, operation=e.operation, actor=e.actor, body=s.body, parent=s.parent)
        for e in after.events()
        if (e.entity, e.after) not in held and (s := after.get(e.entity)) is not None
    ]
    return world.provider.place(added, world.store)


def test_a_further_ticket_keeps_its_declared_number(tmp_path: Path, world: World) -> None:
    placed = _further(tmp_path, world, SeededTicket(provider="youtrack", project="Backend", title="Late", number=7))

    assert "BACKEND-7" in {c.entity.external_id for c in placed}


async def test_a_further_ticket_declaring_a_number_the_agent_has_taken_is_refused(
    tmp_path: Path, world: World, yt: httpx.AsyncClient
) -> None:
    project = state.YouTrackWorld(world.store).project_named("BACKEND")
    assert project is not None
    made = await yt.post(
        "/api/issues", params={"fields": "idReadable"}, json={"project": {"id": project.id}, "summary": "Agent's"}
    )
    assert json.loads(made.text)["idReadable"] == "BACKEND-143"

    with pytest.raises(ValueError, match="already handed out"):
        _further(tmp_path, world, SeededTicket(provider="youtrack", project="Backend", title="Late", number=143))


def test_a_deactivated_person_is_seeded_banned(tmp_path: Path) -> None:
    gone = Person(key="gone", name="Gone Person", account=Account.DEACTIVATED)
    found = World(tmp_path / "world.db", _scenario([], people=[JOHN, gone]))

    user = state.YouTrackWorld(found.store).user_of("gone")
    assert user is not None and user.banned
