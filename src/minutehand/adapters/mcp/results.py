"""What each MCP tool answers with. Every result is a model, so a coding agent reads a schema, not prose."""

from __future__ import annotations

from pydantic import AwareDatetime, Field

from minutehand.application.model_calls import JoinedBy, ModelCall
from minutehand.domain.checks import Effectiveness, FindingKind, Pattern, Severity, Stability, WakeRecord
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Expectation, Model, Person
from minutehand.domain.world import Actor, EntityRef, Operation, Snapshot
from minutehand.session import ForkPoint


class ScenarioFile(Model):
    path: str
    name: str
    goal: str = Field(description="The text handed to the agent, verbatim")
    people: list[Person]
    expectations: list[Expectation] = Field(description="What must be true of the world for a run to be right")


class NotAScenario(Model):
    path: str
    reason: str = Field(description="Why it was not read as a scenario: often it is an agent or fork file")


class ScenarioListing(Model):
    directory: str
    scenarios: list[ScenarioFile]
    not_scenarios: list[NotAScenario] = Field(description="YAML or JSON files under the directory that are not one")


class FindingCounts(Model):
    fail: int = Field(description="Findings that fail the run: something the agent did wrong")
    review: int = Field(description="Findings a person or agent should look at: possibly wrong")
    informational: int


class PlayedRun(Model):
    run_id: str
    scenario: str
    parent_run: str | None = Field(description="The run this one was forked from, if any")
    forked_at: int | None = Field(description="The checkpoint seq it was forked at")
    stop: StopReason = Field(
        description=(
            "agent_done: the agent said it was done. wake_limit: the scenario's wake limit was reached. "
            "deadline_passed: the clock reached the deadline. nothing_pending: nothing more was due and the "
            "agent asked for no wake. agent_failed: the agent could not be reached or answered with an error."
        )
    )
    stopped_at: AwareDatetime = Field(description="Simulated time the run ended")
    passed: bool = Field(description="True when no finding is a failure")
    findings: FindingCounts
    blocked: list[str] = Field(description="Checks that could not read their input and did not run")
    scorecard: Effectiveness
    checkpoints: list[ForkPoint] = Field(description="Where rerun_from may restart this run: a seq per wake end")


class RunsPlayed(Model):
    runs: list[PlayedRun]
    stability: Stability | None = Field(
        default=None, description="With several samples: how many passed. A rate below 1 is the agent's variance"
    )


class NumberedFinding(Model):
    number: int = Field(ge=1, description="Pass this to show_evidence")
    check: str
    kind: FindingKind
    severity: Severity
    message: str
    at: AwareDatetime | None = Field(description="Simulated time")
    wake: int | None = Field(description="The agent wake it happened in")
    evidence: list[int] = Field(description="Seqs of the world events it cites")
    pattern: str | None = Field(description="Key of the design pattern that avoids this failure")
    pattern_title: str | None


class FindingList(Model):
    run_id: str
    findings: list[NumberedFinding]
    blocked: list[str]


class RecordedHttp(Model):
    """The HTTP call that produced a world event, as the proxy recorded it."""

    method: str
    host: str
    path: str
    status: int
    request_body: str | None = Field(description="Only when it was stored")
    response_body: str | None = Field(description="Only when it was stored")
    trace_id: str | None = Field(description="The caller's W3C trace id, when the call carried a traceparent")


class CitedEvent(Model):
    """One change in the world a finding points at."""

    seq: int
    wake: int = Field(description="0 is the scenario's setup")
    at: AwareDatetime = Field(description="Simulated time")
    actor: Actor
    operation: Operation
    entity: EntityRef
    after: Snapshot | None = Field(description="What the entity looked like after the change")
    call: RecordedHttp | None
    model_call: ModelCall | None = Field(
        description="The agent's model call that led to this event, from the telemetry the run received: the "
        "model, what it was asked and answered when the span carries the messages, and the token counts"
    )
    joined_by: JoinedBy | None = Field(
        description="trace: the call's traceparent put both in one trace. wake: no trace joined them; this is the "
        "last model call of the same wake before the event, the nearest rather than a proven cause"
    )
    agent_spans: list[str] = Field(
        description="Names of the agent's spans the call came from: the calling span first, then its ancestors up "
        "to the root"
    )


class Evidence(Model):
    run_id: str
    finding: NumberedFinding
    events: list[CitedEvent]
    wakes: list[WakeRecord] = Field(description="The wake the finding and its events happened in")
    telemetry: str = Field(description="Whether the run received any of the agent's own telemetry, in words")
    pattern: Pattern | None = Field(description="What goes wrong, and the design that stops it")


class ListedRun(Model):
    run_id: str
    scenario: str
    stop: StopReason
    stopped_at: AwareDatetime
    passed: bool
    findings: FindingCounts
    parent_run: str | None
    forked_at: int | None
    children: list[str] = Field(description="Runs forked from this one")


class RunListing(Model):
    runs: list[ListedRun]
