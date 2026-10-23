"""The sandbox's clock as Minutehand drives it (`application/sandbox.py`): which deadline is the agent's next wake,
and that the clock is only ever moved forward. The control calls here are real commands that print what the patched
gVisor's `runsc debug --deadlines` prints; nothing starts a sandbox."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from minutehand.application.sandbox import SandboxClock, SandboxDeadlines, SandboxFailed
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

    async def deadlines(self) -> SandboxDeadlines:
        self._reads += 1
        if self._reads % 2 == 1:
            self._between()
        return SandboxDeadlines.model_validate(_said(T0, {}))


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
