"""What a check answers with. The shape follows the parent repository's lint core:
severity is how loud, kind is what the reader must do, and a check that could not
read its input did not run, which is `blocked` and never a finding."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, Field

from minutehand.domain.agent import Commitment
from minutehand.domain.assessments import Rule, StoppedBy
from minutehand.domain.clock import DueEntry
from minutehand.domain.conversation import Judgement
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Model, ProviderKey, Scenario
from minutehand.domain.world import EntityRef, Exchange, WorldEvent


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


class CheckReport(Model):
    findings: list[Finding] = []
    blocked: list[str] = []
    notes: list[str] = []


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
        description="The longest stretch from a wait settling to the agent's next write on it, or to the run's end "
        "when there was none",
    )
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
    reason: str | None = Field(default=None, description="Why the step began, as whoever marked it said")
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
        default=[], description="What people said: every reply not withdrawn before it landed, with who and when"
    )
    commitments: list[Commitment] | None = None
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
