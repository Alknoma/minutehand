from __future__ import annotations

from pathlib import Path

import pytest

from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from tests.proxy.support import PROVIDERS, START


@pytest.fixture
def clock() -> RunClock:
    return RunClock(START)


@pytest.fixture
def world_path(tmp_path: Path) -> Path:
    return tmp_path / "world.db"


@pytest.fixture
def store(world_path: Path, clock: RunClock) -> SqliteStore:
    return SqliteStore(world_path, "run", clock)


@pytest.fixture
def registry() -> Registry:
    found = Registry()
    found.discover(PROVIDERS)
    return found
