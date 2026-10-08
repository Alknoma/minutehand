from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from minutehand.testing.client import MinutehandClient
from tests.architecture.support import (
    APPROVER_TOKEN,
    APPROVER_TOKEN_VARIABLE,
    MINUTEHAND,
    Rig,
    free_port,
    outside_module,
)
from tests.support.people import people_environment


@pytest.fixture(scope="session")
def outside(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    """The reference agent's outside world (a model API and a venue search over HTTPS), for the session."""
    module = outside_module()
    with module.serving(Path(tmp_path_factory.mktemp("outside"))) as served:
        yield served


@pytest.fixture(scope="module")
def minutehand(outside: object, tmp_path_factory: pytest.TempPathFactory) -> Iterator[MinutehandClient]:
    """`minutehand serve` as the installed command, trusting the outside world's CA for what it passes through."""
    state = tmp_path_factory.mktemp("serve")
    control, proxy, telemetry = free_port(), free_port(), free_port()
    server = subprocess.Popen(
        [
            str(MINUTEHAND),
            "serve",
            "--state",
            str(state),
            "--proxy-port",
            str(proxy),
            "--control-port",
            str(control),
            "--telemetry-port",
            str(telemetry),
            "--upstream-ca",
            str(outside.ca),  # type: ignore[attr-defined]
        ],
        env={**os.environ, APPROVER_TOKEN_VARIABLE: APPROVER_TOKEN, **people_environment()},
    )
    try:
        with MinutehandClient(f"http://127.0.0.1:{control}") as client:
            give_up = time.monotonic() + 90
            while True:
                try:
                    if client.health():
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < give_up, "minutehand serve did not answer"
                time.sleep(0.1)
            yield client
    finally:
        server.terminate()
        server.wait(timeout=15)


@pytest.fixture
def rig(outside: object, tmp_path: Path) -> Rig:
    return Rig(tmp_path, outside.model, outside.search, str(outside.ca))  # type: ignore[attr-defined]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every test here starts real processes and the installed command, several times: on a loaded shared runner
    that takes far longer than the suite's 60 seconds a test, so the limit here is ten minutes."""
    for item in items:
        if "tests/architecture/" in str(item.path) and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(600))
