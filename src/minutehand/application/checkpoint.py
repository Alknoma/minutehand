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


class PendingReply(Model):
    """A person's reply, decided and remembered, that has not landed yet."""

    kind: Literal["reply"] = "reply"
    due: Due
    reply: int = Field(ge=0, description="Position in the run's `Store.replies()`")


class PendingFate(Model):
    kind: Literal["fate"] = "fate"
    due: Due
    ticket: EntityRef
    becomes: TicketState


class PendingWake(Model):
    """A wake the agent asked for (`DUE`, from `AgentReport.next_wake`) or a `Polled` tick (`TICK`)."""

    kind: Literal["wake"] = "wake"
    due: Due
    reason: WakeReason


class PendingDirection(Model):
    kind: Literal["direction"] = "direction"
    due: Due
    text: str


class PendingBooking(Model):
    """A wake the agent booked with a scheduler provider."""

    kind: Literal["booking"] = "booking"
    due: Due
    provider: ProviderKey
    ref: str = Field(description="The scheduler's own reference for the booking")


Pending = Annotated[
    PendingReply | PendingFate | PendingWake | PendingDirection | PendingBooking, Field(discriminator="kind")
]


class Restorable(Model):
    """The agent settled and its state was snapshotted: a fork from here can put it back."""

    kind: Literal["restorable"] = "restorable"
    snapshot_of: str = Field(description="The run whose directory holds the snapshot (a fork inherits its parent's)")
    wake: int = Field(ge=0, description="The wake whose snapshot directory holds it")
    report: AgentReport | None = Field(
        description="What the agent reported once settled, which a restore must bring back; None when the agent "
        "had not reported and cannot be asked"
    )


class NotRestorable(Model):
    """The agent did not settle in time, so no snapshot was taken: a fork from here is refused, saying why."""

    kind: Literal["not_restorable"] = "not_restorable"
    reason: str


class NoHooks(Model):
    """The agent declares no `StateHooks`: nothing of its own state was kept."""

    kind: Literal["no_hooks"] = "no_hooks"


AgentState = Annotated[Restorable | NotRestorable | NoHooks, Field(discriminator="kind")]


class Checkpoint(Model):
    wake: int = Field(ge=0, description="The wake that had just ended; 0 is setup")
    now: AwareDatetime
    replies: int = Field(ge=0, description="How many replies had been decided: the length of `Store.replies()`")
    fated: list[EntityRef] = Field(default=[], description="Tickets whose fate is already scheduled or landed")
    commitments: list[Commitment] | None = None
    pending: list[Pending]
    agent: AgentState = Field(description="Whether the agent's own state at this moment can be put back")


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
