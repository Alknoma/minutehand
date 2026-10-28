"""The run's clock. The orchestrator moves it; everything else reads it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime


class RunClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("the clock needs a timezone-aware start")
        self._now = start
        self._wake = 0
        self._agent: Callable[[], Awaitable[datetime]] | None = None

    def now(self) -> datetime:
        return self._now

    def wake(self) -> int:
        return self._wake

    async def agent_now(self) -> datetime:
        """`ports.clock.Clock.agent_now`: the agent's clock as `reads_agent` reads it, else the run's."""
        return self._now if self._agent is None else await self._agent()

    def reads_agent(self, read: Callable[[], Awaitable[datetime]]) -> None:
        """Read the agent's own clock by `read` from here on: the sandbox's, for a run whose agent has one."""
        self._agent = read

    def jump(self, to: datetime) -> None:
        if to < self._now:
            raise ValueError(f"the clock only moves forward: {to.isoformat()} is before {self._now.isoformat()}")
        self._now = to

    def begin_wake(self) -> int:
        self._wake += 1
        return self._wake

    def enter(self, wake: int) -> None:
        """Enter wake `wake`, skipping any between: a standing world's step, numbered by the case it belongs to."""
        if wake <= self._wake:
            raise ValueError(f"wakes only move forward: {wake} is not after {self._wake}")
        self._wake = wake
