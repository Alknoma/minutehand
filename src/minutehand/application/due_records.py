"""The agent's due records (`AgentUnderTest.due`): read from its memory as it stands whenever the run decides its
next jump, the earliest becoming what is next due.

An agent whose scheduler polls its own records for what has come due (a job table's run-at column, a next-check
field) books nothing: it keeps a projection of those records in its memory, and the agent file says where. Each
decision reads them afresh, never from an earlier read, so a record moved earlier, moved later, cleared or closed
since is followed as it now stands; a booking would have frozen the moment it was taken from.

The wake is the clock reaching the record's instant, or `poll` after it, the latest its own poller would have looked:
Minutehand calls nothing, and the agent's own poller finds the work when its own clock passes it. A record whose
instant is not later than now at a decision that reads it (written by the work just done, a next check of now, moved,
or held back until then) is due at once: the next wake begins with no jump. A declaration's `later` fields hold the
record back: it is due only while each is absent, null, or an instant later than the moment it would fall due, and
one that is not is read again at the next decision, as it then stands.

Where the clock lands is never what makes the wake hold the poller's work. A poller looks on its own timer, which no
one outside it can read (one that sleeps `poll` from the end of its own work looks later than one that sleeps from
its start, and later again after a long cycle), and the sandbox's clock runs on at real speed inside a wake. So a wake
begun for due records is not over until the agent has picked one of each declaration's up, a newer write of it in
the run's store than the one the decision read (`domain.world.written`, `unpicked`): moved later, rewritten to now, rewritten to
what it held, or deleted, each counts, and the value is never compared. Then the sandbox settles, and the wake ends.
A record whose new value is still due is the next thing due, and the next wake begins at once, so a poller that
checks again straight away runs back-to-back wakes, each holding one cycle. A declaration none of whose records the
agent wrote within one `poll` of the sandbox's clock from the wake's start ends the wake all the same, and the check
`due_records` says it was not picked up, so a poller that never looks cannot hold a run.

A record wakes the agent once for one instant by one declaration: woken and left holding the same instant, it is not
woken again (the check `due_records` says it was not picked up when the wake wrote none of its declaration's), and a
new instant is new. Another declaration holding the same instant on the same record is woken for by itself. The one
exception is a backlog its poller takes a record at a time: the wake ends once the first is written and the sandbox
settles, before the poller looks again. A record left due by a wake that took another record of the same declaration (one no longer read by it, or holding a later
instant) is due again at once at the decision that follows that wake; one left due by a wake that took none is not.
Which have woken, and when, and what each wake took, is read from the run loop's table in the log
(`DueEntry.records`, each naming its declaration) and the memory as each wake left it, so a fork knows what its
parent woke for up to the checkpoint.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from pydantic import AwareDatetime, Field, JsonValue

from minutehand.application.checkpoint import PendingRecord
from minutehand.application.dues import due_entries
from minutehand.application.memory import held, memory_of, value_of
from minutehand.domain.clock import REACHED, Due, DueKind, DueSource
from minutehand.domain.memory import DueRecords, RecordedDue, field_at, instant, later_bound, matches
from minutehand.domain.model import Model
from minutehand.domain.world import EntityKind
from minutehand.ports.store import Store

Woken = tuple[int, str, str, datetime]
"""A record woken for: the declaration it was read by, its collection, its key, and the instant it held."""

Read = tuple[RecordedDue, datetime | None]
"""A due record as the run read it, and the earliest instant its declaration's `later` fields held (None for none):
it is due by that declaration only before then."""

Held = Callable[[str, str], Iterable[tuple[str, JsonValue]]]
"""(key, value) of every key of a collection under a prefix, as some moment of the memory holds them."""


class Woke(Model):
    """The last wake a record was woken for in, by one declaration."""

    at: AwareDatetime = Field(description="The moment it was woken for")
    records: list[RecordedDue] = Field(description="Every record that wake was for")
    took: bool | None = Field(
        description="Whether that wake took another record of the same declaration; None until it has ended"
    )


def read(store: Store, sources: Sequence[DueRecords]) -> list[Read]:
    """Every due record the memory holds now, by the agent file's declarations, in their order and by key. A record
    whose field is absent or null is not due; one whose field, or one of whose `later` fields, holds something not an
    instant with an offset is not read as one (the check `due_records` names it)."""
    return _read(sources, lambda collection, prefix: ((k, value_of(b)) for k, b in held(store, collection, prefix)))


def _read(sources: Sequence[DueRecords], holding: Held) -> list[Read]:
    found: list[Read] = []
    for n, source in enumerate(sources):
        for key, value in holding(source.collection, source.prefix):
            if not matches(value, source.where):
                continue
            there, written = field_at(value, source.field)
            moment = instant(written) if there else None
            readable, until = later_bound(value, source.later)
            if moment is not None and readable:
                found.append((RecordedDue(source=n, collection=source.collection, key=key, at=moment), until))
    return found


def key_of(record: RecordedDue) -> Woken:
    return (record.source, record.collection, record.key, record.at)


def took(woke: Sequence[RecordedDue], record: RecordedDue, now: Sequence[Read]) -> bool:
    """Whether the wake for `woke` took a record of `record`'s declaration other than it, by the memory as `now`
    reads it: one no longer read by the declaration, or holding a later instant than it was woken for."""
    holding = {(r.source, r.collection, r.key): r.at for r, _ in now}
    for other in woke:
        if other.source != record.source or (other.collection, other.key) == (record.collection, record.key):
            continue
        where = (other.source, other.collection, other.key)
        if where not in holding or holding[where] > other.at:
            return True
    return False


def woken(store: Store, sources: Sequence[DueRecords]) -> dict[Woken, Woke]:
    """Every record the run has woken the agent for, with the instant it held then, the last wake for it, and what
    that wake took by the memory as it left it, as the log records them."""
    last: dict[Woken, tuple[datetime, int, list[RecordedDue]]] = {}
    for entry in due_entries(store):
        if entry.source is not DueSource.RECORDED or entry.closed not in REACHED:
            continue
        if entry.closed_at is None or entry.closed_wake is None:
            continue
        for r in entry.records:
            key = key_of(r)
            if key not in last or last[key][0] <= entry.closed_at:
                last[key] = (entry.closed_at, entry.closed_wake + 1, entry.records)  # the wake it began
    events = store.events(kinds=[EntityKind.MEMORY])
    after: dict[int, list[Read]] = {}
    found: dict[Woken, Woke] = {}
    for key, (at, wake, records) in last.items():
        if wake not in after:
            until = max((e.seq for e in events if e.wake <= wake), default=0)
            memory = memory_of(events, until=until)
            after[wake] = _read(sources, _holding(memory))
        record = next(r for r in records if key_of(r) == key)
        found[key] = Woke(at=at, records=records, took=took(records, record, after[wake]))
    return found


def _holding(memory: Mapping[tuple[str, str], str]) -> Held:
    def holding(collection: str, prefix: str) -> list[tuple[str, JsonValue]]:
        keys = sorted(k for c, k in memory if c == collection and k.startswith(prefix))
        return [(k, json.loads(memory[(collection, k)])) for k in keys]

    return holding


def woken_by(pending: PendingRecord, at: datetime) -> dict[Woken, Woke]:
    return {key_of(r): Woke(at=at, records=pending.records, took=None) for r in pending.records}


def ended(already: Mapping[Woken, Woke], now: Sequence[Read]) -> dict[Woken, Woke]:
    """`already`, each wake that has ended since told what it took, by the memory as `now` reads it: the first
    decision after a wake reads the memory as that wake left it."""
    return {
        key: woke
        if woke.took is not None
        else woke.model_copy(
            update={"took": took(woke.records, next(r for r in woke.records if key_of(r) == key), now)}
        )
        for key, woke in already.items()
    }


def _moment(base: datetime, poll: timedelta, now: datetime) -> datetime:
    """`poll` after `base` while `base` is still to come, the latest the agent's poller would have looked, so its
    timer has passed when the sandbox's clock is moved there and the wait for its write is short; now once `base`
    has passed, as the record is due already and the wake begins with no jump. Neither is a bet on the poller's
    timer: the wake lasts until the record is written (`domain.world.unpicked`), or one `poll` of the sandbox's clock."""
    return base + poll if base > now else now


def next_due(
    records: Sequence[Read], sources: Sequence[DueRecords], already: Mapping[Woken, Woke], now: datetime
) -> PendingRecord | None:
    """What is next due of `records`: the earliest moment any falls due, with every record falling due then. One not
    yet woken for its instant by its declaration falls due `poll` after that instant while the instant is still to
    come; one woken for and left due, only when that wake took another record of its declaration; either, once its
    instant or that wake has passed, now. Either is due only while its `later` fields are later than that moment.
    None when none is left to wake for."""
    waiting: list[tuple[datetime, RecordedDue]] = []
    for r, until in records:
        source = sources[r.source]
        key = key_of(r)
        woke = already[key] if key in already else None
        if woke is not None and not woke.took:
            continue
        moment = _moment(r.at if woke is None else woke.at, source.poll, now)
        if until is None or until > moment:
            waiting.append((moment, r))
    if not waiting:
        return None
    at = min(moment for moment, _ in waiting)
    due = sorted((r for moment, r in waiting if moment == at), key=lambda r: (r.collection, r.key, r.source))
    named = " ".join(f"{r.collection}/{r.key}={r.at.isoformat()}" for r in due)
    return PendingRecord(due=Due(at=at, kind=DueKind.AGENT_WAKE, ref=f"record:{named}"), records=due)
