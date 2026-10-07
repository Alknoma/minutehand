"""Stopping the agent's own program stops everything it started.

A launcher that starts the agent's processes and waits on them past Minutehand's stop timeout used to be killed
alone: its child kept the agent's port, and the restore that started the agent again was answered by the old
child, holding another moment (the fingerprint refusal in tests/architecture/test_checkpoints.py, seen on CI).
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

from minutehand import session
from minutehand.domain.agent import AgentUnderTest, GoalByWake, Reported
from tests.ports import free_port

LAUNCHER = """
import signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", '''
import signal, socket, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)  # slow to stop: ignores the polite request
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", {port})); s.listen()
open("{ready}", "w").write("up")
while True: time.sleep(1)
'''])
signal.signal(signal.SIGTERM, lambda *_: None)  # waits on its child, as the reference agent's run.py does
child.wait()
"""


def _listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.mark.asyncio
async def test_stopping_the_agent_stops_a_child_its_launcher_was_still_waiting_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, "STOP_TIMEOUT", 0.5)
    port, ready = free_port(), tmp_path / "ready"
    script = tmp_path / "launch.py"
    script.write_text(LAUNCHER.format(port=port, ready=ready))
    url = f"http://127.0.0.1:{port}"
    agent = AgentUnderTest(
        name="launched", goal=GoalByWake(), wakes=[Reported(wake_url=f"{url}/wake", report_url=f"{url}/report")]
    )
    program = session._Program([sys.executable, str(script)], {}, agent, tmp_path / "agent.log")  # pyright: ignore[reportPrivateUsage]

    await program.start()
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _listening(port)

    await program.stop()

    deadline = time.monotonic() + 2
    while _listening(port) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _listening(port), "the launcher's child outlived the stop, still holding the agent's port"
