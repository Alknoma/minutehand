"""A fault a seed fragment adds to an open world lands beside the faults declared on it at runtime
(`provider-faults`): the declared ones are numbered (or kept) apart from the seed's own, so the fragment neither
takes one's number nor rewrites a record the declaration changed."""

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
PEOPLE = [{"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}}]

FAULTS: dict[str, tuple[str, list[dict[str, object]]]] = {
    "slack": ("faults", [{"call": "auth.test", "answer": {"kind": "refused", "error": "account_inactive"}},
                         {"call": "users.list", "answer": {"kind": "rate_limited"}}]),
    "github": ("faults", [{"kind": "server_error"}, {"kind": "server_error"}]),
    "google_drive": ("faults", [{"operation": "files.list", "kind": "rate_limited", "times": 2},
                                {"operation": "files.get", "kind": "rate_limited"}]),
    "microsoft": ("faults", [{"call": "/v1.0/me", "answer": {"kind": "refused", "error": "Forbidden"}},
                             {"answer": {"kind": "rate_limited"}}]),
    "youtrack": ("faults", [{"path": "/issues", "status": 503}, {"path": "/users", "status": 500}]),
    "notion": ("faults", [{"kind": "rate_limited", "path": "/v1/search"}, {"kind": "rate_limited"}]),
    "jira": ("rate_limits", [{"path": "/rest/api/3/myself", "times": 1, "retry_after": 1},
                             {"path": "/rest/api/3/search", "times": 1, "retry_after": 1}]),
}  # fmt: skip


@contextmanager
def _scratch(path: Path, clock: Clock) -> Iterator[Store]:
    store = SqliteStore(path, "scratch", clock)
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("provider", sorted(FAULTS))
def test_a_fragments_fault_lands_after_one_declared_at_runtime(provider: str, tmp_path: Path) -> None:
    field, (first, second) = FAULTS[provider][0], FAULTS[provider][1]
    registry = Registry.installed()
    manifests = {m.key: m for m in registry.manifests}
    scenario = Seed.model_validate(
        {"starts_at": START.isoformat(), "people": PEOPLE,
         "provider_seeds": [{"provider": provider, "body": json.dumps({field: [first]})}]}
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
        world.declare_faults(provider, json.dumps({field: [second]}))
        declared = {
            e.entity: store.get(e.entity) for e in store.events(since=store.head() - 1) if e.entity.provider == provider
        }
        written = world.extend(
            provider_seeds=[ProviderSeed(provider=provider, body=json.dumps({field: [second]}))],
            directory=tmp_path,
            scratch=_scratch,
        )
        assert written[provider] > 0
        for ref, held in declared.items():
            assert store.get(ref) == held, f"the fragment rewrote what the declaration wrote: {ref}"
    finally:
        store.close()
