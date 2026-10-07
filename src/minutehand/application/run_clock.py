"""The run's clock. The orchestrator moves it; everything else reads it."""

from __future__ import annotations

from datetime import datetime


class RunClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("the clock needs a timezone-aware start")
        self._now = start
        self._wake = 0

    def now(self) -> datetime:
        return self._now

    def wake(self) -> int:
        return self._wake

    def jump(self, to: datetime) -> None:
        if to < self._now:
            raise ValueError(f"the clock only moves forward: {to.isoformat()} is before {self._now.isoformat()}")
        self._now = to

    def begin_wake(self) -> int:
        self._wake += 1
        return self._wake

    def withdraw(self, wake: int) -> None:
        """Take back `wake`, the one just begun, when nothing was stamped with it: a contained agent's timer that
        fired and did nothing (a runtime's own housekeeping) was no wake of the agent's."""
        if wake != self._wake or wake == 0:
            raise ValueError(f"only the wake just begun can be withdrawn, not {wake} while in {self._wake}")
        self._wake -= 1

    def enter(self, wake: int) -> None:
        """Enter wake `wake`, skipping any between: a standing world's step, numbered by the case it belongs to."""
        if wake <= self._wake:
            raise ValueError(f"wakes only move forward: {wake} is not after {self._wake}")
        self._wake = wake
