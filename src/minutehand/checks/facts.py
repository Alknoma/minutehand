"""The facts of a run, read from its view: what assessments count, and what an agent's own Python check may read.

Nothing here judges. Each function answers what happened and when: the asks and what followed them, the messages
the agent sent, every change it made, its wakes, the wakes it planned, and what it reported. The YAML rules of
`domain/assessments.py` count these, and a team's own check (`AgentUnderTest.checks`) may import them as they are.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from pydantic import AwareDatetime, Field

from minutehand.checks.ledger import Away, absences, carries, recipients
from minutehand.domain.agent import CommitmentStatus
from minutehand.domain.checks import Needs, Obligation, ObligationKind, RunView
from minutehand.domain.clock import AGENT_SOURCES, REACHED, DueClosed
from minutehand.domain.scenario import DispatchFault, Model, TicketState
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    InboxItemSnapshot,
    ItemStatus,
    MessageSnapshot,
    Operation,
    TicketSnapshot,
    WorldEvent,
)

VISIBLE = frozenset({Operation.CREATE, Operation.UPDATE, Operation.DELETE})
"""What a person can see the agent did: a read or a search tells them nothing."""


class Fact(Model):
    """One thing that happened, and when: what a rule counts."""

    at: AwareDatetime
    seqs: list[int] = Field(default=[], description="WorldEvent.seq of what shows it; empty for a wake or a plan")


class Ask(Model):
    """A wait the agent opened on a person: a question they can answer, an item in its product, or work handed to
    them; and what the agent did about it."""

    obligation: Obligation
    follow_ups: list[Fact] = Field(description="The agent's writes the person could see while it was open, in order")
    touches: list[Fact] = Field(description="Every write of the agent's on the person, the thread or the ticket")
    answer: str | None = Field(default=None, description="What the person answered, when they did by a reply")

    @property
    def person(self) -> str | None:
        return self.obligation.person

    @property
    def at(self) -> datetime:
        return self.obligation.opened_at

    @property
    def answered_at(self) -> datetime | None:
        return self.obligation.settled_at

    def open_at(self, moment: datetime) -> bool:
        """Made by `moment`, and not yet answered then."""
        return self.at <= moment and (self.answered_at is None or moment < self.answered_at)


def ended_at(view: RunView) -> datetime:
    """The last moment the run reached: its last event or its last wake, whichever is later."""
    moments = [e.sim_time for e in view.events] + [w.sim_time for w in view.wakes]
    return max(moments, default=view.scenario.starts_at)


def asks(view: RunView, kind: ObligationKind = ObligationKind.ANSWER_FROM_PERSON) -> list[Ask]:
    """Every wait of `kind` the ledger opened, in the order it opened, with what followed it."""
    by_seq = {e.seq: e for e in view.events}
    found: list[Ask] = []
    for o in view.obligations:
        if o.kind is not kind:
            continue
        follow_ups = [
            Fact(at=by_seq[s].sim_time, seqs=[s])
            for s in o.agent_touches
            if s in by_seq
            and by_seq[s].operation in VISIBLE
            and not unchanged(by_seq[s], by_seq)
            and (o.settled_at is None or by_seq[s].sim_time < o.settled_at)
        ]
        found.append(
            Ask(
                obligation=o,
                follow_ups=sorted(follow_ups, key=lambda f: f.at),
                touches=_touches(view, o, by_seq),
                answer=_answer(view, o),
            )
        )
    return found


def _touches(view: RunView, o: Obligation, by_seq: dict[int, WorldEvent]) -> list[Fact]:
    person = next((p for p in view.scenario.people if p.key == o.person), None)
    emails = {person.email} if person is not None else set()
    touched: list[Fact] = []
    for event in view.events:
        if event.actor is not Actor.AGENT or event.seq <= o.opened_by or event.operation not in VISIBLE:
            continue
        if unchanged(event, by_seq):
            continue
        after = event.after
        on_it = o.entity is not None and (
            event.entity == o.entity or (isinstance(after, MessageSnapshot) and after.thread_of == o.entity.external_id)
        )
        to_them = (
            isinstance(after, MessageSnapshot)
            and event.operation is Operation.CREATE
            and bool(emails.intersection(after.recipient_emails))
        )
        if on_it or to_them or event.seq in o.agent_touches or event.seq == o.first_touch_after_settled:
            touched.append(Fact(at=event.sim_time, seqs=[event.seq]))
    return touched


def _answer(view: RunView, o: Obligation) -> str | None:
    if o.settled_at is None or o.person is None:
        return None
    said = [r for r in view.replies if r.person == o.person and r.at == o.settled_at]
    return said[0].text if said else None


def unchanged(event: WorldEvent, events: dict[int, WorldEvent]) -> bool:
    """An update that leaves the entity as it was: an edit nobody can see."""
    if event.operation is not Operation.UPDATE:
        return False
    before = max(
        (e for e in events.values() if e.seq < event.seq and e.entity == event.entity and e.after is not None),
        key=lambda e: e.seq,
        default=None,
    )
    return before is not None and before.after == event.after


class Sent(Model):
    """A message the agent sent, and to which of the scenario's people."""

    event: WorldEvent
    to: list[str] = Field(description="Person.key of each of the scenario's people it was addressed to")
    to_away: list[str] = Field(default=[], description="Of those, the ones away then while a delegate covered")

    @property
    def text(self) -> str:
        assert isinstance(self.event.after, MessageSnapshot)
        return self.event.after.text

    @property
    def thread_of(self) -> str | None:
        assert isinstance(self.event.after, MessageSnapshot)
        return self.event.after.thread_of


def messages(view: RunView) -> list[Sent]:
    """Every message the agent sent, in order."""
    away: list[Away] = [a for a in absences(view.scenario, view.events) if a.delegate is not None]
    sent: list[Sent] = []
    for event in view.events:
        if event.actor is not Actor.AGENT or event.operation is not Operation.CREATE:
            continue
        if not isinstance(event.after, MessageSnapshot):
            continue
        to = [p.key for p in recipients(event, view.scenario)]
        out = [k for k in to if any(a.person == k and a.covers(event.sim_time) for a in away)]
        sent.append(Sent(event=event, to=to, to_away=out))
    return sent


class Written(Model):
    """A change the agent made to the world, with what is known of it."""

    event: WorldEvent
    repeats_open_ticket: bool = Field(
        default=False, description="A ticket created with the title of one still open in the same project"
    )
    in_repeated_wake: bool = Field(default=False, description="Written in the second delivery of one wake")
    gated: bool = Field(
        default=False,
        description="A call carrying an operation an item held back, made while the item was pending, turned down, "
        "or taken back",
    )


_NOT_WRITES = frozenset({EntityKind.DUE, EntityKind.CHANNEL, EntityKind.MEMORY, EntityKind.NEXT_WAKE})
_WORD = re.compile(r"\w+")


def normalised(title: str) -> str:
    """Case, punctuation and spacing removed: what two titles are, once nobody's typing is counted."""
    return " ".join(_WORD.findall(title.casefold()))


def writes(view: RunView) -> list[Written]:
    """Every change the agent made that a person could see, in order."""
    by_seq = {e.seq: e for e in view.events}
    repeated = repeated_wakes(view)
    duplicates = _duplicates(view.events)
    gated = _gated(view.events)
    return [
        Written(
            event=e,
            repeats_open_ticket=e.seq in duplicates,
            in_repeated_wake=e.wake in repeated,
            gated=e.seq in gated,
        )
        for e in view.events
        if e.actor is Actor.AGENT
        and e.operation in VISIBLE
        and e.entity.kind not in _NOT_WRITES
        and not unchanged(e, by_seq)
    ]


def _duplicates(events: list[WorldEvent]) -> set[int]:
    live: dict[tuple[str, str | None, str], WorldEvent] = {}
    found: set[int] = set()
    for event in events:
        after = event.after
        if event.operation is Operation.DELETE or (
            isinstance(after, TicketSnapshot) and after.state is not TicketState.OPEN
        ):
            live = {k: v for k, v in live.items() if v.entity != event.entity}
            continue
        if event.actor is not Actor.AGENT or event.operation is not Operation.CREATE:
            continue
        if not isinstance(after, TicketSnapshot):
            continue
        key = (event.entity.provider, after.project, normalised(after.title))
        if key in live:
            found.add(event.seq)
        else:
            live[key] = event
    return found


def _gated(events: list[WorldEvent]) -> set[int]:
    """The agent's first write carrying each item's gated operation, when the item as it stood then forbade it."""
    found: set[int] = set()
    for opening in events:
        item = opening.after
        if not (
            opening.actor is Actor.AGENT
            and opening.operation is Operation.CREATE
            and isinstance(item, InboxItemSnapshot)
            and item.gates
        ):
            continue
        act = next(
            (
                e
                for e in events
                if e.actor is Actor.AGENT
                and e.operation in VISIBLE
                and e.entity != opening.entity
                and carries(e, item.gates)
            ),
            None,
        )
        if act is None:
            continue
        stood = next((e.after for e in reversed(events) if e.seq < act.seq and e.entity == opening.entity), item)
        if isinstance(stood, InboxItemSnapshot) and (
            stood.status in (ItemStatus.PENDING, ItemStatus.WITHDRAWN) or stood.permits is False
        ):
            found.add(act.seq)
    return found


def gates_declared(view: RunView) -> bool:
    """Whether the run can say what went ahead unapproved: no item was asked of anyone, or one says what it holds
    back (`pending.gates` of its inbox)."""
    items = [
        e.after
        for e in view.events
        if e.actor is Actor.AGENT and e.operation is Operation.CREATE and isinstance(e.after, InboxItemSnapshot)
    ]
    return not items or any(isinstance(i, InboxItemSnapshot) and i.gates for i in items)


def repeated_wakes(view: RunView) -> set[int]:
    """The wakes that were a second delivery of one of the agent's own (`DispatchFault.TWICE`)."""
    if view.dues is None:
        return set()
    found: set[int] = set()
    for again in view.dues:
        if again.fault is not DispatchFault.TWICE or again.asked_for is None or again.closed is not DueClosed.FIRED:
            continue
        found.update(
            w.index
            for w in view.wakes
            if again.closed_wake is not None and w.index == again.closed_wake + 1 and w.sim_time == again.closed_at
        )
    return found


def planned_wakes(view: RunView, ended: datetime) -> list[Fact]:
    """Each wake the agent asked for itself, at the moment it was due, when that moment came in the run: a second
    or late delivery is the scenario's, not the agent's plan."""
    if view.dues is None:
        return []
    return sorted(
        (
            Fact(at=d.due.at)
            for d in view.dues
            if d.source in AGENT_SOURCES
            and d.asked_for is None
            and d.due.at <= ended
            and (d.closed in REACHED or d.closed is None)
        ),
        key=lambda f: f.at,
    )


class Reported(Model):
    """One commitment as the agent reported it when a wake ended."""

    at: AwareDatetime
    wake: int
    status: CommitmentStatus
    person_email: str | None
    entity: EntityRef | None


def reported(view: RunView) -> list[Reported]:
    """Every commitment the agent reported, once per wake it was reported in."""
    return [
        Reported(at=r.at, wake=r.wake, status=c.status, person_email=c.person_email, entity=c.entity)
        for r in view.reported or []
        for c in r.commitments
    ]


def blocked(view: RunView, needs: frozenset[Needs], check: str) -> list[str]:
    """What a check needs and the view does not carry. Non-empty means the check did not run."""
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
    """A stretch of simulated time in days and hours, the way findings say it."""
    hours = round(abs(delta).total_seconds() / 3600)
    days, hours = divmod(hours, 24)
    parts = [f"{days} day{'s' if days != 1 else ''}"] if days else []
    if hours or not days:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    return " ".join(parts)
