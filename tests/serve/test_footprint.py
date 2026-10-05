"""What `minutehand serve` costs: how long it takes to answer, and that its memory holds steady while worlds are
opened and closed. Bounds are generous, for a loaded machine; the numbers measured are in docs/serve.md."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from minutehand.testing.client import MinutehandClient
from tests.serve.support import spec

READY_WITHIN = 10.0
WORLDS = 300
GROWTH_MB = 40


@pytest.fixture
def started(tmp_path: Path) -> Iterator[tuple[float, MinutehandClient, psutil.Process]]:
    began = time.monotonic()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "minutehand.cli",
            "serve",
            "--state",
            str(tmp_path),
            "--keep",
            "20",
            "--proxy-port",
            "0",
            "--control-port",
            "0",
            "--telemetry-port",
            "0",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        announced = re.search(r"control http://127\.0\.0\.1:(\d+)", process.stdout.readline())
        assert announced is not None
        client = MinutehandClient(f"http://127.0.0.1:{announced.group(1)}")
        while not client.health():
            time.sleep(0.01)
        yield time.monotonic() - began, client, psutil.Process(process.pid)
        client.close()
    finally:
        process.terminate()
        process.wait(10)


def test_serve_answers_soon_after_it_is_started(started: tuple[float, MinutehandClient, psutil.Process]) -> None:
    took, _, _ = started
    assert took < READY_WITHIN


@pytest.mark.timeout(120)
def test_memory_holds_steady_while_worlds_are_opened_and_closed(
    started: tuple[float, MinutehandClient, psutil.Process],
) -> None:
    _, client, process = started

    def cycle(count: int, offset: int) -> None:
        for n in range(count):
            world = client.create_world(spec(f"xoxb-footprint-{offset + n}"))
            client.events(world.world_id, provider="slack")
            client.close_world(world.world_id)

    cycle(50, 0)
    warm = process.memory_info().rss
    cycle(WORLDS, 50)
    grown = (process.memory_info().rss - warm) / 2**20
    assert grown < GROWTH_MB, f"{grown:.0f} MB more after {WORLDS} worlds"
    assert client.worlds() == []
