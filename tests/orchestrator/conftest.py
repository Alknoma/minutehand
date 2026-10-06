from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.orchestrator.rig import Rig, rigged


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    async with rigged(tmp_path) as r:
        yield r
