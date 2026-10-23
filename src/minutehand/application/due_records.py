"""The agent's due records (`AgentUnderTest.due`): read from its memory as it stands whenever the run decides its
next jump, the earliest becoming what is next due.

An agent whose scheduler polls its own records for what has come due (a job table's run-at column, a next-check
field) books nothing: it keeps a projection of those records in its memory, and the agent file says where. Each
decision reads them afresh, never from an earlier read, so a record moved earlier, moved later, cleared or closed
since is followed as it now stands; a booking would have frozen the moment it was taken from.

The wake is the clock reaching the record's instant, or `poll` after it, the latest its own poller would have acted:
Minutehand calls nothing, and the agent's own poller finds the work when its own clock passes it. A record wakes the
agent at most once for one instant: woken and left holding the same instant, it is not woken again (the check
`due_records` says it was left due), and a new instant is new. Which have woken is read from the run loop's table in
the log (`DueEntry.records`), so a fork knows what its parent woke for up to the checkpoint.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from minutehand.application.checkpoint import PendingRecord
from minutehand.application.dues import due_entries
from minutehand.application.memory import held, value_of
from minutehand.domain.clock import REACHED, Due, DueKind, DueSource
from minutehand.domain.memory import DueRecords, RecordedDue, field_at, instant, matches
from minutehand.ports.store import Store

Woken = tuple[str, str, datetime]
"""A record woken for: its collection, its key, and the instant it held."""


def read(store: Store, sources: Sequence[DueRecords]) -> list[RecordedDue]:
    """Every due record the memory holds now, by the agent file's declarations, in their order and by key. A record
    whose field is absent or null is not due; one whose field is not an instant with an offset is not read as one
    (the check `due_records` names it)."""
    found: list[RecordedDue] = []
    for n, source in enumerate(sources):
        for key, body in held(store, source.collection, source.prefix):
            value = value_of(body)
            if not matches(value, source.where):
                continue
            there, written = field_at(value, source.field)
            moment = instant(written) if there else None
            if moment is not None:
                found.append(RecordedDue(source=n, collection=source.collection, key=key, at=moment))
    return found


def woken(store: Store) -> set[Woken]:
    """Every record the run has woken the agent for, with the instant it held then, as the log records it."""
    return {
        (r.collection, r.key, r.at)
        for entry in due_entries(store)
        if entry.source is DueSource.RECORDED and entry.closed in REACHED
        for r in entry.records
    }


def woken_by(pending: PendingRecord) -> set[Woken]:
    return {(r.collection, r.key, r.at) for r in pending.records}


def next_due(
    records: Sequence[RecordedDue], sources: Sequence[DueRecords], already: set[Woken], now: datetime
) -> PendingRecord | None:
    """What is next due of `records`: the earliest moment any not yet woken for its instant falls due (its instant
    and its declaration's `poll`, or now when that has passed), with every record falling due then. None when none
    is left to wake for."""
    waiting = [
        (max(r.at + sources[r.source].poll, now), r) for r in records if (r.collection, r.key, r.at) not in already
    ]
    if not waiting:
        return None
    at = min(moment for moment, _ in waiting)
    due = sorted((r for moment, r in waiting if moment == at), key=lambda r: (r.collection, r.key))
    named = " ".join(f"{r.collection}/{r.key}={r.at.isoformat()}" for r in due)
    return PendingRecord(due=Due(at=at, kind=DueKind.AGENT_WAKE, ref=f"record:{named}"), records=due)
