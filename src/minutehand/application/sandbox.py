"""The sandbox whose clock Minutehand owns (`Contained`): reading whether it is idle and when its earliest deadline
falls, and moving its clock with the run's."""

from __future__ import annotations

import asyncio
import json
import time

from pydantic import Field

from minutehand.application.restore import Traffic
from minutehand.domain.agent import Contained
from minutehand.domain.scenario import Model


class SandboxFailed(RuntimeError):
    """A sandbox command could not be run, or did not answer as `Contained` says."""


class SandboxDeadlines(Model):
    idle: bool
    earliest_ns: int = Field(description="Until the earliest deadline any blocked task waits for; -1 for none")


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
