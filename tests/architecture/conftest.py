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
