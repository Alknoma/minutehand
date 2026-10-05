"""Jira names what it seeds by what the thing is, so an open world takes more of every kind its seed holds (accounts,
credentials, projects with boards and sprints, fields, an added ticket's details, rate limits, people, tickets)
without moving anything already there; an issue's seeded comments read first, in the seed's order, however the ids
of later comments compare."""

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

from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld, WorldRefused
from minutehand.domain.scenario import Person, ProviderSeed, Seed, SeededTicket
from minutehand.domain.world import EntityKind
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
TOKEN = "jira-further-token"
EMAIL = "agent@further.example"
API = "https://further.atlassian.net/rest/api/3"
AGILE = "https://further.atlassian.net/rest/agile/1.0"
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
]


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


@contextmanager
def _opened(directory: Path) -> Iterator[StandingWorld]:
    seed = Seed.model_validate(
        {
            "starts_at": START.isoformat(),
            "people": PEOPLE,
            "tickets": [
                {"provider": "jira", "project": "Launch", "title": "Book the venue", "assignee": "sofia",
                 "key": "venue", "comments": [{"by": "owen", "text": "first"}, {"by": "sofia", "text": "second"}]},
                {"provider": "jira", "project": "Launch", "title": "Order food", "assignee": "owen", "key": "food"},
            ],
            "provider_seeds": [{"provider": "jira", "body": json.dumps({
                "site": "further", "agent_email": EMAIL, "credentials": [{"account": "agent", "api_token": TOKEN}],
                "issues": [{"ticket": "venue", "comments": [{"by": "agent", "text": "third", "at": "PT1H"}],
                            "links": [{"type": "Blocks", "to": "food"}]}],
            })}],
        }
    ).starting(START)  # fmt: skip
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


def _held(store: Store) -> dict[tuple[EntityKind, str], str]:
    found: dict[tuple[EntityKind, str], str] = {}
    for event in store.events():
        stored = store.get(event.entity)
        if stored is not None and event.entity.provider == "jira":
            found[(event.entity.kind, event.entity.external_id)] = stored.body
    return found


def _client(world: StandingWorld) -> httpx.AsyncClient:
    app = world.app_for(world.provider("jira").manifest)
    basic = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), headers={"Authorization": f"Basic {basic}"}, timeout=30
    )


def _extend(world: StandingWorld, directory: Path, **added: Any) -> dict[str, int]:
    return world.extend(
        people=[Person.model_validate(p) for p in added.get("people", [])],
        tickets=[SeededTicket.model_validate(t) for t in added.get("tickets", [])],
        provider_seeds=[ProviderSeed(provider="jira", body=json.dumps(added["jira"]))] if "jira" in added else [],
        directory=directory,
        scratch=_scratch,
    )


async def test_an_issues_seeded_comments_read_first_in_the_seeds_order_and_a_later_one_after(tmp_path: Path) -> None:
    with _opened(tmp_path) as world:
        async with _client(world) as http:
            added = await http.post(
                f"{API}/issue/LAUNCH-1/comment",
                json={
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "fourth"}]}],
                    }
                },
            )
            assert added.status_code == 201, added.text
            read = (await http.get(f"{API}/issue/LAUNCH-1/comment")).json()
        texts = [json.dumps(c["body"]) for c in read["comments"]]
        assert [next(w for w in ("first", "second", "third", "fourth") if w in t) for t in texts] == [
            "first", "second", "third", "fourth"
        ]  # fmt: skip
        assert "seededFrom" not in json.dumps(read)


FRAGMENTS: dict[str, dict[str, Any]] = {
    "account": {"jira": {"accounts": [{"key": "robot", "name": "Robot", "kind": "app"}]}},
    "credential": {"jira": {"credentials": [{"account": "sofia", "api_token": "sofia-token"}]}},
    "project with a board and a sprint": {"jira": {"projects": [{"name": "Ops", "key": "OPS", "boards": [
        {"name": "Ops board", "sprints": [{"name": "Sprint 1", "starts": "-P1D", "lasts": "P14D"}]}]}]}},
    "field": {"jira": {"fields": [{"id": "customfield_10077", "name": "Size", "kind": "float"}]}},
    "rate limit": {"jira": {"rate_limits": [{"path": "/rest/api/3/search", "times": 2, "retry_after": 5}]}},
    "ticket with its details": {
        "tickets": [{"provider": "jira", "project": "Launch", "title": "Hire a band", "key": "band"}],
        "jira": {"issues": [{"ticket": "band", "priority": "High", "comments": [{"by": "agent", "text": "booked"}],
                             "links": [{"type": "Relates", "to": "venue"}]}]},
    },
    "person": {"people": [{"key": "ivy", "name": "Ivy Ng", "email": "ivy@example.com"}]},
    "ticket in a new project": {"tickets": [{"provider": "jira", "project": "Ops", "title": "Rota"}]},
}  # fmt: skip


@pytest.mark.parametrize("kind", list(FRAGMENTS))
def test_every_kind_the_jira_seed_holds_lands_on_an_open_world_and_moves_nothing(kind: str, tmp_path: Path) -> None:
    with _opened(tmp_path) as world:
        before = _held(world.store)
        written = _extend(world, tmp_path, **FRAGMENTS[kind])
        after = _held(world.store)
        assert written["jira"] > 0
        assert set(before) <= set(after), sorted(set(before) - set(after))
        new = {k: v for k, v in after.items() if k not in before}
        assert new or any(after[k] != before[k] for k in before), f"nothing of {kind} reached the world"


async def test_a_ticket_added_to_an_open_world_is_numbered_after_its_projects_seeded_issues(tmp_path: Path) -> None:
    with _opened(tmp_path) as world:
        _extend(world, tmp_path, tickets=[{"provider": "jira", "project": "Launch", "title": "Hire a band"}])
        async with _client(world) as http:
            found = (await http.get(f"{API}/issue/LAUNCH-3")).json()
            venue = (await http.get(f"{API}/issue/LAUNCH-1")).json()
        assert found["fields"]["summary"] == "Hire a band"
        assert venue["fields"]["summary"] == "Book the venue" and venue["id"] == "1000"


async def test_a_ticket_added_where_the_agent_already_took_its_key_is_refused_naming_the_key(tmp_path: Path) -> None:
    """The seed numbers a project's tickets in order, and it cannot see an issue the agent made since: a ticket
    added to that project would take the key the agent's issue holds, so it is refused, naming the key."""
    with _opened(tmp_path) as world:
        async with _client(world) as http:
            made = await http.post(
                f"{API}/issue",
                json={"fields": {"project": {"key": "LAUNCH"}, "summary": "Agent's", "issuetype": {"name": "Task"}}},
            )
            assert made.status_code == 201 and made.json()["key"] == "LAUNCH-3", made.text
        head = world.store.head()
        with pytest.raises(WorldRefused, match="key:LAUNCH-3"):
            _extend(world, tmp_path, tickets=[{"provider": "jira", "project": "Launch", "title": "Hire a band"}])
        assert world.store.head() == head
