"""What a check answers with. The shape follows alknoma-cloud's `lints/_core.py`:
severity is how loud, kind is what the reader must do, and a check that could not
read its input did not run, which is `blocked` and never a finding."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from datetime import timedelta

from pydantic import AwareDatetime, Field

from minutehand.domain.agent import Commitment
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.world import EntityRef, WorldEvent


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


class Finding(Model):
    check: str
    severity: Severity
    kind: FindingKind
    message: str
    at: AwareDatetime | None = Field(default=None, description="Simulated time")
    wake: int | None = None
    evidence: list[int] = Field(default=[], description="WorldEvent.seq values")
    pattern: str | None = Field(default=None, description="Pattern.key: how a proactive agent avoids this")


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
    WORK_WITH_PERSON = "work_with_person"      # the agent handed someone a ticket
    DATE = "date"                              # a date the scenario fixed


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
    due_at: AwareDatetime | None = Field(default=None, description="When the world will settle it; None is never")
    settled_at: AwareDatetime | None = None
    agent_touches: list[int] = Field(default=[], description="Agent events on the same person or entity while open")
    first_touch_after_settled: int | None = None


class Stability(Model):
    """The same scenario run several times. A rate below 1 is the agent's own variance, reported as it is."""

    samples: int = Field(ge=1)
    passed: int = Field(ge=0)

    @property
    def rate(self) -> float:
        return self.passed / self.samples


class Effectiveness(Model):
    """How well the agent carried the work, measured from the world and the clock.

    Time the world itself took (a person's three days, a silent reviewer) is not
    counted against the agent. `time_lost` is only what the agent added.
    """

    expectations_met: int = Field(ge=0)
    expectations_total: int = Field(ge=0)
    waits_opened: int = Field(ge=0)
    waits_open_at_end: int = Field(ge=0)
    follow_ups_due: int = Field(ge=0, description="Waits that passed their expected date while still open")
    follow_ups_made: int = Field(ge=0)
    follow_ups_late: int = Field(ge=0)
    time_lost: timedelta = Field(description="Late follow-ups plus slow reactions to answers")
    slowest_follow_up: timedelta | None = None
    wakes: int = Field(ge=0)
    idle_wakes: int = Field(ge=0, description="Wakes that changed nothing")
    failed_checks: int = Field(ge=0)


class WakeRecord(Model):
    index: int = Field(ge=1)
    sim_time: AwareDatetime
    world_changes: int = Field(ge=0)
    commitments_changed: bool


class RunView(Model):
    """Everything a check may read. A check never reaches past this."""

    scenario: Scenario
    events: list[WorldEvent]
    wakes: list[WakeRecord]
    obligations: list[Obligation] = []
    commitments: list[Commitment] | None = None


class Check(Protocol):
    id: str
    needs: frozenset[Needs]

    def run(self, view: RunView) -> CheckReport: ...
