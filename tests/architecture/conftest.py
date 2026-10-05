from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.architecture.support import Rig, outside_module


@pytest.fixture(scope="session")
def outside(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    """The reference agent's outside world (a model API and a venue search over HTTPS), for the session."""
    module = outside_module()
    with module.serving(Path(tmp_path_factory.mktemp("outside"))) as served:
        yield served


@pytest.fixture
def rig(outside: object, tmp_path: Path) -> Rig:
    return Rig(tmp_path, outside.model, outside.search, str(outside.ca))  # type: ignore[attr-defined]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every test here starts real processes and the installed command, several times: on a loaded shared runner
    that takes far longer than the suite's 60 seconds a test, so the limit here is ten minutes."""
    for item in items:
        if "tests/architecture/" in str(item.path) and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(600))
