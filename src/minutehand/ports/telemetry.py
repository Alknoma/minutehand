"""What the run tells whoever is listening. The store is the record; this is an export of it."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from minutehand.domain.agent import WakeReason
from minutehand.domain.checks import Effectiveness, Finding
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import RecordedCall, WorldEvent


class Telemetry(Protocol):
    def run_started(self, run_id: str, scenario: Scenario) -> None: ...

    def wake_started(self, wake: int, reason: WakeReason, now: datetime) -> None: ...

    def recorded(self, event: WorldEvent) -> None:
        """One event entered the world. Called after `Store.apply` and, when there is one, after `Store.attach`."""
        ...

    def captured(self, call: RecordedCall) -> None:
        """One call to a host no provider claims was captured (`call.exchange.captured`); never its bodies."""
        ...

    def wake_ended(self, wake: int) -> None: ...

    def found(self, finding: Finding) -> None: ...

    def run_ended(self, record: RunRecord, effectiveness: Effectiveness | None) -> None: ...
