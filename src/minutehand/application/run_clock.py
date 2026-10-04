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
