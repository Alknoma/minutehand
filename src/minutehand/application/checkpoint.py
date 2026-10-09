"""Everything the run loop holds between wakes, written into the world's own log at the end of every wake.

The clock and the pending set are rows in the same log as the world (docs/design.md, "What a rewind needs
beyond the world"), so a fork that shares the log up to a checkpoint reads them back with `Store.get` and
needs no side file.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from minutehand.domain.agent import AgentReport, Commitment, WakeReason
from minutehand.domain.clock import Due
from minutehand.domain.scenario import Model, ProviderKey, TicketState
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation
from minutehand.ports.store import Store

CHECKPOINT = EntityRef(provider="minutehand", kind=EntityKind.RECORD, external_id="checkpoint")
"""The one entity the run loop writes. Actor SCENARIO, so no check counts it as the agent's work."""


class PendingFate(Model):
    kind: Literal["fate"] = "fate"
    due: Due
    ticket: EntityRef
    becomes: TicketState | None = Field(description="None: its assignee deletes it")


class PendingHappening(Model):
    """Something the scenario has a person do by themselves (`Scenario.happenings`), of any family, not yet done."""

    kind: Literal["happening"] = "happening"
    due: Due
    happening: int = Field(ge=0, description="Position in the scenario's `happenings`")


class PendingTransition(Model):
    """A person's move on an item pending on them, at the moment it was booked (`application.people`)."""

    kind: Literal["transition"] = "transition"
    due: Due
    pending: EntityRef = Field(description="The people engine's record of the item (`EntityKind.PENDING`)")


class PendingWake(Model):
    """A wake the agent asked for (`DUE`, from `AgentReport.next_wake`) or a `Polled` tick (`TICK`)."""

    kind: Literal["wake"] = "wake"
    due: Due
    reason: WakeReason
    repeat: bool = Field(
        default=False,
        description="A late or second delivery the scenario's dispatch rules made of a wake already due: a tick of "
        "it books no next tick, which the wake it repeats already did",
    )


class PendingTimer(Model):
    """The earliest deadline of the agent's own timers, read from its sandbox (`Contained`)."""

    kind: Literal["timer"] = "timer"
    due: Due


class PendingMachine(Model):
    """A command the scenario runs on the agent's machine (`Scenario.machine`), not yet run."""

    kind: Literal["machine"] = "machine"
    due: Due
    command: int = Field(ge=0, description="Position in the scenario's `machine`")


class PendingDirection(Model):
    kind: Literal["direction"] = "direction"
    due: Due
    text: str


class PendingService(Model):
    """A declared service's own move on one of its items (`application.services`): a timer's, or a system actor's,
    at its moment. It fires only if the item is still in the state it was booked from."""

    kind: Literal["service"] = "service"
    due: Due
    service: ProviderKey
    item: str
    entered: int = Field(description="The WorldEvent.seq of the item's version that entered the state")
    transition: str = Field(description="The machine's timer or system transition, by name")


class PendingBooking(Model):
    """A wake the agent booked with a scheduler provider."""

    kind: Literal["booking"] = "booking"
    due: Due
    provider: ProviderKey
    ref: str = Field(description="The scheduler's own reference for the booking")
    deliver: bool = Field(default=True, description="Deliver this occurrence when it fires")
    advance: bool = Field(
        default=True,
        description="Then finish the occurrence: False on the first of two deliveries, whose second finishes it",
    )


class PendingCall(Model):
    """A call of the agent's held until the world can answer it (a long poll that found nothing yet), looked at again
    at its moment: the end of its wait (`ends`), or the next moment the world may answer it by itself. It lives with
    the connection it holds, in the process that played the run: a fork drops it, and the fork's agent makes its own
    calls."""

    kind: Literal["call"] = "call"
    due: Due
    ends: bool = Field(description="Its moment is the end of the call's wait, when it is answered as the world stands")


Pending = Annotated[
    PendingFate
    | PendingHappening
    | PendingWake
    | PendingDirection
    | PendingBooking
    | PendingMachine
    | PendingTimer
    | PendingTransition
    | PendingService
    | PendingCall,
    Field(discriminator="kind"),
]


class Remembered(Model):
    """The agent's state at this checkpoint, as far as the run holds it: its memory is the run's log up to here
    (`application.memory`), so a fork from here starts from exactly that memory, and its report, which the agent
    must give again once the fork is made."""

    kind: Literal["remembered"] = "remembered"
    report: AgentReport | None = Field(
        description="What the agent last reported, which it must report again after a fork from here; None when it "
        "has not reported and cannot be asked"
    )
    memory: str = Field(description="The agent's memory here, as one SHA-256 (`application.memory.digest`)")
    outside: list[str] = Field(
        default=[],
        description="State the agent kept outside its memory that the run saw here (a database of its own the agent "
        "file names, not empty): a fork does not put it back",
    )


class NotRestorable(Model):
    """A checkpoint a fork cannot start from, and why: said wherever checkpoints are listed, never written in one."""

    kind: Literal["not_restorable"] = "not_restorable"
    reason: str


AgentState = Annotated[Remembered | NotRestorable, Field(discriminator="kind")]


class Checkpoint(Model):
    wake: int = Field(ge=0, description="The wake that had just ended; 0 is setup")
    now: AwareDatetime
    replies: int = Field(ge=0, description="How many replies had landed: the length of `Store.replies()`")
    fated: list[EntityRef] = Field(default=[], description="Tickets whose fate is already scheduled or landed")
    untaken: list[EntityRef] = Field(
        default=[],
        description="Items pending on people whose move or answer did not land (a model failed), tried again on the "
        "next turn",
    )
    commitments: list[Commitment] | None = None
    pending: list[Pending]
    agent: Remembered = Field(description="The agent's memory and report at this moment")


def write_checkpoint(store: Store, checkpoint: Checkpoint) -> int:
    """Append the checkpoint to the log and answer its seq, which is where a fork may be taken."""
    operation = Operation.CREATE if store.get(CHECKPOINT) is None else Operation.UPDATE
    event = store.apply(
        Change(entity=CHECKPOINT, operation=operation, actor=Actor.SCENARIO, body=checkpoint.model_dump_json())
    )
    return event.seq


def checkpoint_seqs(store: Store) -> list[int]:
    """Every seq a fork can be taken at, in order."""
    return [e.seq for e in store.events() if e.entity == CHECKPOINT]


def checkpoints(store: Store) -> dict[int, Checkpoint]:
    """Every checkpoint this store can see, by the seq it was written at, in order."""
    return {v.seq: Checkpoint.model_validate_json(v.body) for v in store.versions(CHECKPOINT)}


def read_checkpoint(store: Store) -> Checkpoint | None:
    """The latest checkpoint this store can see: for a fork, the one at its `at_seq`."""
    stored = store.get(CHECKPOINT)
    return Checkpoint.model_validate_json(stored.body) if stored is not None else None
