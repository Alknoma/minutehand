from __future__ import annotations

from pathlib import Path

import pytest

from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from tests.capture.support import START
from tests.proxy.support import PROVIDERS
from tests.proxy.upstream import Authority, make_authority


@pytest.fixture
def clock() -> RunClock:
    return RunClock(START)


@pytest.fixture
def world_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "world"
    directory.mkdir()
    return directory


@pytest.fixture
def store(world_dir: Path, clock: RunClock) -> SqliteStore:
    return SqliteStore(world_dir / "world.db", "run", clock)


@pytest.fixture
def registry() -> Registry:
    found = Registry()
    found.discover(PROVIDERS)
    return found


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return make_authority(tmp_path / "upstream-ca")
