"""The run loop's table of what is due next, written into the world's own log as it changes.

Every entry is an entity of `EntityKind.DUE`, actor SCENARIO: a version when it enters the table and one when it
leaves, fired, replaced or cancelled (`domain.clock.DueEntry`). The table itself is what the loop dispatches from;
the log is how a check reads what the agent had planned at any moment, and how a fork reads the table as it stood
at its checkpoint. Nothing here decides what is due: it records what the loop put in and took out.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from pydantic import Field

from minutehand.application.checkpoint import (
    Pending,
    PendingBooking,
    PendingDirection,
    PendingFate,
    PendingHappening,
    PendingReply,
    PendingWake,
)
from minutehand.domain.agent import WakeReason
from minutehand.domain.clock import PLANNED_BY, REACHED, Due, DueClosed, DueEntry, DueSource
from minutehand.domain.scenario import DispatchFault, DispatchRule, Model, PlannedBy
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, WorldEvent
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store


def source_of(pending: Pending) -> DueSource:
    if isinstance(pending, PendingWake):
        return DueSource.POLLED if pending.reason is WakeReason.TICK else DueSource.REPORTED
    if isinstance(pending, PendingBooking):
        return DueSource.BOOKED
    if isinstance(pending, PendingReply):
        return DueSource.REPLY
    if isinstance(pending, PendingFate):
        return DueSource.FATE
    if isinstance(pending, PendingHappening):
        return DueSource.HAPPENING
    assert isinstance(pending, PendingDirection)
    return DueSource.DIRECTION


def due_entries(store: Store) -> list[DueEntry]:
    """Every entry the table ever held, as each last stood, in the order they entered."""
    refs = list(dict.fromkeys(e.entity for e in store.events() if e.entity.kind is EntityKind.DUE))
    latest = [store.versions(ref)[-1] for ref in refs]
    return [DueEntry.model_validate_json(v.body) for v in latest]


def due_events(events: list[WorldEvent]) -> list[WorldEvent]:
    """The table's own rows, which a reader listing the world leaves out as it leaves out checkpoints."""
    return [e for e in events if e.entity.kind is EntityKind.DUE]


Key = tuple[str, str, datetime]


class Dispatched(Model):
    """What the run loop does with the entries the clock has reached."""

    delivered: list[Pending] = Field(description="Dispatched now")
    withheld: list[Pending] = Field(description="The agent's own wakes a dispatch rule made late or dropped")
    finished: list[PendingBooking] = Field(
        default=[], description="Bookings dropped: their occurrence is over undelivered, and the scheduler moves on"
    )


def key_of(due: Due) -> Key:
    """What makes two entries the same: what is due, whose, and when."""
    return (due.kind.value, due.ref, due.at)


class Dues:
    """What the run loop holds pending, recorded as it changes."""

    def __init__(self, store: Store, clock: Clock, rules: Sequence[DispatchRule] = ()) -> None:
        self._store = store
        self._clock = clock
        self._rules = list(rules)
        self._items: list[Pending] = []
        self._open: dict[Key, tuple[EntityRef, DueEntry]] = {}
        self._made = 0
        self._reached: dict[PlannedBy, int] = {}

    @property
    def items(self) -> list[Pending]:
        return list(self._items)

    def enter(self, pending: Pending, *, fault: DispatchFault | None = None, asked_for: datetime | None = None) -> None:
        """Put an entry in the table. One that names the same moment as an entry already there from the same
        source is that entry, unchanged: an agent that reports the same next wake after every wake planned it once.
        `fault` and `asked_for` mark a late or second delivery a dispatch rule entered."""
        if key_of(pending.due) in self._open:
            return
        self._items.append(pending)
        self._made += 1
        ref = EntityRef(provider="minutehand", kind=EntityKind.DUE, external_id=f"due:{self._made}")
        entry = DueEntry(
            due=pending.due,
            source=source_of(pending),
            entered_at=self._clock.now(),
            entered_wake=self._clock.wake(),
            fault=fault,
            asked_for=asked_for,
        )
        self._write(ref, entry, Operation.CREATE)
        self._open[key_of(pending.due)] = (ref, entry)

    def replace(self, where: Callable[[Pending], bool], by: Pending | None) -> None:
        """Take out every entry `where` holds, as replaced, and put `by` in their place. An entry the same as
        `by` stays as it was."""
        keep = key_of(by.due) if by is not None else None
        self._leave([p for p in self._items if where(p) and key_of(p.due) != keep], DueClosed.REPLACED)
        if by is not None:
            self.enter(by)

    def cancel(self, where: Callable[[Pending], bool]) -> None:
        """Take out every entry `where` holds, undispatched."""
        self._leave([p for p in self._items if where(p)], DueClosed.CANCELLED)

    def dispatch(self, firing: list[Due]) -> Dispatched:
        """Take out what the clock has reached and decide each: an agent's own wake by the scenario's dispatch rule
        for it, if any (late: held back and a late delivery entered; twice: delivered, and a second delivery
        entered; dropped: never delivered); anything else, and a late or second delivery itself, delivered as is."""
        due = {key_of(d) for d in firing}
        reached = [p for p in self._items if key_of(p.due) in due]
        gone = {id(p) for p in reached}
        self._items = [p for p in self._items if id(p) not in gone]
        delivered: list[Pending] = []
        withheld: list[Pending] = []
        finished: list[PendingBooking] = []
        for p in reached:
            ref, entry = self._open.pop(key_of(p.due))
            planned = PLANNED_BY.get(entry.source)
            rule = self._rule(planned) if planned is not None and entry.asked_for is None else None
            if rule is None:
                self._close(ref, entry, DueClosed.FIRED)
                delivered.append(p)
                continue
            if rule.fault is DispatchFault.DROPPED:
                self._close(ref, entry, DueClosed.DROPPED, fault=rule.fault)
                withheld.append(p)
                if isinstance(p, PendingBooking):
                    finished.append(p)
                continue
            assert rule.by is not None
            if rule.fault is DispatchFault.LATE:
                self._close(ref, entry, DueClosed.DELAYED, fault=rule.fault)
                withheld.append(p)
            else:
                self._close(ref, entry, DueClosed.FIRED, fault=rule.fault)
                # the first of two deliveries leaves the occurrence open: the second finishes it
                delivered.append(p.model_copy(update={"advance": False}) if isinstance(p, PendingBooking) else p)
            self.enter(_again(p, self._clock.now() + rule.by, rule.fault), fault=rule.fault, asked_for=entry.due.at)
        return Dispatched(delivered=delivered, withheld=withheld, finished=finished)

    def _rule(self, planned: PlannedBy) -> DispatchRule | None:
        """Count one more of the agent's wakes of this kind reaching its moment, and the rule for it: one for the
        nth wins over one for each."""
        n = self._reached.get(planned, 0) + 1
        self._reached[planned] = n
        mine = [r for r in self._rules if r.wakes is planned]
        return next((r for r in mine if r.nth == n), None) or next((r for r in mine if r.nth is None), None)

    def resume(self, pending: list[Pending]) -> None:
        """Take up the table a fork's checkpoint holds. The log as the fork shares it says what was open: an entry
        the checkpoint dropped (a reply withdrawn by a `PersonChange`) is cancelled, and one it added is entered."""
        self._items = []
        self._open = {}
        refs = list(dict.fromkeys(e.entity for e in self._store.events() if e.entity.kind is EntityKind.DUE))
        self._made = len(refs)
        self._reached = {}
        logged: dict[Key, tuple[EntityRef, DueEntry]] = {}
        for ref in refs:
            entry = DueEntry.model_validate_json(self._store.versions(ref)[-1].body)
            planned = PLANNED_BY.get(entry.source)
            if planned is not None and entry.asked_for is None and entry.closed in REACHED:
                self._reached[planned] = self._reached.get(planned, 0) + 1
            if entry.closed is None:
                logged[key_of(entry.due)] = (ref, entry)
        wanted = {key_of(p.due) for p in pending}
        for key, (ref, entry) in logged.items():
            if key in wanted:
                self._open[key] = (ref, entry)
            else:
                self._close(ref, entry, DueClosed.CANCELLED)
        for p in pending:
            if key_of(p.due) in self._open:
                self._items.append(p)
            else:
                self.enter(p)

    def _leave(self, leaving: list[Pending], how: DueClosed) -> None:
        if not leaving:
            return
        gone = {id(p) for p in leaving}
        self._items = [p for p in self._items if id(p) not in gone]
        for p in leaving:
            opened = self._open.pop(key_of(p.due), None)
            if opened is not None:
                self._close(*opened, how)

    def _close(self, ref: EntityRef, entry: DueEntry, how: DueClosed, fault: DispatchFault | None = None) -> None:
        closed = entry.model_copy(
            update={
                "closed": how,
                "closed_at": self._clock.now(),
                "closed_wake": self._clock.wake(),
                "fault": fault or entry.fault,
            }
        )
        self._write(ref, closed, Operation.UPDATE)

    def _write(self, ref: EntityRef, entry: DueEntry, operation: Operation) -> None:
        self._store.apply(Change(entity=ref, operation=operation, actor=Actor.SCENARIO, body=entry.model_dump_json()))


def _again(pending: Pending, at: datetime, fault: DispatchFault) -> Pending:
    """The late or second delivery of one of the agent's own wakes, at `at`. It keeps whatever its delivery needs
    (a booking's own reference), and a tick of it books no next tick."""
    due = Due(at=at, kind=pending.due.kind, ref=f"{pending.due.ref}~{fault.value}")
    if isinstance(pending, PendingWake):
        return pending.model_copy(update={"due": due, "repeat": True})
    return pending.model_copy(update={"due": due})
