"""Every provider that types its own faults declares them on a world already open (`DeclaresFaults`): the
fragment is read as its seed model reads it, recorded after what seeding recorded, and a fragment that sets
anything but its faults is refused."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Scenario
from minutehand.ports.provider import DeclaresFaults

START = datetime(2026, 9, 1, 9, tzinfo=UTC)

FRAGMENTS = {
    "slack": ('{"faults": [{"answer": {"kind": "rate_limited"}}]}', '{"faults": [], "channels": []}'),
    "google_drive": (
        '{"faults": [{"operation": "files.list", "kind": "rate_limited"}]}',
        '{"faults": [], "sign_ins": []}',
    ),
    "youtrack": ('{"faults": [{"path": "/issues", "status": 503}]}', '{"faults": [], "count_unknown": true}'),
    "asana": ('{"rate_limits": [{"after": "PT1M", "lasts": "PT1M"}]}', '{"rate_limits": [], "tags": ["x"]}'),
    "jira": ('{"rate_limits": [{"path": "/rest/api/3/issue", "times": 1, "retry_after": 3}]}', '{"site": "elsewhere"}'),
    "notion": ('{"faults": [{"kind": "conflict"}]}', '{"faults": [], "webhooks": []}'),
    "microsoft": ('{"faults": [{"answer": {"kind": "refused", "error": "generalException"}}]}', '{"not_installed_for": []}'),
}  # fmt: skip

SCENARIO = Scenario.model_validate(
    {
        "name": "declared",
        "goal": "g",
        "owner": "owen",
        "starts_at": START,
        "people": [{"key": "owen", "name": "Owen Owner", "email": "owen@example.com"}],
    }
)


def test_every_provider_that_seeds_faults_declares_them() -> None:
    declaring = {
        m.key for m in Registry.installed().manifests if isinstance(Registry.installed().provider(m), DeclaresFaults)
    }
    assert declaring == set(FRAGMENTS)


@pytest.mark.parametrize("key", sorted(FRAGMENTS))
def test_a_declared_fault_is_recorded_after_the_seed_and_anything_else_is_refused(key: str, tmp_path: Path) -> None:
    registry = Registry.installed()
    provider = registry.provider(next(m for m in registry.manifests if m.key == key))
    assert isinstance(provider, DeclaresFaults)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider.seed(SCENARIO, store)
    head = store.head()
    faults, other = FRAGMENTS[key]
    provider.declare(faults, store, clock)
    assert store.head() > head, "the declaration is in the world"
    head = store.head()
    with pytest.raises(ValueError):
        provider.declare(other, store, clock)
    assert store.head() == head, "a refused declaration writes nothing"
