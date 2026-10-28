"""The sandbox's clock as Minutehand drives it (`application/sandbox.py`): that it is read only for whether it is idle
and what its clock says, never for a wake, and that the clock is only ever moved forward. The control calls here are
real commands that print what the patched gVisor's `runsc debug --deadlines` prints; nothing starts a sandbox."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from minutehand.application.sandbox import SandboxClock, SandboxFailed, SandboxReading
from minutehand.application.traffic import SeenCall
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


async def test_the_sandbox_is_read_for_whether_it_is_idle_its_clock_and_whether_it_is_held_and_nothing_else() -> None:
    reading = await SandboxClock(_sandbox(_said(T0, {1: 86_400 * SECOND, 2: SECOND // 2}))).read()
    assert reading.model_dump() == {"idle": True, "now_ns": int(T0.timestamp() * SECOND), "held": False}
    assert reading.now() == T0
    held = await SandboxClock(_sandbox({**_said(T0, {}), "held": True})).read()
    assert held.held


def _logging(log: Path, word: str) -> list[str]:
    """A control call that appends `word` to `log`."""
    return [sys.executable, "-c", "import sys; open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')", str(log), word]


async def test_hold_and_release_run_the_agent_files_commands_and_hold_answers_the_held_reading(tmp_path: Path) -> None:
    log = tmp_path / "called"
    source = _sandbox({**_said(T0, {}), "held": True}, hold=_logging(log, "hold"), release=_logging(log, "release"))
    clock = SandboxClock(source)
    assert clock.holds
    reading = await clock.hold()
    await clock.release()
    assert log.read_text().split() == ["hold", "release"]
    assert reading.held and reading.now() == T0


async def test_a_hold_the_sandbox_does_not_report_is_refused(tmp_path: Path) -> None:
    """A `runsc` built without the hold that accepts the call and leaves the clock running."""
    source = _sandbox(_said(T0, {}), hold=_logging(tmp_path / "x", "hold"), release=_logging(tmp_path / "x", "go"))
    with pytest.raises(SandboxFailed, match="reads as not held"):
        await SandboxClock(source).hold()


def test_a_hold_without_a_release_is_refused() -> None:
    with pytest.raises(ValueError, match="`hold` and `release` are named together or not at all"):
        Sandbox.model_validate({"deadlines": ["runsc"], "advance": ["runsc", "{nanoseconds}"], "hold": ["runsc"]})


async def test_a_sandbox_without_a_hold_does_not_hold() -> None:
    clock = SandboxClock(_sandbox(_said(T0, {})))
    assert not clock.holds
    with pytest.raises(ValueError, match=r"names no `sandbox\.hold`"):
        await clock.hold()


def test_a_stale_housekeeping_key_is_refused() -> None:
    with pytest.raises(ValueError, match=r"`sandbox\.housekeeping` is gone: no deadline in the sandbox is a wake"):
        Sandbox.model_validate({"deadlines": ["runsc"], "advance": ["runsc", "{nanoseconds}"], "housekeeping": "PT1S"})


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


class _Calls:
    """`application.traffic.Traffic` as the proxy keeps it: the agent's calls sent on to a real host and not yet
    answered, and a count that rises as each is sent or answered."""

    def __init__(self) -> None:
        self._sent: list[str] = []
        self._moved = 0

    def send(self, what: str) -> None:
        self._sent.append(what)
        self._moved += 1

    def answer(self, what: str) -> None:
        self._sent.remove(what)
        self._moved += 1

    def last_call(self) -> SeenCall | None:
        return None

    def waiting(self) -> list[str]:
        return list(self._sent)

    def activity(self) -> int:
        return self._moved


class _Blocked(SandboxClock):
    """A sandbox whose every task is blocked on every read, as an agent's are on its socket while a call is out;
    after each first read of a pair, `between` runs, as what the agent does before the second."""

    def __init__(self, between: Callable[[], None]) -> None:
        super().__init__(_sandbox(_said(T0, {}), quiet=timedelta(milliseconds=5), settle_limit=timedelta(seconds=0.2)))
        self._between = between
        self._reads = 0

    async def read(self) -> SandboxReading:
        self._reads += 1
        if self._reads % 2 == 1:
            self._between()
        return SandboxReading.model_validate(_said(T0, {}))


async def test_settle_does_not_answer_idle_when_calls_moved_between_its_two_reads() -> None:
    # Between the reads the agent's tool loop has its model call answered and sends the next, which is answered
    # too: nothing is in flight at either read, and every task reads blocked throughout.
    calls = _Calls()

    def turn() -> None:
        for _ in range(2):
            calls.send("POST model.example/v1/messages")
            calls.answer("POST model.example/v1/messages")

    settled = await _Blocked(turn).settle(calls)
    assert calls.waiting() == []
    assert settled is None, (
        "settle answered the sandbox idle although the agent's calls were sent and answered between its two "
        "reads: it sampled waiting() at one instant, which fell in the gap between one call's answer and the next"
    )


async def test_settle_answers_idle_when_no_call_moved_between_its_two_reads() -> None:
    calls = _Calls()
    settled = await _Blocked(lambda: None).settle(calls)
    assert settled is not None and settled.idle
