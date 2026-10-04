"""What the viewer's JSON API answers with: one model per path, so the page and its tests read a schema."""

from __future__ import annotations

from pydantic import AwareDatetime, Field

from minutehand.checks.patterns import pattern
from minutehand.checks.runner import RunResult
from minutehand.domain.checks import Effectiveness, Finding, Obligation, Pattern, WakeRecord
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.world import RecordedCall, WorldEvent
from minutehand.session import ForkPoint


class RunRow(Model):
    run_id: str
    scenario: str
    goal: str
    finished: bool = Field(description="False while another process is still writing the run")
    stop: StopReason | None
    failed: int = Field(description="Findings of kind fail; 0 until the run finishes")
    to_review: int
    parent_run: str | None
    forked_at: int | None
    forked_after_wake: int | None = Field(description="The wake whose end the fork was taken at")
    children: list[str]


class RunsResponse(Model):
    runs: list[RunRow] = Field(description="Root runs in the order their files sort, each root's forks after it")


class RunResponse(Model):
    run_id: str
    finished: bool
    scenario: Scenario = Field(description="As this run played it; a running fork shows its parent's")
    record: RunRecord | None = Field(description="None until the run finishes")
    reached: AwareDatetime = Field(description="The latest simulated moment the run has recorded")
    checkpoints: list[ForkPoint]


class WakesResponse(Model):
    wakes: list[WakeRecord]


class EventsResponse(Model):
    events: list[WorldEvent] = Field(description="Every change and read, without the run loop's own checkpoints")


class CallsResponse(Model):
    calls: list[RecordedCall]


class ObligationsResponse(Model):
    obligations: list[Obligation]


class ExplainedFinding(Model):
    number: int = Field(ge=1)
    finding: Finding
    pattern: Pattern | None


class FindingsResponse(Model):
    finished: bool
    findings: list[ExplainedFinding] = Field(description="Empty until the run finishes and is checked")
    blocked: list[str]
    notes: list[str]


class ScorecardResponse(Model):
    scorecard: Effectiveness | None = Field(description="None until the run finishes")


class Refusal(Model):
    error: str


def explained(result: RunResult) -> list[ExplainedFinding]:
    return [
        ExplainedFinding(number=i, finding=f, pattern=pattern(f.pattern) if f.pattern is not None else None)
        for i, f in enumerate(result.findings, start=1)
    ]
