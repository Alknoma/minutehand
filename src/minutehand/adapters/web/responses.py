"""What the viewer's JSON API answers with: one model per path, so the page and its tests read a schema."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from minutehand.application.forks import ForkAccount
from minutehand.application.model_calls import EventTrace, JoinedBy
from minutehand.checks.patterns import pattern
from minutehand.checks.runner import RunResult
from minutehand.domain.checks import Effectiveness, Finding, Obligation, Pattern, WakeRecord
from minutehand.domain.run import RunRecord, StopReason, Verdict, VerdictKind
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.telemetry import ForwardFailure, StoredSpan
from minutehand.domain.world import Actor, RecordedCall, WorldEvent
from minutehand.session import ForkPoint


class RunRow(Model):
    run_id: str
    scenario: str
    goal: str
    finished: bool = Field(description="False while another process is still writing the run")
    stop: StopReason | None
    verdict: VerdictKind | None = Field(description="None until the run finishes")
    failed: int = Field(description="Findings of kind fail; 0 until the run finishes")
    to_review: int
    parent_run: str | None
    forked_at: int | None
    forked_after_wake: int | None = Field(description="The wake whose end the fork was taken at")
    forked_ran_on: bool = Field(
        default=False, description="The fork was taken at a later checkpoint of that wake: the clock had run on"
    )
    changed: str | None = Field(description="For a fork, what it changed, in a few words; None for a root run")
    children: list[str]
    case: str | None = Field(default=None, description="A case: the label its worlds were opened under")
    worlds: list[str] = Field(default=[], description="A case: its worlds, read here as this one run")


class RunsResponse(Model):
    runs: list[RunRow] = Field(description="Root runs in the order their files sort, each root's forks after it")


class RunResponse(Model):
    run_id: str
    finished: bool
    scenario: Scenario = Field(description="As this run played it; a running fork shows its parent's")
    record: RunRecord | None = Field(description="None until the run finishes")
    reached: AwareDatetime = Field(description="The latest simulated moment the run has recorded")
    checkpoints: list[ForkPoint]
    driven: bool = Field(
        default=False, description="Driven from outside (`minutehand serve`): its wakes are steps, marked or inferred"
    )
    fork: ForkAccount | None = Field(
        description="For a fork: where it split, what it changed, whether its restore was verified, and how its "
        "outcome differs from its parent's; None for a run started from the beginning"
    )


class MessageChange(StrEnum):
    SENT = "sent"
    EDITED = "edited"  # rewritten in place after it was sent
    DELETED = "deleted"


class WrittenBy(Model):
    """The model call that wrote a message, and how it was found (`application.model_calls.JoinedBy`)."""

    span_id: str
    model: str | None
    joined_by: JoinedBy


class MessageLine(Model):
    """One message as a person reads the record: when, to whom, what it said, and for an edit what it said before."""

    seq: int
    at: AwareDatetime = Field(description="Simulated")
    wake: int
    actor: Actor
    change: MessageChange
    to: list[str] = Field(description="The people it reached, by name; empty: it reached nobody in the scenario")
    text: str
    before: str | None = Field(description="An edit: the text it replaced")
    thread: bool = Field(description="Sent in a thread")
    written_by: WrittenBy | None = Field(default=None, description="The agent's model call that wrote it, if joined")


class MessagesResponse(Model):
    messages: list[MessageLine] = Field(description="Every message created, edited or deleted, in record order")


class TrafficCall(Model):
    """One model call the run holds a span of."""

    span_id: str
    trace_id: str
    step: int = Field(description="The wake or step it is placed in")
    started: AwareDatetime = Field(description="Real time")
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    wrote: list[int] = Field(description="Seqs of the messages it is joined to as their writer")
    joined_by: JoinedBy | None = Field(description="How it was joined to the first of them")


class HostTraffic(Model):
    """The calls to one model host relayed unopened on tunnels: counted, never read."""

    host: str
    calls: int = Field(ge=0, description="Bursts on its tunnels")
    connections: int = Field(ge=0)
    bytes_sent: int = Field(ge=0)
    bytes_received: int = Field(ge=0)


class ModelTrafficResponse(Model):
    calls: list[TrafficCall] = Field(description="Every model call a span was received or recorded of, by start")
    hosts: list[HostTraffic] = Field(description="Model hosts reached on tunnels never opened, one line each")


class WakesResponse(Model):
    wakes: list[WakeRecord]


class EventsResponse(Model):
    events: list[WorldEvent] = Field(description="Every change and read, without the run loop's own checkpoints")


class CallsResponse(Model):
    calls: list[RecordedCall]


class FellDue(Model):
    """One moment a wait fell due while it was still open, exactly as the scorecard counts it."""

    at: AwareDatetime
    until: AwareDatetime = Field(
        description="The follow-up at or after it; with none, when the wait settled or the run ended"
    )
    followed_up: bool
    late: bool = Field(description="Followed up more than the grace after, or never")


class DrawnWait(Model):
    obligation: Obligation
    fell_due: list[FellDue] = Field(
        description="Empty for a wait that never fell due while open, and for the scenario's deadline"
    )


class ObligationsResponse(Model):
    obligations: list[DrawnWait] = Field(
        description="What the world was waiting on (`checks.ledger`), each with the moments it fell due as "
        "`checks._waits.chases` reads them for the scorecard: the viewer draws nothing as overdue on its own"
    )


class ExplainedFinding(Model):
    number: int = Field(ge=1)
    finding: Finding
    pattern: Pattern | None


class FindingsResponse(Model):
    finished: bool
    verdict: Verdict | None = Field(description="None until the run finishes")
    findings: list[ExplainedFinding] = Field(description="Empty until the run finishes and is checked")
    blocked: list[str]
    notes: list[str]


class ScorecardResponse(Model):
    scorecard: Effectiveness | None = Field(description="None until the run finishes")


class TraceResponse(Model):
    trace_id: str
    spans: list[StoredSpan] = Field(description="The agent's spans of this trace the run received, as they arrived")


class ModelCallsResponse(Model):
    received: int = Field(description="Spans of the agent's own telemetry the run received; 0 means none")
    forward_failures: list[ForwardFailure] = Field(
        description="Payloads that could not be passed on to where the agent's telemetry went before"
    )
    events: list[EventTrace] = Field(
        description="For each event a finding cites, the agent's spans behind it and the model call that led to it; "
        "empty until the run finishes"
    )


class Refusal(Model):
    error: str


def explained(result: RunResult) -> list[ExplainedFinding]:
    return [
        ExplainedFinding(number=i, finding=f, pattern=pattern(f.pattern) if f.pattern is not None else None)
        for i, f in enumerate(result.findings, start=1)
    ]
