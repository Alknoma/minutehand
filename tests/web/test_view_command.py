"""`minutehand view` itself: the installed command, serving on 127.0.0.1 and nowhere else."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

from tests.e2e.support import free_port

MINUTEHAND = Path(sys.executable).parent / "minutehand"


def test_the_command_serves_the_page_and_the_api_on_loopback(tmp_path: Path) -> None:
    port = free_port()
    process = subprocess.Popen(
        [str(MINUTEHAND), "view", "--state", str(tmp_path / "state"), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        give_up = time.monotonic() + 20
        while True:
            try:
                runs = httpx.get(f"http://127.0.0.1:{port}/api/runs")
                break
            except httpx.ConnectError:
                assert process.poll() is None, process.stderr.read() if process.stderr else b""
                assert time.monotonic() < give_up, "the viewer did not start listening"
                time.sleep(0.05)
        page = httpx.get(f"http://127.0.0.1:{port}/")
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert runs.status_code == 200 and runs.json() == {"runs": []}
    assert page.status_code == 200 and "<title>Minutehand Runs</title>" in page.text
