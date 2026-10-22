"""The agent file's `start_limit` is how long the agent's command may take before its address accepts connections.

An agent whose whole stack starts before its front door opens (a dozen services and a seeded tenant) takes longer
than the 30 seconds Minutehand once allowed every agent; opened earlier, its door took the first pushed event while
nothing behind it could answer, and the event was acknowledged and lost.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.people import InboundTarget
from minutehand.session import AgentExited
from tests.ports import free_port
from tests.support.scenarios import SANDBOX

SLOW = """
import socket, time
time.sleep({delay})
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", {port})); s.listen()
while True: time.sleep(1)
"""


def _program(tmp_path: Path, limit: timedelta, delay: float) -> session._Program:  # pyright: ignore[reportPrivateUsage]
    port = free_port()
    script = tmp_path / "slow.py"
    script.write_text(SLOW.format(delay=delay, port=port))
    agent = AgentUnderTest(
        name="slow",
        sandbox=SANDBOX,
        start_limit=limit,
        inbound=[InboundTarget(provider="slack", url=f"http://127.0.0.1:{port}/slack/events")],
    )
    return session._Program([sys.executable, str(script)], {}, agent, tmp_path / "agent.log")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_a_command_slower_than_its_start_limit_is_refused_naming_it(tmp_path: Path) -> None:
    program = _program(tmp_path, timedelta(milliseconds=500), delay=3)
    try:
        with pytest.raises(AgentExited, match="start_limit"):
            await program.start()
    finally:
        await program.stop()


@pytest.mark.asyncio
async def test_a_command_within_its_start_limit_is_waited_for(tmp_path: Path) -> None:
    program = _program(tmp_path, timedelta(seconds=20), delay=1.5)
    try:
        await program.start()
        assert program.exited() is None
    finally:
        await program.stop()
