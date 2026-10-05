"""Who a person is in Jira, and what a seed may say an issue is.

A person with no email is an Atlassian account whose email is not shown, as Jira shows one whose owner hid it; a
person's `accounts` entry for Jira gives the account its accountId, display name and hidden email; everything the
world does to a person's account finds it by who they are, never by their email. A seeded ticket may declare its
number in its project (`BACKEND-142`) and its issue id, and what is created afterwards is numbered past the highest
declared."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from minutehand.adapters.providers.jira import state
from minutehand.adapters.providers.jira.provider import build
from minutehand.adapters.providers.jira.seed import account_id, person_account_id
from minutehand.adapters.providers.jira.state import JiraWorld
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld, WorldRefused
from minutehand.domain.provider import PersonChange
from minutehand.domain.scenario import (
    Person,
    Reassigns,
    Scenario,
    Seed,
    SeededTicket,
    TicketHappening,
)
from minutehand.domain.world import EntityKind, TicketSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ActsOnTickets
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
TOKEN = "jira-people-token"
EMAIL = "agent@people.example"
API = "https://people.atlassian.net/rest/api/3"
DECLARED_ACCOUNT = "5b10ac8d82e05b22cc7d4ef5"

PEOPLE: list[dict[str, Any]] = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "svc", "name": "Build Service", "reply": {"kind": "silent"}},
    {
        "key": "john",
        "name": "John Smith",
        "email": "john@example.com",
        "reply": {"kind": "silent"},
        "accounts": [{"provider": "jira", "id": DECLARED_ACCOUNT, "name": "J. Smith", "email_visible": False}],
    },
]
JIRA: dict[str, Any] = {
    "site": "people",
    "agent_email": EMAIL,
    "credentials": [{"account": "agent", "api_token": TOKEN}],
}


def _seed(**changed: Any) -> dict[str, Any]:
    written: dict[str, Any] = {
        "starts_at": START.isoformat(),
        "people": PEOPLE,
        "tickets": [
            {"provider": "jira", "project": "Backend", "title": "Recorded", "number": 142, "id": "10042",
             "assignee": "svc"},
            {"provider": "jira", "project": "Backend", "title": "Undeclared", "assignee": "john"},
        ],
        "provider_seeds": [{"provider": "jira", "body": json.dumps(JIRA)}],
    }  # fmt: skip
    written.update(changed)
    return written


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


@contextmanager
def _opened(directory: Path, **changed: Any) -> Iterator[StandingWorld]:
    seed = Seed.model_validate(_seed(**changed)).starting(START)
    registry = Registry.installed()
    manifest = next(m for m in registry.manifests if m.key == "jira")
    clock = RunClock(seed.starts_at)
    store = SqliteStore(directory / "world.db", "world", clock)
    world = StandingWorld(
        scenario=seed,
        store=store,
        clock=clock,
        provider=lambda _: registry.provider(manifest),
        inbound=[],
        signing={},
        scripted=False,
    )
    world.open(["jira"])
    try:
        yield world
    finally:
        store.close()


def _client(world: StandingWorld) -> httpx.AsyncClient:
    app = world.app_for(world.provider("jira").manifest)
    basic = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), headers={"Authorization": f"Basic {basic}"}, timeout=30
    )


def _seeding_refused(tmp_path: Path, match: str, **changed: Any) -> None:
    scenario = Scenario.model_validate({"name": "s", "goal": "g", "owner": "owen", **_seed(**changed)})
    with _scratch(tmp_path / "refused.db", RunClock(START)) as store, pytest.raises(ValueError, match=match):
        build().seed(scenario, store)


def _person(world: StandingWorld, key: str) -> Person:
    return next(p for p in world.scenario.people if p.key == key)


# --------------------------------------------------------------------------- people


async def test_a_person_without_an_email_is_an_account_whose_email_is_not_shown(tmp_path: Path) -> None:
    with _opened(tmp_path) as world:
        svc = person_account_id(_person(world, "svc"))
        async with _client(world) as http:
            read = (await http.get(f"{API}/user", params={"accountId": svc})).json()
        assert read["displayName"] == "Build Service"
        assert "emailAddress" not in read
        assert svc.startswith("712020:"), "derived as every seeded accountId is"
        recorded = [e.after for e in world.store.events() if isinstance(e.after, TicketSnapshot)]
        assert recorded[0].assignee == "svc" and recorded[0].assignee_email is None


async def test_a_person_s_declared_account_is_the_one_jira_shows(tmp_path: Path) -> None:
    """The entry's accountId and display name are the account's; its hidden email is not shown, and the world's
    record of the ticket still names the person, by key and by the email they have."""
    with _opened(tmp_path) as world:
        async with _client(world) as http:
            read = (await http.get(f"{API}/user", params={"accountId": DECLARED_ACCOUNT})).json()
            issue = (await http.get(f"{API}/issue/BACKEND-1")).json()
        assert (read["accountId"], read["displayName"]) == (DECLARED_ACCOUNT, "J. Smith")
        assert "emailAddress" not in read
        assert issue["fields"]["assignee"]["accountId"] == DECLARED_ACCOUNT
        recorded = [e.after for e in world.store.events() if isinstance(e.after, TicketSnapshot)]
        assert (recorded[1].assignee, recorded[1].assignee_email) == ("john", "john@example.com")


def test_a_person_s_account_kind_and_timezone_are_the_accounts(tmp_path: Path) -> None:
    people = [
        *PEOPLE,
        {"key": "gone", "name": "Gone", "email": "gone@example.com", "account": "deactivated"},
        {"key": "robot", "name": "Robot", "account": "bot", "reply": {"kind": "silent"}},
        {"key": "tz", "name": "Tz", "email": "tz@example.com", "working_hours": {"timezone": "Europe/Oslo"}},
    ]
    with _opened(tmp_path, people=people) as world:
        users = {u.person: u for u in JiraWorld(world.store).users()}
    assert users["gone"].active is False
    assert users["robot"].accountType.value == "app"
    assert users["tz"].timeZone == "Europe/Oslo"


def _assignee(jira: JiraWorld) -> str | None:
    issue = jira.find_issue("BACKEND-1")
    assert issue is not None
    return issue.assignee


def test_editing_acting_and_deactivating_find_a_person_without_an_email(tmp_path: Path) -> None:
    with _opened(tmp_path) as world:
        provider = world.provider("jira")
        jira = JiraWorld(world.store)
        undeclared = jira.find_issue("BACKEND-1")
        assert undeclared is not None
        ref = state.issue_ref(undeclared.id)
        world.edit_ticket(ref, state=None, assignee="svc")
        assert _assignee(jira) == person_account_id(_person(world, "svc"))
        assert isinstance(provider, ActsOnTickets)
        provider.act(
            TicketHappening(person="svc", ticket="Undeclared", after=START - START, action=Reassigns(to="john")),
            world.scenario,
            world.store,
            world.clock,
        )
        assert _assignee(jira) == DECLARED_ACCOUNT
        world.change_person("jira", "svc", PersonChange.DEACTIVATED)
        user = jira.user(person_account_id(_person(world, "svc")))
        assert user is not None and user.active is False


def test_a_declared_account_id_jira_could_not_hand_out_is_refused(tmp_path: Path) -> None:
    people = [{**PEOPLE[0], "accounts": [{"provider": "jira", "id": "has spaces in it"}]}, *PEOPLE[1:]]
    _seeding_refused(tmp_path, "is not one", people=people)


def test_a_person_and_a_seeded_account_that_are_one_account_is_refused(tmp_path: Path) -> None:
    taken = account_id("former@example.com")
    people = [{**PEOPLE[0], "accounts": [{"provider": "jira", "id": taken}]}, *PEOPLE[1:]]
    jira = {**JIRA, "accounts": [{"key": "former", "name": "Former", "email": "former@example.com"}]}
    _seeding_refused(
        tmp_path,
        "declare it once, as a person's account",
        people=people,
        provider_seeds=[{"provider": "jira", "body": json.dumps(jira)}],
    )


def test_an_api_token_for_a_person_without_an_email_is_refused(tmp_path: Path) -> None:
    jira = {**JIRA, "credentials": [*JIRA["credentials"], {"account": "svc", "api_token": "svc-token"}]}
    _seeding_refused(tmp_path, "has no email", provider_seeds=[{"provider": "jira", "body": json.dumps(jira)}])


# --------------------------------------------------------------------------- declared identities


async def test_a_declared_number_and_id_are_the_issue_s_and_the_next_issue_is_numbered_past_them(
    tmp_path: Path,
) -> None:
    with _opened(tmp_path) as world:
        async with _client(world) as http:
            recorded = (await http.get(f"{API}/issue/BACKEND-142")).json()
            by_id = (await http.get(f"{API}/issue/10042")).json()
            undeclared = (await http.get(f"{API}/issue/BACKEND-1")).json()
            made = await http.post(
                f"{API}/issue",
                json={"fields": {"project": {"key": "BACKEND"}, "summary": "Later", "issuetype": {"name": "Task"}}},
            )
        assert (recorded["id"], recorded["fields"]["summary"]) == ("10042", "Recorded")
        assert by_id["key"] == "BACKEND-142"
        assert undeclared["fields"]["summary"] == "Undeclared"
        assert made.status_code == 201 and made.json()["key"] == "BACKEND-143", made.text


async def test_undeclared_tickets_take_the_free_numbers_around_declared_ones(tmp_path: Path) -> None:
    tickets = [
        {"provider": "jira", "project": "Backend", "title": "First", "number": 1},
        {"provider": "jira", "project": "Backend", "title": "Second"},
        {"provider": "jira", "project": "Backend", "title": "Third", "number": 2},
        {"provider": "jira", "project": "Backend", "title": "Fourth"},
    ]
    with _opened(tmp_path, tickets=tickets) as world:
        async with _client(world) as http:
            keys = {n: (await http.get(f"{API}/issue/BACKEND-{n}")).json()["fields"]["summary"] for n in range(1, 5)}
    assert keys == {1: "First", 2: "Third", 3: "Second", 4: "Fourth"}


def test_an_issue_id_minted_later_is_never_one_a_seed_declared(tmp_path: Path) -> None:
    """Issue ids are minted from the log's sequence; a seed may declare the very id the next would be, and the
    mint passes over it."""
    with _opened(tmp_path) as probe:
        minted = JiraWorld(probe.store).next_id()
    (tmp_path / "declared").mkdir()
    tickets = [{**t, "id": minted} if t["title"] == "Recorded" else t for t in _seed()["tickets"]]
    with _opened(tmp_path / "declared", tickets=tickets) as world:
        jira = JiraWorld(world.store)
        assert jira.next_id() == minted, "the declared world is the probe's, event for event"
        assert jira.next_issue_id() != minted
        assert jira.find_issue(minted) is not None


def test_a_declared_issue_id_jira_could_not_hand_out_is_refused(tmp_path: Path) -> None:
    tickets = [{"provider": "jira", "project": "Backend", "title": "Recorded", "id": "BACKEND-1"}]
    _seeding_refused(tmp_path, "is not one", tickets=tickets)


def test_a_declared_issue_id_another_ticket_is_seeded_with_is_refused(tmp_path: Path) -> None:
    tickets = [
        {"provider": "jira", "project": "Backend", "title": "Derived"},
        {"provider": "jira", "project": "Backend", "title": "Declared", "id": state.seeded_issue_id(0)},
    ]
    _seeding_refused(tmp_path, "the id 'Derived' is seeded with", tickets=tickets)


async def test_a_ticket_added_to_an_open_world_keeps_its_declared_number(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    with _opened(tmp_path / "w") as world:
        world.extend(
            tickets=[SeededTicket(provider="jira", project="Backend", title="Imported", number=500)],
            directory=tmp_path,
            scratch=_scratch,
        )
        async with _client(world) as http:
            imported = (await http.get(f"{API}/issue/BACKEND-500")).json()
            made = await http.post(
                f"{API}/issue",
                json={"fields": {"project": {"key": "BACKEND"}, "summary": "Later", "issuetype": {"name": "Task"}}},
            )
        assert imported["fields"]["summary"] == "Imported"
        assert made.json()["key"] == "BACKEND-501"
        assert any(e.entity.kind is EntityKind.TICKET for e in world.store.events())


async def test_a_ticket_added_with_a_number_the_world_handed_out_is_refused(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    with _opened(tmp_path / "w") as world:
        async with _client(world) as http:
            made = await http.post(
                f"{API}/issue",
                json={"fields": {"project": {"key": "BACKEND"}, "summary": "Agent's", "issuetype": {"name": "Task"}}},
            )
            assert made.json()["key"] == "BACKEND-143"
        head = world.store.head()
        with pytest.raises(WorldRefused, match="BACKEND-143 is declared by the addition"):
            world.extend(
                tickets=[SeededTicket(provider="jira", project="Backend", title="Imported", number=143)],
                directory=tmp_path,
                scratch=_scratch,
            )
        assert world.store.head() == head
