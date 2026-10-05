"""The contract between the monitor and the agent it is testing.

The agent answers three questions. Everything else the monitor learns from the
traffic it intercepts.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from minutehand.domain.outbound import OutboundHost, refuse_repeats
from minutehand.domain.people import InboundTarget
from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import EntityRef


class WakeReason(StrEnum):
    START = "start"
    DUE = "due"
    PERSON_REPLIED = "person_replied"
    DIRECTION = "direction"
    TICK = "tick"


class AgentStatus(StrEnum):
    WORKING = "working"
    IDLE = "idle"
    DONE = "done"


class WaitingOn(StrEnum):
    PERSON = "person"
    SYSTEM = "system"
    DATE = "date"
    RESULT = "result"


class CommitmentStatus(StrEnum):
    OPEN = "open"
    MET = "met"
    DROPPED = "dropped"


class Commitment(Model):
    """Something the agent says it is waiting for. Optional; unlocks the time checks."""

    key: str
    description: str
    waiting_on: WaitingOn
    person_email: str | None = None
    entity: EntityRef | None = None
    opened_at: AwareDatetime
    expected_by: AwareDatetime | None = None
    status: CommitmentStatus = CommitmentStatus.OPEN


class WakeRequest(Model):
    """ "It is now `now`; go." Sent each time the clock reaches a due moment."""

    run_id: str
    now: AwareDatetime
    reason: WakeReason
    goal: str | None = Field(default=None, description="Set on the START wake only")
    direction: str | None = Field(default=None, description="What the owner said; set on a DIRECTION wake only")


class AgentReport(Model):
    """The answer to "are you still working?" and "when do you next need to wake?"."""

    status: AgentStatus
    next_wake: AwareDatetime | None = None
    commitments: list[Commitment] | None = None


class Reported(Model):
    """The agent says when it next needs to wake. Exact, and needs an endpoint or adapter.

    A wake is over when `report_url` answers anything but WORKING. The defaults suit an agent whose turn takes
    minutes: the report is asked for quickly at first and then less often, the wait doubling from
    `report_first_after` up to `report_at_most_every`, and a wake still WORKING after `working_limit` stops the
    run as AGENT_FAILED.
    """

    kind: Literal["reported"] = "reported"
    wake_url: str
    report_url: str
    wake_timeout: timedelta = Field(
        default=timedelta(minutes=2),
        gt=timedelta(0),
        description="How long one call to wake_url or report_url may take",
    )
    report_first_after: timedelta = Field(
        default=timedelta(milliseconds=100), gt=timedelta(0), description="The wait before the first ask for the report"
    )
    report_at_most_every: timedelta = Field(
        default=timedelta(seconds=10), gt=timedelta(0), description="The longest wait between two asks for the report"
    )
    working_limit: timedelta = Field(
        default=timedelta(minutes=30), gt=timedelta(0), description="How long one wake may stay WORKING"
    )

    @model_validator(mode="after")
    def _backs_off(self) -> Reported:
        if self.report_at_most_every < self.report_first_after:
            raise ValueError("report_at_most_every is shorter than report_first_after")
        return self


class Booked(Model):
    """The agent books its own wake-ups with a scheduler the proxy intercepts.

    Nothing is asked of the agent: the booking is an outbound call like any other,
    and the scheduler provider calls the agent back when the clock reaches it.
    """

    kind: Literal["booked"] = "booked"


class Polled(Model):
    """The agent is invoked on a fixed rhythm and decides for itself whether anything is due."""

    kind: Literal["polled"] = "polled"
    wake_url: str
    every: timedelta = timedelta(minutes=5)


class Command(Model):
    """A command run per wake; it reads a WakeRequest on stdin and prints an AgentReport."""

    kind: Literal["command"] = "command"
    argv: list[str]


WakeSource = Annotated[Reported | Booked | Polled | Command, Field(discriminator="kind")]


class ActionArgument(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str


class HumanAction(Model):
    """Something a person does in the agent's OWN product, where no SaaS fake can stand in:
    approving an operation in its web app, answering a question on its own page.

    Declared here, or learned from the agent's API description wherever an operation
    carries `x-minutehand: human_action`. Either way it becomes a tool the simulated
    person can use, beside replying in chat and pressing a button.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(description="When a person would do this; the persona reads it")
    method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str = Field(description="May hold {argument} placeholders")
    body: str | None = Field(default=None, description="JSON text with {argument} placeholders")
    arguments: list[ActionArgument] = []


class Inbox(Model):
    """Where the monitor learns what is waiting on a person in the agent's own product."""

    url: str = Field(description="Lists what is pending; may hold {person_email}")
    id_field: str
    summary_field: str


class StateHooks(Model):
    """How the agent's own state is saved and put back, so a run can be rewound. Without hooks a run cannot be
    forked, and a sample after the first starts from whatever the agent remembers.

    Every command receives MINUTEHAND_SNAPSHOT_DIR, the directory one checkpoint's snapshot lives in.

    A checkpoint is snapshotted only once the agent has settled: it reports it is not working, and no outbound
    call of its has been seen for `quiet`. One that has not settled after `settle_limit` is recorded as not
    restorable, with the reason, and is never snapshotted.

    A restore is a sequence: `stop`, `restore`, `start`, then the agent's report endpoint must answer within
    `answer_limit`, and its report must equal the one recorded at the checkpoint. Any command running longer
    than `step_limit` fails its step.
    """

    snapshot: list[str] = Field(min_length=1)
    restore: list[str] = Field(min_length=1)
    stop: list[str] | None = Field(default=None, min_length=1, description="Stops the agent's processes")
    start: list[str] | None = Field(default=None, min_length=1, description="Starts them again after `restore`")
    quiet: timedelta = Field(
        default=timedelta(seconds=1),
        ge=timedelta(0),
        description="How long no outbound call of the agent's must be seen before a checkpoint is taken",
    )
    settle_limit: timedelta = Field(
        default=timedelta(seconds=60),
        gt=timedelta(0),
        description="How long a checkpoint waits to settle before it is recorded as not restorable",
    )
    answer_limit: timedelta = Field(
        default=timedelta(seconds=120),
        gt=timedelta(0),
        description="How long after `start` the agent's report endpoint has to answer",
    )
    step_limit: timedelta = Field(
        default=timedelta(minutes=5), gt=timedelta(0), description="How long one hook command may run"
    )

    @model_validator(mode="after")
    def _can_settle(self) -> StateHooks:
        if self.settle_limit < self.quiet:
            raise ValueError("settle_limit is shorter than quiet, so no checkpoint could ever settle")
        return self


class GoalByWake(Model):
    """The goal arrives in the START wake's `WakeRequest.goal`, and directions in DIRECTION wakes."""

    kind: Literal["by_wake"] = "by_wake"


class GoalByMessage(Model):
    """The scenario's owner sends the goal as a message through this provider, as a person would, and every
    scripted direction the same way. An agent reached like this may expose no wake endpoint at all."""

    kind: Literal["by_message"] = "by_message"
    provider: ProviderKey


GoalSource = Annotated[GoalByWake | GoalByMessage, Field(discriminator="kind")]


class AgentUnderTest(Model):
    """How the monitor reaches the agent. Replies and pushed events always wake it;
    `wakes` lists every other way it comes back to work, and may be empty when the goal is sent as a message."""

    name: str
    goal: GoalSource = GoalByWake()
    wakes: list[WakeSource] = []
    inbound: list[InboundTarget] = []
    human_actions: list[HumanAction] = []
    inbox: Inbox | None = None
    state: StateHooks | None = None
    outbound: list[OutboundHost] = Field(
        default=[], description="Hosts that are not places the agent keeps state, captured rather than faked"
    )

    @model_validator(mode="after")
    def _goal_reaches_it(self) -> AgentUnderTest:
        refuse_repeats(self.outbound)
        if isinstance(self.goal, GoalByMessage):
            if not any(t.provider == self.goal.provider for t in self.inbound):
                raise ValueError(
                    f"the goal is sent as a message on {self.goal.provider}, "
                    "and the agent declares no inbound target there"
                )
        elif not self.wakes:
            raise ValueError("the goal is handed over in a wake, and the agent declares no way to be woken")
        return self
