"""An open YouTrack world takes more of everything its seed holds — people, users, tokens, Hub services, fields,
projects, tickets with their issue details, grants, faults — and nothing it already held moves: YouTrack numbers
what it seeds by what it is, never by where seeding reached in the log.

What the agent makes in the run is numbered by the log; what was seeded lists before it, in the order it was seeded,
even when both happen at the same instant."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.youtrack import state
from minutehand.adapters.providers.youtrack.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld, WorldRefused
from minutehand.domain.scenario import Person, ProviderSeed, Scenario, Seed, SeededTicket
from minutehand.domain.world import EntityKind
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
TOKEN = "perm:YWdlbnQ=.Zm9yd2FyZA==.c2VlZA=="
FRAGMENT = {
    "users": [{"login": "vendor", "name": "Visiting Vendor", "email": "vendor@example.net"}],
    "tokens": [{"token": TOKEN, "login": "agent-bot"}],
    "services": [{"client_id": "planner", "secret": "s3cr3t", "login": "agent-bot"}],
    "fields": [{"name": "Sprint", "type": "version[1]"}],
    "projects": [
        {"name": "Launch", "short_name": "LAUNCH"},
        {"name": "Spare", "short_name": "SPARE", "fields": [{"name": "State"}, {"name": "Assignee"}]},
    ],
    "issues": [
        {"ticket": "venue", "created_ago": "PT2H", "links": [{"phrase": "relates to", "ticket": "food"}],
         "history": [{"ago": "PT1H", "by": "owen", "fields": [{"field": "Priority", "value": "Major"}]}]},
    ],
    "grants": [{"login": "sofia", "permission": "jetbrains.jetpass.project-update", "held": False}],
    "faults": [{"method": "DELETE", "path": "/issues/*", "status": 503, "lasts": "PT1H"}],
}  # fmt: skip
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
    {"key": "mila", "name": "Mila Hart", "email": "mila@example.com", "reply": {"kind": "silent"}},
]
TICKETS = [
    {"provider": "youtrack", "project": "Launch", "title": "Book the venue", "key": "venue", "assignee": "sofia",
     "labels": ["urgent", "venue"], "comments": [{"by": "owen", "text": "please"}, {"by": "sofia", "text": "on it"}]},
    {"provider": "youtrack", "project": "Launch", "title": "Order food", "key": "food", "assignee": "mila"},
    {"provider": "youtrack", "project": "Ops", "title": "Rota", "assignee": "owen", "labels": ["urgent"]},
]  # fmt: skip


def _scenario(fragment: dict[str, object] = FRAGMENT) -> Scenario:
    seed = Seed.model_validate(
        {
            "starts_at": START.isoformat(),
            "people": PEOPLE,
            "tickets": TICKETS,
            "provider_seeds": [{"provider": "youtrack", "body": json.dumps(fragment)}],
        }
    )
    return seed.starting(START)


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def world(tmp_path: Path) -> Iterator[StandingWorld]:
    scenario = _scenario()
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(tmp_path / "world.db", "world", clock)
    provider = build()
    opened = StandingWorld(
        scenario=scenario,
        store=store,
        clock=clock,
        provider=lambda _: provider,
        inbound=[],
        signing={},
        scripted=False,
    )
    opened.open(["youtrack"])
    yield opened
    store.close()


def _held(store: Store) -> dict[tuple[EntityKind, str], str]:
    """Every entity YouTrack holds and its body; a project's team grows with the users added, so only its id."""
    found: dict[tuple[EntityKind, str], str] = {}
    for event in store.events():
        stored = store.get(event.entity)
        if event.entity.provider == "youtrack" and stored is not None:
            found[(event.entity.kind, event.entity.external_id)] = (
                "" if stored.parent == state.PROJECTS else stored.body
            )
    return found


IVY = {"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com", "reply": {"kind": "silent"}}
FRAGMENTS: dict[str, dict[str, object]] = {
    "users": {"users": [{"login": "auditor", "name": "Audit Bot"}]},
    "tokens": {"tokens": [{"token": "perm:bW9yZQ==.dG9rZW4=.YWRkZWQ=", "login": "vendor"}]},
    "services": {"services": [{"client_id": "reporter", "secret": "x", "login": "vendor"}]},
    "fields": {"fields": [{"name": "Budget", "type": "integer"}]},
    "projects": {"projects": [{"name": "Summit", "short_name": "SUMMIT", "team": ["owen"]}]},
    "grants": {"grants": [{"login": "mila", "permission": "jetbrains.jetpass.project-update", "held": False}]},
    "faults": {"faults": [{"method": "POST", "path": "/issues", "status": 500}]},
}


@pytest.mark.parametrize("kind", list(FRAGMENTS))
def test_each_kind_of_the_youtrack_seed_lands_on_an_open_world_and_moves_nothing(
    world: StandingWorld, kind: str, tmp_path: Path
) -> None:
    before = _held(world.store)
    written = world.extend(
        provider_seeds=[ProviderSeed(provider="youtrack", body=json.dumps(FRAGMENTS[kind]))],
        directory=tmp_path,
        scratch=_scratch,
    )
    after = _held(world.store)
    assert written["youtrack"] > 0
    assert {k: v for k, v in after.items() if k in before} == before


ADDITIONS: dict[str, dict[str, list[dict[str, object]]]] = {
    "a person": {"people": [IVY]},
    "a ticket": {
        "tickets": [
            {
                "provider": "youtrack",
                "project": "Launch",
                "title": "Hire a band",
                "labels": ["new"],
                "comments": [{"by": "owen", "text": "jazz"}],
            }
        ]
    },
    "a ticket in a new project": {"tickets": [{"provider": "youtrack", "project": "Docs", "title": "Write it"}]},
    "a ticket in a plain project": {"tickets": [{"provider": "youtrack", "project": "Ops", "title": "Rota two"}]},
}


@pytest.mark.parametrize("kind", list(ADDITIONS))
def test_a_person_or_ticket_added_to_an_open_world_moves_nothing_it_held(
    world: StandingWorld, kind: str, tmp_path: Path
) -> None:
    before = _held(world.store)
    added = ADDITIONS[kind]
    written = world.extend(
        people=[Person.model_validate(p) for p in added.get("people", [])],
        tickets=[SeededTicket.model_validate(t) for t in added.get("tickets", [])],
        directory=tmp_path,
        scratch=_scratch,
    )
    after = _held(world.store)
    assert written["youtrack"] > 0
    assert {k: v for k, v in after.items() if k in before} == before


def test_a_ticket_with_its_issue_details_lands_with_a_link_to_a_seeded_issue(
    world: StandingWorld, tmp_path: Path
) -> None:
    detail = {"issues": [{"ticket": "band", "links": [{"phrase": "relates to", "ticket": "venue"}]}]}
    world.extend(
        tickets=[SeededTicket(provider="youtrack", project="Launch", title="Hire a band", key="band")],
        provider_seeds=[ProviderSeed(provider="youtrack", body=json.dumps(detail))],
        directory=tmp_path,
        scratch=_scratch,
    )
    youtrack = state.YouTrackWorld(world.store)
    band = youtrack.find_issue("LAUNCH-3")
    venue = youtrack.find_issue("LAUNCH-1")
    assert band is not None and venue is not None and band.summary == "Hire a band"
    assert any({link.source, link.target} == {band.id, venue.id} for link in youtrack.links())


def test_a_seeded_project_short_name_given_other_content_is_refused(world: StandingWorld, tmp_path: Path) -> None:
    """A fragment naming a held project grows it (`ProjectSeed.IDENTITY`); one giving it another short name
    contradicts it, and is refused naming the field."""
    head = world.store.head()
    clash = {"projects": [{"name": "Launch", "short_name": "OTHER"}]}
    with pytest.raises(WorldRefused, match=r"projects\[name='Launch'\]\.short_name is 'LAUNCH'"):
        world.extend(
            provider_seeds=[ProviderSeed(provider="youtrack", body=json.dumps(clash))],
            directory=tmp_path,
            scratch=_scratch,
        )
    assert world.store.head() == head


async def _agent(world: StandingWorld) -> httpx.AsyncClient:
    app = world.app_for(world.provider("youtrack").manifest)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://acme.youtrack.cloud",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


async def test_what_was_seeded_lists_before_what_the_agent_made_at_the_same_instant(world: StandingWorld) -> None:
    async with await _agent(world) as http:
        made = await http.post("/api/issues/LAUNCH-1/comments", json={"text": "agent says"})
        tagged = await http.post("/api/tags", json={"name": "agent-tag"})
        created = await http.post("/api/issues", json={"project": {"id": "0-1000"}, "summary": "Agent's own"})
        comments = await http.get("/api/issues/LAUNCH-1/comments", params={"fields": "text"})
        tags = await http.get("/api/tags", params={"fields": "name"})
        found = await http.get("/api/issues", params={"query": "project: LAUNCH", "fields": "idReadable"})
    assert made.status_code == 200 and tagged.status_code == 200 and created.status_code == 200
    assert [c["text"] for c in comments.json()] == ["please", "on it", "agent says"]
    assert [t["name"] for t in tags.json()] == ["urgent", "venue", "agent-tag"]
    assert [i["idReadable"] for i in found.json()] == ["LAUNCH-1", "LAUNCH-2", "LAUNCH-3"]


async def test_a_ticket_added_where_the_agent_already_took_its_number_is_numbered_after_the_agents_issue(
    world: StandingWorld, tmp_path: Path
) -> None:
    """A seeded issue's readable number is worked out from the seed alone, so where the agent has made LAUNCH-3
    since the world opened, a ticket added to Launch would be LAUNCH-3 too; YouTrack places it at the project's next
    number, as it would the agent's next issue, and the agent's issue keeps its readable id."""
    async with await _agent(world) as http:
        created = await http.post("/api/issues", json={"project": {"id": "0-1000"}, "summary": "Agent's own"})
    assert created.status_code == 200
    before = _held(world.store)
    world.extend(
        tickets=[SeededTicket(provider="youtrack", project="Launch", title="Hire a band")],
        directory=tmp_path,
        scratch=_scratch,
    )
    after = _held(world.store)
    assert all(after[k] == v for k, v in before.items())
    async with await _agent(world) as http:
        added = await http.get("/api/issues/LAUNCH-4", params={"fields": "idReadable,numberInProject,summary"})
        agents = await http.get("/api/issues/LAUNCH-3", params={"fields": "summary"})
        later = await http.post("/api/issues", json={"project": {"id": "0-1000"}, "summary": "Later"},
                                params={"fields": "idReadable"})  # fmt: skip
    assert added.json() | {"$type": None} == {"idReadable": "LAUNCH-4", "numberInProject": 4,
                                               "summary": "Hire a band", "$type": None}  # fmt: skip
    assert agents.json()["summary"] == "Agent's own"
    assert later.json()["idReadable"] == "LAUNCH-5"
