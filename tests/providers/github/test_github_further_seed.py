"""An open GitHub world takes every kind `GitHubSeed` holds through a fragment of it (users, organizations,
tokens, repositories, faults, limits, budgets), and nothing it already held moves: every id GitHub seeds comes from a
name (a login, `owner/name`, a path, a token's digest), and a commit's sha from its place in its own repository's
history, which only a new repository can bring."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld, WorldRefused
from minutehand.domain.scenario import Person, ProviderSeed, Seed
from minutehand.domain.world import EntityKind
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
BASE = {
    "users": [{"login": "octo", "person": "owen"}, {"login": "sofi", "person": "sofia"}],
    "organizations": [{"login": "acme", "members": ["octo"]}],
    "tokens": [{"token": "ghp_base1", "kind": "classic", "login": "octo"}],
    "repositories": [
        {"owner": "acme", "name": "notes", "files": [{"path": "README.md", "text": "hi"}],
         "commits": [{"message": "start", "author": "octo", "before": "P2D", "paths": ["README.md"]}]},
    ],
    "faults": [{"kind": "server_error"}],
}  # fmt: skip


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
            "people": [
                {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
                {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
            ],
            "provider_seeds": [{"provider": "github", "body": json.dumps(BASE)}],
        }
    ).starting(START)
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
        world.open(["github"])
        yield world
    finally:
        store.close()


def _held(store: Store) -> dict[tuple[EntityKind, str], str]:
    found: dict[tuple[EntityKind, str], str] = {}
    for event in store.events():
        stored = store.get(event.entity)
        if stored is not None and event.entity.provider == "github":
            found[(event.entity.kind, event.entity.external_id)] = stored.body
    return found


FRAGMENTS: dict[str, object] = {
    "user": {"users": [{"login": "ivy-ng", "name": "Ivy Ng"}]},
    "organization": {"organizations": [{"login": "ops", "members": ["sofi"]}]},
    "token": {"tokens": [{"token": "ghp_added1", "kind": "classic", "login": "sofi"}]},
    "repository": {"repositories": [{"owner": "sofi", "name": "plans", "files": [{"path": "a.md", "text": "a"}]}]},
    "fault": {"faults": [{"kind": "rate_limited", "resource": "core"}]},
    "limits": {"limits": [{"repository": "acme/notes", "tree_entry_limit": 1}]},
    "budget": {"budgets": [{"login": "octo", "resource": "search", "remaining": 3}]},
}


@pytest.mark.parametrize("kind", list(FRAGMENTS))
def test_an_open_github_world_takes_each_kind_its_seed_holds_and_moves_nothing(kind: str, tmp_path: Path) -> None:
    with _open(tmp_path) as world:
        before = _held(world.store)
        fragment = ProviderSeed(provider="github", body=json.dumps(FRAGMENTS[kind]))
        written = world.extend(provider_seeds=[fragment], directory=tmp_path, scratch=_scratch)
        after = _held(world.store)
        assert written["github"] > 0
        assert set(before) <= set(after)
        changed = {k for k in before if before[k] != after[k]}
        assert changed == ({(EntityKind.RECORD, "repo/acme/notes")} if kind == "limits" else set())


def test_a_person_added_to_an_open_github_world_who_has_an_account_is_given_it(tmp_path: Path) -> None:
    with _open(tmp_path) as world:
        before = _held(world.store)
        user = ProviderSeed(provider="github", body=json.dumps({"users": [{"login": "ivy-ng", "person": "ivy"}]}))
        world.extend(
            people=[Person(key="ivy", name="Ivy Ng", email="ivy@example.com")],
            provider_seeds=[user],
            directory=tmp_path,
            scratch=_scratch,
        )
        after = _held(world.store)
        assert {k: v for k, v in after.items() if k in before} == before
        assert '"email":"ivy@example.com"' in after[(EntityKind.RECORD, "account/ivy-ng")].replace(" ", "")


def test_a_repository_added_under_a_name_the_world_holds_is_refused(tmp_path: Path) -> None:
    with _open(tmp_path) as world:
        again = ProviderSeed(provider="github", body=json.dumps({"repositories": [{"owner": "acme", "name": "notes"}]}))
        with pytest.raises(WorldRefused, match="two repositories share a name"):
            world.extend(provider_seeds=[again], directory=tmp_path, scratch=_scratch)
