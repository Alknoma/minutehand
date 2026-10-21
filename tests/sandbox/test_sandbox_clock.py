"""The sandbox's clock as Minutehand drives it (`application/sandbox.py`): which deadline is the agent's next wake,
and that the clock is only ever moved forward. The control calls here are real commands that print what the patched
gVisor's `runsc debug --deadlines` prints; nothing starts a sandbox."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from minutehand.application.sandbox import SandboxClock, SandboxDeadlines, SandboxFailed
from minutehand.domain.agent import Sandbox

T0 = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
SECOND = 1_000_000_000


def _sandbox(deadlines: dict[str, object], **fields: object) -> Sandbox:
    """A sandbox whose `deadlines` command prints `deadlines` and whose `advance` command records its step."""
    return Sandbox(
        deadlines=[sys.executable, "-c", f"print({json.dumps(deadlines)!r})"],
        advance=[
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')",
            "{log}",
            "{nanoseconds}",
        ],
        **fields,  # pyright: ignore[reportArgumentType]
    )


def _said(now: datetime, waits: dict[int, int]) -> dict[str, object]:
    return {
        "idle": True,
        "now_ns": int(now.timestamp() * SECOND),
        "tasks": len(waits),
        "timed": len(waits),
        "earliest_ns": min(waits.values()) if waits else -1,
        "waits": [{"task": task, "ns": ns} for task, ns in waits.items()],
    }


def test_the_next_wake_is_the_earliest_deadline_longer_than_housekeeping() -> None:
    said = SandboxDeadlines.model_validate(_said(T0, {1: 86_400 * SECOND, 2: SECOND // 2}))
    assert said.earliest(timedelta(seconds=1)) == 86_400 * SECOND  # the HTTP server's half-second poll is not it
    assert said.earliest(timedelta(milliseconds=100)) == SECOND // 2  # a floor the agent file lowers
    assert SandboxDeadlines.model_validate(_said(T0, {2: SECOND // 2})).earliest(timedelta(seconds=1)) == -1


async def test_a_sandbox_behind_the_run_is_brought_up_to_it_and_one_slightly_ahead_is_left(tmp_path) -> None:
    log = tmp_path / "advanced"
    behind = _sandbox(_said(T0 - timedelta(seconds=5), {}))
    behind = behind.model_copy(update={"advance": [p.replace("{log}", str(log)) for p in behind.advance]})
    await SandboxClock(behind).sync(T0)
    assert log.read_text().split() == [str(5 * SECOND)]

    ahead = _sandbox(_said(T0 + timedelta(seconds=2), {}))
    ahead = ahead.model_copy(update={"advance": [p.replace("{log}", str(log)) for p in ahead.advance]})
    await SandboxClock(ahead).sync(T0)
    assert log.read_text().split() == [str(5 * SECOND)]  # not moved back


async def test_a_sandbox_born_long_after_the_runs_start_is_refused() -> None:
    born_late = _sandbox(_said(T0 + timedelta(days=58), {}), settle_limit=timedelta(seconds=30))
    with pytest.raises(SandboxFailed, match="born at the run's start"):
        await SandboxClock(born_late).sync(T0)


async def test_a_backward_step_is_rejected() -> None:
    with pytest.raises(ValueError, match="never moved back"):
        await SandboxClock(_sandbox(_said(T0, {}))).advance(-1)
