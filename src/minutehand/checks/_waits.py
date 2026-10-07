"""How long the agent took on each wait: the one reading of an obligation that the
scorecard and the time checks share, so they cannot disagree about a number."""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import AwareDatetime, Field

from minutehand.domain.checks import Needs, Obligation, ObligationKind, RunView
from minutehand.domain.clock import AGENT_SOURCES, DueClosed, DueEntry, DueSource
from minutehand.domain.scenario import DispatchFault, Model
from minutehand.domain.world import Operation, WorldEvent

GRACE = timedelta(hours=1)
"""How long after the moment it should act the agent may take before the time counts against it."""

VISIBLE = frozenset({Operation.CREATE, Operation.UPDATE, Operation.DELETE})
"""What a person can see the agent did: a read or a search tells them nothing."""


class Expiry(Model):
    """One moment a wait fell due while still open, and the first follow-up at or after it."""

    expired: AwareDatetime
    closes: AwareDatetime
    touch: int | None
    touched_at: AwareDatetime | None

    @property
    def gap(self) -> timedelta:
        """From falling due to the follow-up, or to the close when there was none."""
        return (self.touched_at or self.closes) - self.expired

    @property
    def late(self) -> bool:
        return self.touch is None or self.gap > GRACE


class Chase(Model):
    """A wait read for follow-ups: every one the agent made, and every moment one fell due.

    A follow-up is an agent write the person could see (a message to them or their delegate, a change to the
    ticket or the thread) while the wait is open, whether it came before the wait fell due or after. A read is
    not one: looking at the channel tells nobody anything. The wait falls due at its `expected_by`; a follow-up
    gives the person its `patience` again from the moment it was sent, so it falls due again then. Without a
    patience (work, which has its own pace) the first follow-up after the date answers it for good.
    """

    obligation: Obligation
    follow_ups: list[int] = Field(description="WorldEvent.seq of each follow-up, in order")
    follow_up_times: list[AwareDatetime]
    expiries: list[Expiry]
    early: list[int] = Field(
        default=[], description="WorldEvent.seq of each follow-up sent before the wait had fallen due"
    )

    @property
    def abandoned(self) -> Expiry | None:
        """The last moment due, when nothing followed it before the wait closed."""
        return self.expiries[-1] if self.expiries and self.expiries[-1].touch is None else None


def chase(o: Obligation, events: dict[int, WorldEvent], ended: datetime) -> Chase:
    """Every follow-up on `o` and every moment it fell due before it settled or the run ended."""
    closes = o.settled_at or ended
    seen = sorted(
        (events[s].sim_time, s)
        for s in o.agent_touches
        if s in events
        and events[s].operation in VISIBLE
        and not _unchanged(events[s], events)
        and (o.settled_at is None or events[s].sim_time < o.settled_at)  # only while the wait is open
    )
    due = max(o.expected_by, o.opened_at) if o.expected_by is not None and o.kind is not ObligationKind.DATE else None
    expiries: list[Expiry] = []
    early: list[int] = []
    for touched_at, seq in seen:
        if due is None:
            break
        if touched_at < due:
            early.append(seq)
            if o.patience is not None:
                due = max(due, touched_at + o.patience)
            continue
        if due >= closes:
            break
        expiries.append(Expiry(expired=due, closes=closes, touch=seq, touched_at=touched_at))
        due = touched_at + o.patience if o.patience is not None else None
    if due is not None and due < closes:
        expiries.append(Expiry(expired=due, closes=closes, touch=None, touched_at=None))
    return Chase(
        obligation=o,
        follow_ups=[s for _, s in seen],
        follow_up_times=[t for t, _ in seen],
        expiries=expiries,
        early=early,
    )


def chases(view: RunView, ended: datetime) -> list[Chase]:
    """Every wait in the view read for follow-ups, in ledger order, up to `ended`: the one reading the scorecard
    counts and the viewer draws. The scenario's deadline is not a wait."""
    by_seq = {e.seq: e for e in view.events}
    return [chase(o, by_seq, ended) for o in view.obligations if o.kind is not ObligationKind.DATE]


def _unchanged(event: WorldEvent, events: dict[int, WorldEvent]) -> bool:
    """An update that leaves the entity as it was: an edit nobody can see, so not a follow-up."""
    if event.operation is not Operation.UPDATE:
        return False
    before = max(
        (e for e in events.values() if e.seq < event.seq and e.entity == event.entity and e.after is not None),
        key=lambda e: e.seq,
        default=None,
    )
    return before is not None and before.after == event.after


class Reaction(Model):
    """A wait the world settled, and how long the agent took to come back to it."""

    obligation: Obligation
    settled: AwareDatetime
    touch: int | None
    touched_at: AwareDatetime | None
    ended: AwareDatetime

    @property
    def gap(self) -> timedelta:
        return (self.touched_at or self.ended) - self.settled

    @property
    def slow(self) -> bool:
        return self.gap > GRACE


def ended_at(view: RunView) -> datetime:
    """The last moment the run reached: its last event or its last wake, whichever is later."""
    moments = [e.sim_time for e in view.events] + [w.sim_time for w in view.wakes]
    return max(moments, default=view.scenario.starts_at)


def reaction(o: Obligation, when: dict[int, datetime], ended: datetime) -> Reaction | None:
    """None when there is nothing to react to, or nothing the agent could touch to react.

    An obligation naming neither a person nor an entity (one read from an agent's own
    records rather than built by the ledger) has no reaction that can be timed.
    """
    if o.kind is ObligationKind.DATE or o.settled_at is None or (o.person is None and o.entity is None):
        return None
    touch = o.first_touch_after_settled
    return Reaction(
        obligation=o,
        settled=o.settled_at,
        touch=touch,
        touched_at=when[touch] if touch is not None and touch in when else None,
        ended=max(ended, o.settled_at),
    )


def blocked(view: RunView, needs: frozenset[Needs], check: str) -> list[str]:
    """What this check needs and the view does not carry. Non-empty means the check did not run."""
    missing: list[str] = []
    if Needs.OBLIGATIONS in needs and not view.obligations:
        missing.append(f"{check}: no obligations in the view; the ledger did not run or found nothing to wait on")
    if Needs.COMMITMENTS in needs and view.commitments is None:
        missing.append(f"{check}: the agent reported no commitments")
    if Needs.WAKES in needs and not view.wakes:
        missing.append(f"{check}: no wakes recorded")
    if Needs.CALLS in needs and view.unmatched_calls is None:
        missing.append(f"{check}: nobody recorded which calls reached no provider")
    return missing


def span(delta: timedelta) -> str:
    """A stretch of simulated time in days and hours, the way the findings say it."""
    hours = round(delta.total_seconds() / 3600)
    days, hours = divmod(hours, 24)
    parts = [f"{days} day{'s' if days != 1 else ''}"] if days else []
    if hours or not days:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    return " ".join(parts)


class Plan(Model):
    """The agent's own plan to come back to work as it stood at one moment: the earliest wake it had reported,
    booked or declared a rhythm for that was still in the run loop's table then (`DueEntry`)."""

    moment: AwareDatetime
    earliest: DueEntry | None = Field(description="None: the agent had asked for no wake of its own")
    dropped: DueEntry | None = Field(
        default=None,
        description="A wake the agent had asked for within the grace of `moment` that the scenario's dispatch rules "
        "dropped: the agent's plan was right and the delivery failed it",
    )

    @property
    def past_due(self) -> bool:
        """Whether the agent planned to be away more than the grace past `moment`."""
        return self.earliest is None or self.earliest.due.at - self.moment > GRACE

    def said(self) -> str:
        lost = (
            f", though it had asked for one then ({_PLANNED[self.dropped.source]} in wake {self.dropped.entered_wake}) "
            "that the scenario's dispatch rules dropped"
            if self.dropped is not None and self.past_due
            else ""
        )
        if self.earliest is None:
            return f"the agent had asked for no wake of its own{lost}"
        how = _PLANNED[self.earliest.source]
        if not self.past_due:
            return f"the agent's own next wake was due then ({how} in wake {self.earliest.entered_wake})"
        late = self.earliest.asked_for
        if late is not None and self.earliest.fault is DispatchFault.LATE and late - self.moment <= GRACE:
            return (
                f"the agent's own next wake was {span(self.earliest.due.at - self.moment)} later: it had asked for one "
                f"then ({how} in wake {self.earliest.entered_wake}) that the scenario's dispatch rules delivered late"
            )
        return (
            f"the agent's own next wake was {span(self.earliest.due.at - self.moment)} later "
            f"({how} in wake {self.earliest.entered_wake}){lost}"
        )


_PLANNED = {
    DueSource.REPORTED: "reported",
    DueSource.BOOKED: "booked",
    DueSource.POLLED: "its declared rhythm, set",
    DueSource.TIMER: "its own timer, read from its sandbox",
}


def plan_at(dues: list[DueEntry], moment: datetime) -> Plan:
    """What the agent had planned at `moment`, read from the table as the log recorded it."""
    planned = [
        d
        for d in dues
        if d.source in AGENT_SOURCES
        and d.open_at(moment)
        and d.closed not in (DueClosed.DELAYED, DueClosed.DROPPED)  # never delivered at that moment
    ]
    dropped = [
        d
        for d in dues
        if d.source in AGENT_SOURCES
        and d.closed is DueClosed.DROPPED
        and d.entered_at <= moment
        and abs(d.due.at - moment) <= GRACE
    ]
    return Plan(
        moment=moment,
        earliest=min(planned, key=lambda d: d.due.at, default=None),
        dropped=max(dropped, key=lambda d: d.due.at, default=None),
    )
