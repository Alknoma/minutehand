"""The contract between the monitor and the agent it is testing.

The agent answers three questions. Everything else the monitor learns from the
traffic it intercepts.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from minutehand.domain.people import InboundTarget
from minutehand.domain.scenario import Model
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
    """"It is now `now`; go." Sent each time the clock reaches a due moment."""

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
    """The agent says when it next needs to wake. Exact, and needs an endpoint or adapter."""

    kind: Literal["reported"] = "reported"
    wake_url: str
    report_url: str


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
    """How the agent's own state is saved and put back, so a run can be rewound.

    Each command receives MINUTEHAND_SNAPSHOT_DIR. Without hooks a rerun starts from the beginning.
    """

    snapshot: list[str] = Field(min_length=1)
    restore: list[str] = Field(min_length=1)


class AgentUnderTest(Model):
    """How the monitor reaches the agent. Replies and pushed events always wake it;
    `wakes` lists every other way it comes back to work."""

    name: str
    wakes: list[WakeSource] = Field(min_length=1)
    inbound: list[InboundTarget] = []
    human_actions: list[HumanAction] = []
    inbox: Inbox | None = None
    state: StateHooks | None = None
