"""A provider seed fragment on an open world grows a thing the world already holds (a page in a Notion workspace,
a token in a Slack workspace, a file in a GitHub repository, a member of a Jira, YouTrack or Asana project) instead
of declaring a second of the same thing, because each keyed list's model says which field names its items."""

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
from minutehand.application.standing import StandingWorld
from minutehand.domain.scenario import ProviderSeed, Seed
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
]

CASES: list[tuple[str, dict[str, object], dict[str, object], str]] = [
    (
        "notion",
        {"workspaces": [{"key": "acme", "name": "Acme", "pages": [{"key": "home", "title": "Home"}],
                         "integrations": [{"key": "bot", "name": "Bot", "tokens": ["secret_a"]}]}]},
        {"workspaces": [{"key": "acme", "pages": [{"key": "plans", "title": "Plans", "parent": "home"}],
                         "integrations": [{"key": "bot", "shared": ["plans"]}]}]},
        "Plans",
    ),
    (
        "slack",
        {"workspaces": [{"team_id": "TACME", "domain": "acme", "tokens": ["xoxb-a"]}]},
        {"workspaces": [{"team_id": "TACME", "tokens": ["xoxb-b"]}]},
        "TACME",
    ),
    (
        "github",
        {"users": [{"login": "octo", "person": "owen"}],
         "repositories": [{"owner": "octo", "name": "notes", "files": [{"path": "README.md", "text": "hi"}],
                           "commits": [{"message": "Add the notes", "author": "octo", "before": "P1D", "paths": ["README.md"]}]}]},
        {"repositories": [{"owner": "octo", "name": "notes", "files": [{"path": "docs/plan.md", "text": "plan"}]}]},
        "docs/plan.md",
    ),
    (
        "jira",
        {"projects": [{"name": "Launch", "key": "LAU", "members": ["owen"]}]},
        {"projects": [{"name": "Launch", "members": ["sofia"]}]},
        "LAU",
    ),
    (
        "youtrack",
        {"projects": [{"name": "Launch", "short_name": "LAU", "team": ["owen"]}]},
        {"projects": [{"name": "Launch", "team": ["sofia"]}]},
        "Launch",
    ),
    (
        "asana",
        {"projects": [{"name": "Launch", "members": ["owen"]}]},
        {"projects": [{"name": "Launch", "members": ["sofia"]}]},
        "Launch",
    ),
]  # fmt: skip


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize(("provider", "base", "fragment", "shows"), CASES, ids=[c[0] for c in CASES])
def test_a_fragment_naming_a_held_item_grows_it(
    provider: str, base: dict[str, object], fragment: dict[str, object], shows: str, tmp_path: Path
) -> None:
    registry = Registry.installed()
    manifests = {m.key: m for m in registry.manifests}
    scenario = Seed.model_validate(
        {"starts_at": START.isoformat(), "people": PEOPLE,
         "provider_seeds": [{"provider": provider, "body": json.dumps(base)}]}
    ).starting(START)  # fmt: skip
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(tmp_path / "world.db", "world", clock)
    try:
        world = StandingWorld(
            scenario=scenario,
            store=store,
            clock=clock,
            provider=lambda k: registry.provider(manifests[k]),
            inbound=[],
            signing={},
            scripted=False,
        )
        world.open([provider])
        written = world.extend(
            provider_seeds=[ProviderSeed(provider=provider, body=json.dumps(fragment))],
            directory=tmp_path,
            scratch=_scratch,
        )
        assert written[provider] > 0
        grown = json.loads(next(s for s in world.scenario.provider_seeds if s.provider == provider).body)
        listed = next(iter(fragment))
        held_items = base[listed]
        assert isinstance(held_items, list)
        assert len(grown[listed]) == len(held_items), "the fragment's item was added beside the one held"
        held = [store.get(e.entity) for e in store.events() if e.entity.provider == provider]
        assert any(s is not None and shows in s.body for s in held)
    finally:
        store.close()
