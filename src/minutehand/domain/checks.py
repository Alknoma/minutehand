"""What a check answers with. The shape follows the parent repository's lint core:
severity is how loud, kind is what the reader must do, and a check that could not
read its input did not run, which is `blocked` and never a finding."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, Field

from minutehand.domain.agent import Commitment
from minutehand.domain.assessments import IntegrityCheck, Rule, StoppedBy
from minutehand.domain.clock import DueEntry
from minutehand.domain.conversation import Judgement, PersonCall
from minutehand.domain.items import Assessed, ProvidedTypes, TypedItem
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Model, ProviderKey, Scenario
from minutehand.domain.world import EntityRef, Exchange, RecordedCall, WorldEvent


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"


class FindingKind(StrEnum):
    FAIL = "fail"
    REVIEW = "review"
    INFORMATIONAL = "informational"


class Needs(StrEnum):
    WORLD = "world"
    WAKES = "wakes"
    OBLIGATIONS = "obligations"
    COMMITMENTS = "commitments"
    CALLS = "calls"


class Finding(Model):
    check: str
    severity: Severity
    kind: FindingKind
    message: str
    at: AwareDatetime | None = Field(default=None, description="Simulated time")
    wake: int | None = None
    evidence: list[int] = Field(default=[], description="WorldEvent.seq values")
    pattern: str | None = Field(default=None, description="Pattern.key: how a proactive agent avoids this")
    judged: Judgement | None = Field(default=None, description="Set when a model judged this: which, how, and why")
    calls: list[int] = Field(
        default=[], description="The agent's calls it cites, by their place among the run's calls (calls.call_id)"
    )
    assessed: Assessed | None = Field(
        default=None,
        description="Set by the assessment of the agent's effects: a violation, a wrong action or wrong timing, and "
        "the declaration it was measured against",
    )


class HealthKind(StrEnum):
    """What kept the simulated world from playing as its files declare it: a fact about the world, never about the
    agent (`checks.health`)."""

    RESPONDER_NEVER_ACTS = "responder_never_acts"  # a declared service responder whose script ends in silence
    WAITS_ON_NOBODY = "waits_on_nobody"  # an item only a person can move, held pending on nobody
    OWED_UNBOOKED = "owed_unbooked"  # a reply or decision someone owes, with no moment booked for it
    MODEL_FAILED = "model_failed"  # a people model call that failed and was never answered after
    SERVICE_FAILED = "service_failed"  # a declared service answered the agent with Minutehand's own failure (502)
    PUSH_FAILED = "push_failed"  # an event the world pushed that never reached the agent, retries and all
    BEYOND_FACTS = "beyond_facts"  # a person's model-written reply said what nothing they know supports
    WAITS_BY_DECLARATION = "waits_by_declaration"  # an item pending on someone the files declare never acts
    NEVER_EXERCISED = "never_exercised"  # a declared service, collection or person nothing in the run touched
    STEP_NEVER_FIRED = "step_never_fired"  # a scripted step whose ask never came


INCOMPLETE = frozenset(
    {
        HealthKind.RESPONDER_NEVER_ACTS,
        HealthKind.WAITS_ON_NOBODY,
        HealthKind.OWED_UNBOOKED,
        HealthKind.MODEL_FAILED,
        HealthKind.SERVICE_FAILED,
        HealthKind.PUSH_FAILED,
        HealthKind.BEYOND_FACTS,
    }
)
"""The kinds that mean the world did not play what its files declare, so the run is `SIMULATION_INCOMPLETE`. The
others state how much of what was declared the run reached, which no verdict reads."""


class HealthFinding(Model):
    """One fact about the simulated world's health: what did not play as declared, with its evidence."""

    kind: HealthKind
    incomplete: bool = Field(description="Whether it makes the run SIMULATION_INCOMPLETE (`INCOMPLETE`)")
    words: str = Field(description="What happened, in one sentence")
    person: str | None = Field(default=None, description="Person.key it is about")
    entity: EntityRef | None = Field(default=None, description="The item, message, service or push it is about")
    since: AwareDatetime | None = Field(default=None, description="Simulated time it began")
    evidence: list[int] = Field(default=[], description="WorldEvent.seq values")


class DeclaredCollection(Model):
    """A collection an outbound `store` host keeps (`domain.outbound.DeclaredStore`), as the health check names it."""

    host: str
    collection: str


class Pattern(Model):
    """One piece of know-how: a way proactive agents fail, and the design that stops it.

    A finding names its pattern, so whoever reads the finding, a person or a coding
    agent, is handed the fix along with the failure.
    """

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    failure: str = Field(description="What the agent does wrong, in one sentence")
    design: str = Field(description="What a proactive agent does instead")
    reference: str | None = Field(default=None, description="Where a working implementation can be read")


class RuleRead(Model):
    """How often one of the team's rules was read over a run, and how often it could not be (`checks.assessments`):
    with its findings, whether it held for everything it was read for."""

    rule: str = Field(description="Rule.id")
    read: int = Field(
        ge=0,
        description="Times it applied and was read: once for each thing it is for where its `when` held, at each of "
        "its moments",
    )
    unread: int = Field(
        ge=0, description="Times it could not be read: a moment the run never reached, or one the thing lacked"
    )


class CheckReport(Model):
    findings: list[Finding] = []
    blocked: list[str] = []
    notes: list[str] = []
    rules_read: list[RuleRead] = Field(default=[], description="The team's rules, each with how often it was read")


class ObligationKind(StrEnum):
    ANSWER_FROM_PERSON = "answer_from_person"  # the agent asked someone something
    WORK_WITH_PERSON = "work_with_person"  # the agent handed someone a ticket
    DATE = "date"  # a date the scenario fixed


class Obligation(Model):
    """Something outstanding in the world, as the monitor knows it.

    The monitor plays the people and owns the clock, so it knows what was asked,
    when the answer landed, and what the agent did in between. None of this is
    reported by the agent.
    """

    key: str
    kind: ObligationKind
    person: str | None = Field(default=None, description="Person.key")
    entity: EntityRef | None = None
    opened_at: AwareDatetime
    opened_by: int = Field(description="WorldEvent.seq of the ask or hand-off")
    expected_by: AwareDatetime | None = Field(
        default=None, description="After this, silence is the agent's to act on; None means no date applies"
    )
    patience: timedelta | None = Field(
        default=None,
        description="How long the person may take over each message on this wait: a follow-up gives them this long "
        "again from the moment it was sent. None: a follow-up does not move the date (work has its own pace)",
    )
    settled_at: AwareDatetime | None = Field(default=None, description="When the answer landed or the work was done")
    agent_touches: list[int] = Field(default=[], description="Agent events on the same person or entity while open")
    first_touch_after_settled: int | None = None


class Stability(Model):
    """The same scenario run several times. A rate below 1 is the agent's own variance, reported as it is."""

    samples: int = Field(ge=1)
    passed: int = Field(ge=0)

    @property
    def rate(self) -> float:
        return self.passed / self.samples


class PersonBurden(Model):
    """What the agent asked of one person."""

    person: str = Field(description="Person.key")
    messages: int = Field(ge=0, description="Messages the agent sent them")
    follow_ups: int = Field(ge=0, description="Of those, sent while an earlier ask of theirs was still open")


class Effectiveness(Model):
    """What the agent did and what the world did, counted from the world and the clock.

    Every number is a count or a stretch of time; none is measured against what the agent should have done, which
    only the team's own rules say (`domain/assessments.py`).
    """

    expectations_met: int = Field(ge=0)
    expectations_total: int = Field(ge=0)
    waits_opened: int = Field(
        ge=0,
        description="Asks and hand-offs the agent is owed an answer or work on; the scenario's deadline is not one",
    )
    waits_open_at_end: int = Field(ge=0, description="Of those, the ones the world had not settled when the run ended")
    follow_ups_made: int = Field(
        ge=0, description="Agent writes the person could see on a wait while it was still open"
    )
    waits_settled: int = Field(
        default=0, ge=0, description="Waits the world settled that name a person or entity, so a reaction can be timed"
    )
    slowest_reaction: timedelta | None = Field(
        default=None,
        description="The longest stretch from a wait settling to the agent's next write anywhere a person could see "
        "it (a message to anyone, a ticket, an item on another service; not its own memory), or to the run's end "
        "when there was none",
    )
    changes_by_others: int = Field(
        default=0, ge=0, description="Changes someone else made in the world: replies, decisions, ask-backs"
    )
    changes_never_seen: int = Field(default=0, ge=0, description="Of them, those the agent could never have known")
    slowest_unseen: timedelta | None = Field(
        default=None, description="The longest any change sat before the agent could know it (`domain.reactions`)"
    )
    slowest_unseen_change: str | None = Field(default=None, description="Which change that was")
    slowest_to_act: timedelta | None = Field(
        default=None, description="The longest from the agent seeing a change to its next move in the world"
    )
    slowest_to_act_change: str | None = Field(default=None, description="Which change that was")
    messages_to_people: int = Field(default=0, ge=0)
    messages_edited: int = Field(
        default=0,
        ge=0,
        description="Agent messages to people rewritten in place after they were sent, each rewrite once",
    )
    messages_deleted: int = Field(default=0, ge=0, description="Agent messages to people deleted after they were sent")
    decisions_asked: int = Field(
        default=0, ge=0, description="Items the agent left waiting on a person in its own product (`domain.inboxes`)"
    )
    decisions_made: int = Field(default=0, ge=0, description="Of those, the ones the person decided")
    decisions_pending: int = Field(default=0, ge=0, description="Of those, the ones still waiting when the run ended")
    burden: list[PersonBurden] = Field(default=[], description="Messages per person, in Scenario.people order")
    messages_per_outcome: float | None = Field(
        default=None, description="messages_to_people per expectation met; None when none was met"
    )
    wakes: int = Field(ge=0)
    idle_wakes: int = Field(ge=0, description="Wakes that changed nothing")
    failed_checks: int = Field(ge=0)


class CommitmentsReported(Model):
    """What the agent said it was committed to as one wake ended (`AgentReport.commitments`)."""

    wake: int = Field(ge=0)
    at: AwareDatetime = Field(description="Simulated time the wake ended")
    commitments: list[Commitment]


class WakeRecord(Model):
    """One wake of the agent; in a standing world, one step whoever drives the agent marked (or the server inferred
    from the clock): the stretch the checks read as one go of the agent's."""

    index: int = Field(ge=1)
    sim_time: AwareDatetime
    world_changes: int = Field(ge=0)
    commitments_changed: bool
    inferred: bool = Field(
        default=False,
        description="A standing world's step nobody marked: the stretch between two forward moves of its clock",
    )
    reason: str | None = Field(
        default=None,
        description="Why it began: in a run, the `WakeReason` the wake carried; in a standing world, as whoever marked "
        "the step said",
    )
    memory_reads: int = Field(
        default=0, ge=0, description="Gets and listings of the agent's memory (`minutehand.agent.store`) in the wake"
    )
    memory_writes: int = Field(default=0, ge=0, description="Keys of the agent's memory written or deleted in the wake")


class WakeModelCalls(Model):
    """How many of the agent's model calls the run received telemetry of, for one wake."""

    wake: int = Field(ge=0)
    calls: int = Field(ge=0, description="Spans of a model call placed in this wake")


class RunView(Model):
    """Everything a check may read. A check never reaches past this."""

    scenario: Scenario
    events: list[WorldEvent]
    wakes: list[WakeRecord]
    obligations: list[Obligation] = []
    replies: list[PersonReply] = Field(
        default=[], description="What people said: every reply that landed, with who and when"
    )
    commitments: list[Commitment] | None = None
    agent_instructions: list[str] = Field(
        default=[],
        description="Every distinct system prompt the agent gave its model, in the order it first gave each: its own "
        "statement of its work (`application.model_calls.agent_instructions`); empty when no call of its was seen",
    )
    model_calls: list[WakeModelCalls] | None = Field(
        default=None,
        description="Model calls per wake, from the agent's telemetry; None when the run received no span of a "
        "model call, so nobody can say how many it made",
    )
    unmatched_calls: list[Exchange] | None = Field(
        default=None,
        description="Calls to hosts no provider claims, which produced no event; None when nobody could say",
    )
    contract_breaks: list[Exchange] = Field(
        default=[],
        description="Calls Minutehand made as a person to the agent's own product whose answer departed from the "
        "agent's own API description (`InboxCall.contract`): the agent's contract changed",
    )
    reported: list[CommitmentsReported] | None = Field(
        default=None,
        description="The agent's commitments as each wake ended, in order; None when the agent reported none, so "
        "nothing it said can be held against the world",
    )
    dues: list[DueEntry] | None = Field(
        default=None,
        description="Every entry of the run loop's table of what is due next, as each last stood: what the agent "
        "planned and when, and what else was due; None when the run kept no table (a captured run, a standing world)",
    )
    around_proxy: list[AroundProxy] | None = Field(
        default=None,
        description="Hosts a provider claims that the agent's telemetry says it called more often than the proxy saw; "
        "None when the agent exported no span of an HTTP client call, so nobody can say",
    )
    uncalled_providers: list[ProviderKey] = Field(
        default=[],
        description="The providers the scenario and the agent file name, when the agent was woken and the proxy saw no "
        "call to any of them; empty when it saw one",
    )
    broken_calls: list[Exchange] = Field(
        default=[],
        description="Calls Minutehand failed to answer (`CallOutcome.INTERNAL_ERROR`): the run says nothing about the "
        "agent while any is here",
    )
    rules: list[Rule] = Field(
        default=[],
        description="The team's own rules the run is judged by: the agent file's `assess`, with the scenario's "
        "(`domain.assessments.merged`)",
    )
    stopped: StoppedBy | None = Field(default=None, description="How the run stopped; None while it runs, or unknown")
    calls: list[RecordedCall] | None = Field(
        default=None,
        description="Every call the proxy saw, in order, numbered from 1 by their place; None when nobody recorded them",
    )
    typed: list[TypedItem] = Field(
        default=[],
        description="Each event that changed an item of a kind the providers declare (`domain.items`), read as that item",
    )
    item_types: list[ProvidedTypes] = Field(
        default=[], description="The item types each provider in the run declares, with the built-in ones"
    )
    rhythm: timedelta | None = Field(
        default=None,
        description="The agent's declared rhythm: the agent file's `tick`, else its shortest polled `every`; None when "
        "it declares none",
    )
    fail_on_integrity: list[IntegrityCheck] = Field(
        default=[],
        description="The integrity facts the agent file or the scenario says fail the run; any other is stated as "
        "`review` and never changes the verdict",
    )
    person_calls: list[PersonCall] = Field(
        default=[], description="Every call Minutehand made to a model for people and services, in order"
    )
    collections: list[DeclaredCollection] = Field(
        default=[], description="Every collection the agent file's `store` hosts declare"
    )

    def integrity(self, check: IntegrityCheck) -> tuple[FindingKind, Severity]:
        """How a finding of an integrity fact is stated: a failure when the user's files say it fails the run,
        else something for someone to look at."""
        if check in self.fail_on_integrity:
            return FindingKind.FAIL, Severity.ERROR
        return FindingKind.REVIEW, Severity.WARNING


class AroundProxy(Model):
    """Calls to a host a provider claims that the agent's own telemetry says it made and the proxy never saw: its HTTP
    client went around Minutehand, to the real host (`application.around_proxy`)."""

    host: str
    provider: ProviderKey
    by_agent: int = Field(ge=1, description="Spans of HTTP client calls to the host the agent exported")
    through_proxy: int = Field(ge=0, description="Calls to the host the proxy recorded")
    around: int = Field(ge=1, description="Calls the agent made that the proxy did not see")
    example: str = Field(description="One such call as its span names it: the span's name and its URL")


class Check(Protocol):
    id: str
    needs: frozenset[Needs]

    def run(self, view: RunView) -> CheckReport: ...
