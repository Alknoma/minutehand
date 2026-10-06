"""An open Asana world takes every kind its seed holds, from the scenario (people, tickets, tickets in a new project)
or from a fragment of `AsanaSeed` (teams, custom fields, tags, projects, tasks, tokens, refresh tokens, rate limits,
limits), and nothing it already held moves: a seeded story's gid is its task's place and its own, never where
seeding reached in the log."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand.adapters.providers.asana import state
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld
from minutehand.domain.scenario import Person, ProviderSeed, Seed, SeededTicket
from minutehand.domain.world import EntityKind
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
]
BASE = {
    "teams": [{"name": "Events"}],
    "custom_fields": [{"name": "Size", "kind": "enum", "options": [{"name": "S"}, {"name": "L"}]}],
    "tags": ["urgent"],
    "projects": [{"name": "Launch", "team": "Events", "custom_fields": ["Size"]}],
    "tasks": [{"ticket": "Book the venue", "comments": [{"person": "sofia", "text": "seen", "ago": "PT1H"}]}],
}


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


@contextmanager
def _open(directory: Path) -> Iterator[StandingWorld]:
    registry = Registry.installed()
    manifests = {m.key: m for m in registry.manifests}
    scenario = Seed.model_validate(
        {
            "starts_at": START.isoformat(),
            "people": PEOPLE,
            "tickets": [
                {"provider": "asana", "project": "Launch", "title": "Book the venue", "assignee": "sofia",
                 "comments": [{"by": "owen", "text": "please"}]},
                {"provider": "asana", "project": "Launch", "title": "Order food", "assignee": "owen"},
            ],
            "provider_seeds": [{"provider": "asana", "body": json.dumps(BASE)}],
        }
    ).starting(START)  # fmt: skip
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(directory / "world.db", "world", clock)
    world = StandingWorld(
        scenario=scenario,
        store=store,
        clock=clock,
        provider=lambda k: registry.provider(manifests[k]),
        inbound=[],
        signing={},
        scripted=False,
    )
    try:
        world.open(["asana"])
        yield world
    finally:
        store.close()


def _held(store: Store) -> dict[tuple[EntityKind, str], str]:
    found: dict[tuple[EntityKind, str], str] = {}
    for event in store.events():
        stored = store.get(event.entity)
        if stored is not None and event.entity.provider == "asana":
            found[(event.entity.kind, event.entity.external_id)] = stored.body
    return found


FRAGMENTS: dict[str, object] = {
    "team": {"teams": [{"name": "Ops", "members": ["owen"]}]},
    "custom field": {"custom_fields": [{"name": "Cost", "kind": "number", "precision": 2}]},
    "tag": {"tags": ["later"]},
    "project": {"projects": [{"name": "Ops board", "team": "Events", "sections": [{"name": "Backlog"}]}]},
    "token": {"tokens": [{"token": "asana-pat-added", "person": "sofia"}]},
    "refresh token": {"refresh_tokens": [{"refresh_token": "asana-refresh-added"}]},
    "rate limit": {"rate_limits": [{"after": "PT1H", "lasts": "PT5M"}]},
    "limits": {"limits": {"premium": False}},
}


@pytest.mark.parametrize("kind", list(FRAGMENTS))
def test_an_open_asana_world_takes_each_kind_its_seed_holds_and_moves_nothing(kind: str, tmp_path: Path) -> None:
    with _open(tmp_path) as world:
        before = _held(world.store)
        fragment = ProviderSeed(provider="asana", body=json.dumps(FRAGMENTS[kind]))
        written = world.extend(provider_seeds=[fragment], directory=tmp_path, scratch=_scratch)
        after = _held(world.store)
        assert written["asana"] > 0
        assert set(before) <= set(after)


def test_an_added_asana_task_with_its_details_lands_after_the_seeded_ones(tmp_path: Path) -> None:
    with _open(tmp_path) as world:
        detail = {"tasks": [{"ticket": "Hire a band", "tags": ["urgent"], "values": [{"field": "Size", "option": "L"}],
                             "comments": [{"person": "owen", "text": "budget?", "ago": "PT2H"}]}]}  # fmt: skip
        world.extend(
            tickets=[SeededTicket(provider="asana", project="Launch", title="Hire a band", assignee="sofia")],
            provider_seeds=[ProviderSeed(provider="asana", body=json.dumps(detail))],
            directory=tmp_path,
            scratch=_scratch,
        )
        task = state.task_gid(2)
        stories = [s.entity.external_id for s in world.store.children("asana", EntityKind.COMMENT, task, after=None,
                                                                         limit=10)]  # fmt: skip
        assert stories == [state.story_gid(2, 0)]


def test_a_person_added_to_an_open_asana_world_moves_no_seeded_story(tmp_path: Path) -> None:
    """Before, a seeded story's gid was the log's position, so a person seeded ahead of it renumbered it and the
    addition was refused."""
    with _open(tmp_path) as world:
        before = _held(world.store)
        ivy = Person(key="ivy", name="Ivy Ng", email="ivy@example.org")
        world.extend(people=[ivy], directory=tmp_path, scratch=_scratch)
        after = _held(world.store)
        assert set(before) <= set(after)
        first = state.task_gid(0)
        stories = [s.entity.external_id for s in world.store.children("asana", EntityKind.COMMENT, first, after=None,
                                                                         limit=10)]  # fmt: skip
        assert stories == [state.story_gid(0, 0), state.story_gid(0, 1)]
        assert ("record", state.user_gid("ivy")) in {(k.value, i) for k, i in after}
