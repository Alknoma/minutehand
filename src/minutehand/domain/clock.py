"""The simulated clock. It moves forward only, and only to a moment something is due."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime

from minutehand.domain.scenario import Model


class DueKind(StrEnum):
    AGENT_WAKE = "agent_wake"
    PERSON_REPLY = "person_reply"
    DIRECTION = "direction"
    TICKET_FATE = "ticket_fate"
    DOCUMENT_CHANGE = "document_change"


class Due(Model):
    at: AwareDatetime
    kind: DueKind
    ref: str


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
