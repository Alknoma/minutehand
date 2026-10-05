from __future__ import annotations

from collections.abc import Iterator

import pytest

from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from tests.serve.support import Served


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    """One server per module: one proxy runs in a process, so it is stopped before another module's tests."""
    with serve_in_background(tmp_path_factory.mktemp("serve")) as url, MinutehandClient(url) as client:
        yield Served(url=url, client=client, environment=client.environment())
