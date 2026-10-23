"""The sandbox every agent runs in, whose clock Minutehand owns (`domain.agent.Sandbox`): reading whether it is idle
and when its earliest deadline falls, and moving its clock with the run's."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

from pydantic import ConfigDict, Field

from minutehand.application.refusals import AgentFailed
from minutehand.application.traffic import Traffic
from minutehand.domain.agent import Sandbox
from minutehand.domain.scenario import Model
from minutehand.domain.templates import STARTS_AT_VARIABLE


class SandboxFailed(AgentFailed):
    """A sandbox command could not be run, or did not answer as `Sandbox` says: the agent's sandbox is the agent,
    and one that is gone (its program exited, and the sandbox with it) or cannot be read is the agent failed."""


class TaskWait(Model):
    """One blocked task's deadline, as the sandbox reports it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    task: int
    ns: int


class SandboxDeadlines(Model):
    """As `Sandbox.deadlines` prints them; anything else it prints (a task count) is the sandbox's, and ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    idle: bool
    now_ns: int = Field(description="The sandbox's realtime clock, nanoseconds since the epoch")
    earliest_ns: int = Field(description="Until the earliest deadline any blocked task waits for; -1 for none")
    waits: list[TaskWait] | None = Field(
        default=None,
        description="Every blocked task's deadline, when the sandbox says; None when it says only the earliest",
    )

    def now(self) -> datetime:
        """The sandbox's realtime clock, as an instant."""
        return datetime.fromtimestamp(self.now_ns // 1_000_000_000, UTC) + timedelta(
            microseconds=(self.now_ns % 1_000_000_000) / 1000
        )

    def earliest(self, housekeeping: timedelta) -> int:
        """Until the earliest deadline longer than `housekeeping` (`Sandbox.housekeeping`): a shorter one is a
        runtime's own timer (an HTTP server's poll), which fires in real time while the sandbox is read as idle and
        is no wake. -1 for none."""
        floor = housekeeping.total_seconds() * 1_000_000_000
        waits = [w.ns for w in self.waits] if self.waits is not None else [self.earliest_ns]
        mine = [ns for ns in waits if ns > floor]
        return min(mine) if mine else -1


async def _run(argv: list[str]) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as e:
        raise SandboxFailed(f"{argv[0]}: {e}") from e
    out, err = await process.communicate()
    if process.returncode != 0:
        raise SandboxFailed(f"{' '.join(argv)} exited {process.returncode}: {err.decode(errors='replace').strip()}")
    return out.decode(errors="replace")


class SandboxClock:
    def __init__(self, source: Sandbox) -> None:
        self._source = source

    async def deadlines(self) -> SandboxDeadlines:
        said = await _run(self._source.deadlines)
        lines = [line for line in said.splitlines() if line.strip().startswith("{")]
        if not lines:
            raise SandboxFailed(f"{' '.join(self._source.deadlines)} printed no deadlines: {said.strip()[-300:]}")
        try:
            return SandboxDeadlines.model_validate(json.loads(lines[-1]))
        except ValueError as e:
            raise SandboxFailed(f"{' '.join(self._source.deadlines)} printed {lines[-1]!r}: {e}") from e

    async def advance(self, nanoseconds: int) -> None:
        """Move the sandbox's clock forward with the run's; nothing for a step of none. Never back: a timer the
        agent armed from its clock before a backward step would fire that much early."""
        if nanoseconds < 0:
            raise ValueError(f"a sandbox's clock is never moved back ({nanoseconds} ns)")
        if nanoseconds == 0:
            return
        await _run([part.replace("{nanoseconds}", str(nanoseconds)) for part in self._source.advance])

    async def sync(self, to: datetime) -> None:
        """Bring the sandbox's clock up to the run's, `to`, when it is behind. A sandbox ahead of `to` is left
        where it is, and the run's clock catches up to it when its wake ends (`Orchestrator._plan_timer`): the
        real time an agent spends working passes on its clock. One ahead by more than `settle_limit` was not born at
        the run's start, and is refused: moved back, its timers would fire early."""
        said = await self._once_up()
        step = int(to.timestamp() * 1_000_000_000) - said.now_ns
        if step < 0 and -step > self._source.settle_limit.total_seconds() * 1_000_000_000:
            raise SandboxFailed(
                f"the agent's sandbox clock reads {said.now().isoformat()}, after the run's {to.isoformat()}: a "
                f"sandbox is born at the run's start ({STARTS_AT_VARIABLE} in the agent's command's environment) "
                "and its clock is only ever moved forward"
            )
        if step > 0:
            await self.advance(step)

    async def _once_up(self) -> SandboxDeadlines:
        """The sandbox's deadlines once it answers: the agent's command creates the sandbox, and the first read may
        come before it exists. Read again, `quiet` apart, up to `settle_limit`; then the last refusal stands."""
        give_up = time.monotonic() + self._source.settle_limit.total_seconds()  # clock-lint: exempt real-time limit
        while True:
            try:
                return await self.deadlines()
            except SandboxFailed:
                if time.monotonic() >= give_up:  # clock-lint: exempt real-time limit
                    raise
                await asyncio.sleep(self._source.quiet.total_seconds())

    async def settle(self, traffic: Traffic | None) -> SandboxDeadlines | None:
        """Wait until the sandbox has settled, and answer its deadlines then; None when it did not within
        `settle_limit`. Settled is: idle on two reads `quiet` apart, no call of the agent's in flight at either read,
        and none sent or answered between them (`Traffic.activity` unchanged). While a call to a real host is in
        flight the agent's tasks are blocked on its socket and every read is idle, so `waiting` empty at one
        instant says only that the instant fell between one call's answer and the next call."""
        give_up = time.monotonic() + self._source.settle_limit.total_seconds()  # clock-lint: exempt real-time limit
        quiet = self._source.quiet.total_seconds()
        while time.monotonic() < give_up:  # clock-lint: exempt real-time limit
            before = _moving(traffic)
            first = await self.deadlines()
            await asyncio.sleep(quiet)
            second = await self.deadlines()
            after = _moving(traffic)
            still = before == after and not before[1]
            if first.idle and second.idle and still:
                return second
        return None


def _moving(traffic: Traffic | None) -> tuple[int, bool]:
    """What `traffic` reads now: its activity count, and whether any call of the agent's is in flight."""
    if traffic is None:
        return 0, False
    return traffic.activity(), bool(traffic.waiting())
