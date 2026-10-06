"""The sandbox whose clock Minutehand owns (`Contained`): reading whether it is idle and when its earliest deadline
falls, and moving its clock with the run's."""

from __future__ import annotations

import asyncio
import json
import time

from pydantic import ConfigDict, Field

from minutehand.application.restore import Traffic
from minutehand.domain.agent import Contained
from minutehand.domain.scenario import Model


class SandboxFailed(RuntimeError):
    """A sandbox command could not be run, or did not answer as `Contained` says."""


HOUSEKEEPING_SLACK = 1.02
"""How far past a task's housekeeping period a deadline still reads as that housekeeping re-armed."""


class TaskWait(Model):
    """One blocked task's deadline, as the sandbox reports it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    task: int
    ns: int


class SandboxDeadlines(Model):
    """As `Contained.deadlines` prints them; anything else it prints (a task count) is the sandbox's, and ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    idle: bool
    earliest_ns: int = Field(description="Until the earliest deadline any blocked task waits for; -1 for none")
    waits: list[TaskWait] | None = Field(
        default=None,
        description="Every blocked task's deadline, when the sandbox says; None when it says only the earliest",
    )

    def earliest(self, quiet: dict[int, int]) -> int:
        """Until the earliest deadline that is not a task's own housekeeping: one no further than the period that
        task's timer was seen to fire at and do nothing (`quiet`, task to nanoseconds). A longer deadline on the
        same task is the agent's own timer. -1 for none."""
        if self.waits is None:
            return self.earliest_ns
        mine = [w.ns for w in self.waits if w.task not in quiet or w.ns > quiet[w.task] * HOUSEKEEPING_SLACK]
        return min(mine) if mine else -1

    def tasks_at(self, ns: int) -> set[int]:
        """The tasks whose deadline is `ns` away."""
        return {w.task for w in self.waits or [] if w.ns == ns}


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
    def __init__(self, source: Contained) -> None:
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
        """Move the sandbox's clock forward with the run's; nothing for a step of none."""
        if nanoseconds <= 0:
            return
        await _run([part.replace("{nanoseconds}", str(nanoseconds)) for part in self._source.advance])

    async def settle(self, traffic: Traffic | None) -> SandboxDeadlines | None:
        """Wait until the sandbox is idle on two reads `quiet` apart with no call of the agent's in flight, and
        answer its deadlines then; None when it did not fall idle within `settle_limit`."""
        give_up = time.monotonic() + self._source.settle_limit.total_seconds()  # clock-lint: exempt real-time limit
        quiet = self._source.quiet.total_seconds()
        while time.monotonic() < give_up:  # clock-lint: exempt real-time limit
            first = await self.deadlines()
            await asyncio.sleep(quiet)
            second = await self.deadlines()
            busy = traffic is not None and bool(traffic.waiting())
            if first.idle and second.idle and not busy:
                return second
        return None
