"""A fork: one run restarted from a moment in another, with something changed.

Nothing here touches the agent's code. The world and the people are Minutehand's
to change; the agent's prompt and model are changed on the wire, in the request
the agent sends to its model provider.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field

from minutehand.domain.scenario import DispatchRule, Model, ReplyBehaviour, TicketState
from minutehand.domain.world import EntityRef


class CallMatch(Model):
    """Which of the agent's model calls a change applies to. Every set field must match."""

    host: str | None = None
    model: str | None = None
    system_contains: str | None = Field(default=None, description="Text that identifies one prompt among several")


class PromptPatch(Model):
    kind: Literal["prompt_patch"] = "prompt_patch"
    where: CallMatch = CallMatch()
    find: str | None = Field(default=None, description="Text to replace; None appends")
    text: str


class ModelSwap(Model):
    kind: Literal["model_swap"] = "model_swap"
    where: CallMatch = CallMatch()
    to: str


class PersonChange(Model):
    """From the fork onward this person behaves differently."""

    kind: Literal["person_change"] = "person_change"
    person: str = Field(description="Person.key")
    reply: ReplyBehaviour


class TicketEdit(Model):
    """The world is different at the fork: a ticket is in another state or with another person."""

    kind: Literal["ticket_edit"] = "ticket_edit"
    entity: EntityRef
    state: TicketState | None = None
    assignee: str | None = Field(default=None, description="Person.key")


class DeadlineShift(Model):
    kind: Literal["deadline_shift"] = "deadline_shift"
    by: timedelta


class DispatchChange(Model):
    """From the fork onward the agent's own wakes are delivered by these rules in place of the scenario's
    (`Scenario.dispatch`): the same run, with the scheduler late, doubling or dropping. A rule for the nth wake of
    a kind counts the wakes that fell due before the fork, so one the parent already reached never applies."""

    kind: Literal["dispatch_change"] = "dispatch_change"
    rules: list[DispatchRule] = Field(description="Every rule from the fork on; empty: every wake delivered as asked")


Override = Annotated[
    PromptPatch | ModelSwap | PersonChange | TicketEdit | DeadlineShift | DispatchChange, Field(discriminator="kind")
]


class Fork(Model):
    parent_run: str
    at_seq: int = Field(ge=0, description="The last WorldEvent.seq the fork shares with its parent")
    overrides: list[Override] = []
    samples: int = Field(default=1, ge=1)
