"""The simulated clock. It moves forward only, and only to a moment something is due."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from minutehand.domain.scenario import Model


class DueKind(StrEnum):
    AGENT_WAKE = "agent_wake"
    PERSON_REPLY = "person_reply"
    DIRECTION = "direction"
    TICKET_FATE = "ticket_fate"
    HAPPENING = "happening"


class Due(Model):
    at: AwareDatetime
    kind: DueKind
    ref: str


class DueSource(StrEnum):
    """Who put an entry into the run loop's table of what is due next."""

    REPORTED = "reported"  # the agent's `AgentReport.next_wake`
    BOOKED = "booked"  # the agent's booking with a scheduler provider
    POLLED = "polled"  # the declared rhythm of a `Polled` agent
    REPLY = "reply"  # a person's reply, decided and on its way
    FATE = "fate"  # what becomes of a ticket assigned to a person
    HAPPENING = "happening"  # something the scenario has a person do by themselves
    DIRECTION = "direction"  # something the scenario's owner says to the agent


AGENT_SOURCES = frozenset({DueSource.REPORTED, DueSource.BOOKED, DueSource.POLLED})
"""The entries that are the agent's own plan to come back to work, whoever else may wake it first."""


class DueClosed(StrEnum):
    """How an entry left the table."""

    FIRED = "fired"  # the clock reached it and the run loop dispatched it
    REPLACED = "replaced"  # its source named another moment in its place: a new next wake, a booking changed
    CANCELLED = "cancelled"  # taken out undispatched: a booking deleted, a reply withdrawn, a fork that drops it


class DueEntry(Model):
    """One entry of the run loop's table, as the log records it: when it entered, and when and how it left.

    Written by the run loop as actor SCENARIO under `EntityKind.DUE`, a version when it enters and one when it
    leaves, so a fork reads the table as it stood at its checkpoint, and no check counts it as the agent's work."""

    due: Due
    source: DueSource
    entered_at: AwareDatetime = Field(description="Simulated time it entered the table")
    entered_wake: int = Field(ge=0, description="The wake in progress when it entered; 0 is setup")
    closed: DueClosed | None = Field(default=None, description="None while it is still in the table")
    closed_at: AwareDatetime | None = None
    closed_wake: int | None = Field(default=None, ge=0)

    def open_at(self, moment: datetime) -> bool:
        """Whether it was in the table at `moment`: entered by then, and not yet gone. One that fired at `moment`
        was still there."""
        if self.entered_at > moment:
            return False
        if self.closed_at is None:
            return True
        return self.closed_at > moment or (self.closed is DueClosed.FIRED and self.closed_at == moment)


class Jump(Model):
    was: AwareDatetime
    now: AwareDatetime
    firing: list[Due]


def next_jump(now: datetime, pending: list[Due]) -> Jump | None:
    """Move to the earliest pending moment and fire everything due by then.

    Anything already overdue fires without the clock moving. None means nothing
    is pending: the run is over or the agent is stuck.
    """
    if not pending:
        return None
    target = max(now, min(d.at for d in pending))
    firing = sorted((d for d in pending if d.at <= target), key=lambda d: (d.at, d.ref))
    return Jump(was=now, now=target, firing=firing)
