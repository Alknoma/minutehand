"""What a finished run is, for the record."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from minutehand.domain.checks import WakeRecord
from minutehand.domain.scenario import Model


class StopReason(StrEnum):
    AGENT_DONE = "agent_done"  # the agent reported it had finished
    WAKE_LIMIT = "wake_limit"  # Scenario.max_wakes reached
    DEADLINE_PASSED = "deadline_passed"  # the clock passed the scenario's deadline
    NOTHING_PENDING = "nothing_pending"  # nothing is due and the agent named no next wake: it is stuck
    AGENT_FAILED = "agent_failed"  # the agent could not be reached or answered with an error


class RunRecord(Model):
    run_id: str
    scenario: str
    seed: int
    parent_run: str | None = None
    forked_at: int | None = Field(default=None, description="WorldEvent.seq shared with the parent")
    started_at: AwareDatetime = Field(description="Simulated")
    ended_at: AwareDatetime = Field(description="Simulated")
    wall_seconds: float = Field(ge=0)
    stop: StopReason
    failure: str | None = Field(default=None, description="Why, when the run stopped AGENT_FAILED")
    wakes: list[WakeRecord]
