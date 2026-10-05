"""What a check answers with. The shape follows the parent repository's lint core:
severity is how loud, kind is what the reader must do, and a check that could not
read its input did not run, which is `blocked` and never a finding."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, Field

from minutehand.domain.agent import Commitment
from minutehand.domain.conversation import Judgement
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Model, Scenario
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
    """How well the agent carried the work, measured from the world and the clock.

    Time the world itself took (a person's three days, a silent reviewer) is not
    counted against the agent. `time_lost` is only what the agent added.
    """

    expectations_met: int = Field(ge=0)
    expectations_total: int = Field(ge=0)
    waits_opened: int = Field(
        ge=0,
        description="Asks and hand-offs the agent is owed an answer or work on; the scenario's deadline is not one",
    )
    waits_open_at_end: int = Field(ge=0, description="Of those, the ones the world had not settled when the run ended")
    follow_ups_due: int = Field(
        ge=0,
        description="Moments a wait fell due while still open: its expected date, and again its patience after "
        "each follow-up",
    )
    follow_ups_made: int = Field(
        ge=0, description="Agent writes the person could see on a wait still open, whether before or after it fell due"
    )
    follow_ups_late: int = Field(
        ge=0, description="Of the moments due, those followed up more than the grace after, or never"
    )
    follow_ups_early: int = Field(
        default=0,
        ge=0,
        description="Follow-ups sent before the wait they chased had fallen due: each gives the person their whole "
        "delay again, so many of them keep a wait current while asking the same person again and again",
    )
    time_lost: timedelta = Field(description="Late follow-ups plus slow reactions to answers")
    slowest_follow_up: timedelta | None = None
    reactions_due: int = Field(
        default=0, ge=0, description="Settled waits naming a person or entity, so a reaction can be timed"
    )
    reactions_slow: int = Field(
        default=0, ge=0, description="Of those, the agent's next touch came after the grace, or never"
    )
    slowest_reaction: timedelta | None = None
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
    broken_calls: list[Exchange] = Field(
        default=[],
        description="Calls Minutehand failed to answer (`CallOutcome.INTERNAL_ERROR`): the run says nothing about the "
        "agent while any is here",
    )


class Check(Protocol):
    id: str
    needs: frozenset[Needs]

    def run(self, view: RunView) -> CheckReport: ...
