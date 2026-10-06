"""The one clock in a run."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Simulated time. Every timestamp a provider writes comes from here."""
        ...

    def wake(self) -> int:
        """The agent wake in progress; 0 while the scenario is being set up."""
        ...
