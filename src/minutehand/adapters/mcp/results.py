"""What each MCP tool answers with. Every result is a model, so a coding agent reads a schema, not prose."""

from __future__ import annotations

from pydantic import AwareDatetime, Field

from minutehand.adapters.query.reader import Value
from minutehand.adapters.query.schema import View
from minutehand.application.forks import ForkAccount
from minutehand.application.model_calls import JoinedBy, ModelCall
from minutehand.domain.checks import Effectiveness, FindingKind, Pattern, Severity, Stability, WakeRecord
from minutehand.domain.run import OutboundUse, StopReason, Verdict
from minutehand.domain.scenario import Expectation, Model, Person
from minutehand.domain.world import Actor, Captured, EntityRef, Operation, Snapshot
from minutehand.session import ForkPoint

_VERDICT = (
    "passed: no check failed and the agent finished (it reported done, or nothing was left open). failed: a "
    "check failed. unfinished: no check failed, but the run stopped without the agent reporting done while a "
    "wait or a commitment was still open; `words` says how it stopped and what was open. tool_failed: Minutehand "
    "itself failed while answering a call, so the run is not scored; `words` names the first such call"
)

_FORK = (
    "For a rerun: the checkpoint it split from (after which wake, at what simulated time), what it changed in words, "
    "whether its agent was verified and by what (memory, report) or why not, and against its parent from the "
    "split on: both verdicts, each scorecard line that differs, findings gained and lost, and the first thing in "
    "the record at which the two part, with which kind of thing it is (`first_divergence.kind`: a change in the world, "
    "a call the agent made, the agent's report at a wake's end, or a model call) and what about it differs. Null for "
    "a run played from the beginning"
)


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
            "agent asked for no wake. agent_failed: the agent could not be reached or answered with an error. "
            "closed: a standing world served by `minutehand serve` was closed by whoever opened it."
        )
    )
    stopped_at: AwareDatetime = Field(description="Simulated time the run ended")
    verdict: Verdict = Field(description=_VERDICT)
    findings: FindingCounts
    blocked: list[str] = Field(description="Checks that could not read their input and did not run")
    scorecard: Effectiveness
    checkpoints: list[ForkPoint] = Field(description="Where rerun_from may restart this run: a seq per wake end")
    fork: ForkAccount | None = Field(description=_FORK)


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
    verdict: Verdict = Field(description=_VERDICT)
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
    captured: Captured | None = Field(
        default=None,
        description="Set for a call to a host no provider claims that the agent file declares outbound: its mode "
        "(acknowledge, pass_through, replay, discovered), what answered it (declaration, real_host, recording, "
        "refusal), the recording a replay came from, and its real start and end",
    )


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
    verdict: Verdict = Field(description=_VERDICT)
    findings: FindingCounts
    parent_run: str | None
    forked_at: int | None
    children: list[str] = Field(description="Runs forked from this one")
    fork: ForkAccount | None = Field(description=_FORK)
    worlds: list[str] = Field(
        default=[], description="A case: the standing worlds it is made of, scored here as this one run"
    )


class RunListing(Model):
    runs: list[ListedRun]


class OutboundCall(Model):
    """One call to a host no provider claims, as the run kept it: bodies redacted, cut at the declared size."""

    wake: int
    at: AwareDatetime = Field(description="Simulated time")
    call: RecordedHttp
    events: list[int] = Field(description="Seqs of the world events it wrote: a send read as a message")


class OutboundCalls(Model):
    run_id: str
    hosts: list[OutboundUse] = Field(
        description="Per host: how many calls, how it was declared (mode null: refused, nobody declares it), how "
        "many were replayed or refused, and addresses its sends named that match no person"
    )
    calls: list[OutboundCall] = Field(description="Every captured call, in order; refused calls are not here")


class ReadModelSchema(Model):
    version: int = Field(description="The read model's version: a column removed, renamed or changed is a new one")
    views: list[View] = Field(description="Every view, its columns in order, and the order its rows come in")


class QueryAnswer(Model):
    run_id: str = Field(description="The run read, its id in full")
    columns: list[str]
    rows: list[list[Value]] = Field(description="One page of rows, each a list of values in the order of `columns`")
    offset: int = Field(description="Rows skipped before this page")
    more: bool = Field(description="True when more rows follow this page")
    next_offset: int | None = Field(description="The offset to ask for the next page; None when this page is the last")
