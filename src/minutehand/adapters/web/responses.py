"""What the viewer's JSON API answers with: one model per path, so the page and its tests read a schema."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from minutehand.application.forks import ForkAccount
from minutehand.application.model_calls import EventTrace, JoinedBy, ModelCall
from minutehand.checks.patterns import pattern
from minutehand.checks.runner import RunResult
from minutehand.domain.assessments import Rule
from minutehand.domain.checks import Effectiveness, Finding, FindingKind, Obligation, Pattern, WakeRecord
from minutehand.domain.clock import DueEntry
from minutehand.domain.conversation import PersonCall
from minutehand.domain.people import PersonReply, Writing
from minutehand.domain.run import RunRecord, StopReason, Verdict, VerdictKind
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.telemetry import ForwardFailure, SpanStatus, StoredSpan
from minutehand.domain.world import Actor, CallOutcome, Operation, RecordedCall, Snapshot, WorldEvent
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
    ASKED = "asked"  # an item left waiting on a person in the agent's own product
    DECIDED = "decided"  # the person decided it
    REFUSED = "refused"  # the person decided it, and the product did not take the decision
    WITHDRAWN = "withdrawn"  # the agent took it back undecided


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
    words: str | None = Field(
        default=None, description="An item in the agent's own product, said whole: 'asked Nadia Ek to approve: ...'"
    )


class MessagesResponse(Model):
    messages: list[MessageLine] = Field(description="Every message created, edited or deleted, in record order")


class TrafficCall(Model):
    """One model call the run holds a span of."""

    span_id: str
    trace_id: str
    step: int = Field(description="The wake or step it is placed in")
    started: AwareDatetime = Field(description="Real time")
    ended: AwareDatetime = Field(description="Real time: with `started`, how long the model took to answer")
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


class StepActivity(Model):
    """What one wake or step of the run did in real time, as far as its record and the agent's spans show it."""

    step: int = Field(ge=0, description="The wake or step its events and spans are placed in; 0 is setup")
    began: AwareDatetime = Field(description="Real time: the earliest event written or span started in it")
    ended: AwareDatetime = Field(description="Real time: the latest event written or span ended in it")
    events: int = Field(ge=0, description="Events of the world's log placed in it, reads included")
    spans: int = Field(ge=0, description="Spans of the agent's telemetry placed in it")
    model_calls: int = Field(ge=0, description="Of those spans, the calls to a model")


class StepsResponse(Model):
    steps: list[StepActivity] = Field(
        description="Every wake or step that holds an event or a span, by number; a step whose record says it "
        "happened and that holds neither is not listed"
    )


class SpanBar(Model):
    """One span of the agent's telemetry, without its attributes: what a waterfall draws."""

    span_id: str
    parent_span_id: str | None
    trace_id: str
    name: str
    service: str | None
    start: AwareDatetime = Field(description="Real time")
    end: AwareDatetime = Field(description="Real time")
    status: SpanStatus
    model_call: bool = Field(description="A call to a model (`application.model_calls.is_model_call`)")


class StepSpansResponse(Model):
    step: int
    spans: list[SpanBar] = Field(description="Every span placed in the step, by start; log events left out")


class ModelCallResponse(Model):
    call: ModelCall = Field(description="What the model was asked and answered, as its span carries them")
    wrote: list[int] = Field(description="Seqs of the messages this call is joined to as their writer")


class WakesResponse(Model):
    wakes: list[WakeRecord]


class EventsResponse(Model):
    events: list[WorldEvent] = Field(description="Every change and read, without the run loop's own checkpoints")


class CallsResponse(Model):
    calls: list[RecordedCall]


class DrawnWait(Model):
    obligation: Obligation
    touched_at: list[AwareDatetime] = Field(
        default=[], description="Simulated moments of `obligation.agent_touches`, in their order"
    )
    came_back_at: AwareDatetime | None = Field(
        default=None, description="Simulated moment of `obligation.first_touch_after_settled`"
    )


class ObligationsResponse(Model):
    obligations: list[DrawnWait] = Field(
        description="What the world was waiting on (`checks.ledger`), as facts: when each opened, what the agent did "
        "while it was open, and when it settled. Whether anything was late is the team's rules' to say, in the findings"
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


# -- the timeline: every mark of the run, compact, for a page that bins and draws them itself ----------------------


class LaneKind(StrEnum):
    WAKES = "wakes"  # the agent's wakes, or a driven run's steps
    PERSON = "person"  # what passed between the agent and one person, and what the agent waited on them for
    PROVIDER = "provider"  # the calls to one provider, and the changes in it nobody's call made
    HOST = "host"  # the calls to one host no provider claims
    DISPATCH = "dispatch"  # the run loop's table of what is due next
    AGENT_MODEL = "agent_model"  # the agent's model calls, from its telemetry
    PEOPLE_MODEL = "people_model"  # the calls Minutehand made to a model to write what people say
    MEMORY = "memory"  # the agent's own memory
    STORED = "stored"  # items kept for hosts declared `store`
    SPANS = "spans"  # the agent's spans that are not model calls


class MarkKind(StrEnum):
    WAKE = "wake"
    SENT = "sent"  # a message or item from the agent to a person
    SAID = "said"  # a message, reply, press or decision from a person
    EDITED = "edited"  # a message rewritten or deleted after it was sent
    WAIT = "wait"  # a wait on a person, from the ask to its settling (a range)
    CALL = "call"  # an HTTP call answered
    CALL_FAILED = "call_failed"  # an HTTP call refused, failed, or answered with an error status
    CHANGE = "change"  # a change in a provider no call of the agent's made: the scenario's, a person's
    READ = "read"
    DUE = "due"  # an entry of the table, at the moment it was due
    DUE_FAULT = "due_fault"  # an entry a dispatch rule held back, dropped or repeated
    MODEL_CALL = "model_call"
    MEMORY_WRITE = "memory_write"
    MEMORY_READ = "memory_read"
    STORED = "stored"
    SPAN = "span"


class Lane(Model):
    key: str = Field(description="Unique on the page: `person:<key>`, `provider:<key>`, `host:<host>`, or the kind")
    kind: LaneKind
    label: str
    marks: int = Field(ge=0)


class Marks(Model):
    """Every mark, one per index across the lists, sorted by `sim`. A selection names a mark by `ref`: `ev:<seq>`,
    `call:<index>`, `due:<index>`, `mc:<span_id>`, `pc:<index>`, `wake:<n>`, `span:<span_id>`, `wait:<key>`."""

    sim: list[int] = Field(description="Simulated time, milliseconds since the epoch")
    sim_end: list[int | None] = Field(description="A range's end on the simulated clock; None for an instant")
    real: list[int] = Field(
        description="Real time, milliseconds since the epoch: as recorded, else the real moment of the latest "
        "event recorded at or before it on the simulated clock"
    )
    real_end: list[int | None]
    lane: list[int] = Field(description="Index into `lanes`")
    kind: list[MarkKind]
    ref: list[str]
    label: list[str] = Field(description="A few words; the inspector reads the rest by `ref`")


class FindingMark(Model):
    number: int = Field(ge=1, description="ExplainedFinding.number")
    kind: FindingKind
    sim: int = Field(description="Where it happened: the finding's moment, else its first evidence's")
    refs: list[str] = Field(description="The marks of its evidence")


class TimelineResponse(Model):
    starts: int = Field(description="The scenario's start, simulated, ms")
    reached: int = Field(description="The latest simulated moment recorded, ms")
    deadline: int | None
    split: int | None = Field(description="A fork: the simulated moment it split from its parent")
    lanes: list[Lane]
    marks: Marks
    findings: list[FindingMark]


# -- one thing, read whole, for the inspector -------------------------------------------------------------------------


class EventDetail(Model):
    event: WorldEvent
    before: Snapshot | None = Field(description="The entity's version before this change, if it had one")
    call: int | None = Field(description="Index of the HTTP call that made it, in `/calls`")
    thread: list[MessageLine] = Field(description="A message: every message of its conversation, in order")
    written_by: WrittenBy | None = Field(description="The agent's model call that wrote it, if joined")
    reply: int | None = Field(description="A person's message: index of the reply it delivered, in `/people`")
    findings: list[int] = Field(description="Numbers of the findings that cite it")


class CallAnswer(StrEnum):
    """Who answered a call, in one word for a table."""

    PROVIDER = "provider"  # a provider of this process: a fake
    DECLARED = "declared"  # an outbound declaration's answer, never sent
    STORED = "stored"  # a declared `store`, kept and read back
    REAL_HOST = "real_host"
    REPLAYED = "replayed"  # REPLAYED from a recording
    EMULATOR = "emulator"
    MODEL = "model"  # a model standing in for an undeclared service
    TUNNELLED = "tunnelled"  # relayed unopened: a model host
    AS_PERSON = "as_person"  # Minutehand's own call as a person to the agent's product
    REFUSED = "refused"  # nothing answered it, or a declaration or replay refused


class CallRow(Model):
    """One HTTP call without its bodies, for a table of thousands."""

    index: int = Field(ge=0, description="Its place in `/calls`, and its id in `/calls/{index}`")
    wake: int
    at: AwareDatetime = Field(description="Simulated, when it began")
    provider: str | None
    method: str
    host: str
    path: str
    status: int
    answered: CallAnswer
    outcome: CallOutcome | None
    request_bytes: int = Field(ge=0, description="Of the body as recorded")
    response_bytes: int = Field(ge=0)
    events: int = Field(ge=0, description="Events of the world's log it produced")


class CallRowsResponse(Model):
    calls: list[CallRow]


class CallDetail(Model):
    index: int
    call: RecordedCall
    answered: CallAnswer
    events: list[int] = Field(description="Seqs of the events it produced")


class DueRow(Model):
    index: int = Field(ge=0, description="Its id in a `due:<index>` ref")
    entry: DueEntry


class DispatchResponse(Model):
    entries: list[DueRow] = Field(
        description="Every entry the run loop's table held, as it last stood, in the order they entered; empty for "
        "a run that kept no table (a standing world)"
    )


class MemoryChange(Model):
    seq: int
    at: AwareDatetime = Field(description="Simulated")
    wake: int
    actor: Actor = Field(description="AGENT, or SCENARIO for the memory a scenario seeds")
    collection: str
    key: str
    value: str | None = Field(description="Canonical JSON as written; None: deleted")
    before: str | None = Field(description="The value it replaced; None: the key was new or had been deleted")


class MemoryKey(Model):
    collection: str
    key: str
    writes: int = Field(ge=0)
    reads: int = Field(ge=0, description="Gets of exactly this key")
    value: str | None = Field(description="As it stood at the run's head; None: deleted")


class MemoryResponse(Model):
    keys: list[MemoryKey] = Field(description="Every key written or read, by collection and key")
    changes: list[MemoryChange] = Field(description="Every write and delete, in record order")
    reads: int = Field(ge=0, description="Gets and listings, in all")


class StoredChange(Model):
    seq: int
    at: AwareDatetime
    wake: int
    operation: Operation
    host: str
    collection: str
    path: str
    id: str
    item: str | None = Field(description="JSON as stored; None for a delete")
    before: str | None = Field(description="The item it replaced; None when it was new")


class StoredResponse(Model):
    changes: list[StoredChange]


class ReplyLine(Model):
    index: int = Field(ge=0, description="Its id in a `reply:<index>` ref")
    reply: PersonReply
    seq: int | None = Field(description="The world event that delivered it, when one did")
    asked: int | None = Field(description="Seq of the message or item it answers")


class PersonCallLine(Model):
    index: int = Field(ge=0, description="Its id in a `pc:<index>` ref")
    call: PersonCall


class ConversationLine(Model):
    """One message between the agent and a person, chat-style: who wrote it, and how."""

    seq: int | None
    at: AwareDatetime = Field(description="Simulated")
    from_agent: bool
    change: MessageChange
    text: str
    before: str | None = Field(description="An edit: the text it replaced")
    writing: Writing | None = Field(description="A person's: where its words came from; None for the agent's")
    written_by: str | None = Field(description="The model that wrote it, when one did, agent's or person's")
    reply: int | None = Field(description="A person's: index of the reply, in `replies`")


class PersonActivity(Model):
    key: str
    name: str
    email: str
    conversation: list[ConversationLine]
    replies: list[ReplyLine]
    model_calls: list[PersonCallLine]


class PeopleResponse(Model):
    people: list[PersonActivity] = Field(description="In the scenario's order")


class RuleStatus(StrEnum):
    PASSED = "passed"  # read at least once, and nothing found
    FAILED = "failed"
    REVIEW = "review"  # found something a person must look at, and nothing failed
    UNREAD = "unread"  # never read: every moment it names was missing
    NOT_APPLIED = "not_applied"  # read for nothing: no thing it is for met its `when`, and none was left unread
    NOT_CHECKED = "not_checked"  # the run is not finished, or was checked before rules were tallied


class RuleOutcome(Model):
    rule: Rule
    status: RuleStatus
    read: int = Field(ge=0)
    unread: int = Field(ge=0)
    findings: list[int] = Field(description="Numbers of its findings, in `/findings`")


class AssessmentsResponse(Model):
    rules: list[RuleOutcome] = Field(description="Each of the team's rules the run is judged by, in order")


class SpanResponse(Model):
    span: StoredSpan


class SampleRow(Model):
    seed: int
    verdict: VerdictKind | None
    words: str
    run_id: str | None = Field(description="The run, when it was performed and is in this state directory")


class ScenarioSamples(Model):
    scenario: str
    file: str
    expected: str = Field(description="What it is written to reach, as its file says it")
    matched: bool
    passed: int = Field(ge=0, description="Samples that reached what it expects")
    samples: list[SampleRow]
    failing_seeds: list[int]


class BatchRow(Model):
    batch_id: str
    folder: str
    samples: int
    scenarios: list[ScenarioSamples]


class BatchesResponse(Model):
    batches: list[BatchRow] = Field(description="Every `run-all` batch kept under the state directory, oldest first")
