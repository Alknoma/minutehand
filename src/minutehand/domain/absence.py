"""Where a person's absence sits on the run's clock: one rule, read by the ledger, the scripted replier's checks and
every provider that shows a person away.

An absence starts at the scenario's start (`AbsenceTrigger.AT_START`) or at the agent's first message to the
person, on any provider (`ON_FIRST_ASK`), each plus its own `starts_after`, and lasts `lasts`. One anchored on a
first ask is not placed until that message exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from minutehand.domain.world import Actor, MessageSnapshot, Operation, WorldEvent


def first_ask(person: str, email: str | None, events: Iterable[WorldEvent]) -> datetime | None:
    """When the agent first wrote to the person keyed `person` (or, in a record that names no keys, to `email`, when
    they have one), on any provider; None before it has."""
    return next(
        (
            e.sim_time
            for e in events
            if e.actor is Actor.AGENT
            and e.operation is Operation.CREATE
            and isinstance(e.after, MessageSnapshot)
            and (person in e.after.recipients or (email is not None and email in e.after.recipient_emails))
        ),
        None,
    )


def placed(
    *, from_start: datetime | None, asked: datetime | None, starts_after: timedelta, lasts: timedelta
) -> tuple[datetime, datetime] | None:
    """The stretch an absence covers: anchored at `from_start` when it starts with the scenario, else at `asked`,
    the first ask; None while that has not happened."""
    anchor = from_start if from_start is not None else asked
    if anchor is None:
        return None
    start = anchor + starts_after
    return start, start + lasts
