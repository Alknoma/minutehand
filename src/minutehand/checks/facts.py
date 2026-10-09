"""The facts of a run, read from its view: what assessments count, and what an agent's own Python check may read.

Nothing here judges. Each function answers what happened and when: the asks and what followed them, the messages
the agent sent, every change it made, its wakes, the wakes it planned, and what it reported. The YAML rules of
`domain/assessments.py` count these, and a team's own check (`AgentUnderTest.checks`) may import them as they are.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from pydantic import AwareDatetime, Field

from minutehand.checks.ledger import Away, absences, recipients
from minutehand.domain.agent import CommitmentStatus
from minutehand.domain.checks import Needs, Obligation, ObligationKind, RunView
from minutehand.domain.clock import AGENT_SOURCES, REACHED, DueClosed
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import DispatchFault, Model, TicketState
from minutehand.domain.transitions import Transition, moves
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    MessageSnapshot,
    Operation,
    RecordedCall,
    Snapshot,
    TicketSnapshot,
    WorldEvent,
)

VISIBLE = frozenset({Operation.CREATE, Operation.UPDATE, Operation.DELETE})
"""What a person can see the agent did: a read or a search tells them nothing."""


class Fact(Model):
    """One thing that happened, and when: what a rule counts."""

    at: AwareDatetime
    seqs: list[int] = Field(default=[], description="WorldEvent.seq of what shows it; empty for a wake or a plan")
    calls: list[int] = Field(default=[], description="The agent's calls that show it, by their place among the calls")


class Ask(Model):
    """A wait the agent opened on a person: a question they can answer, an item in its product, or work handed to
    them; and what the agent did about it."""

    obligation: Obligation
    follow_ups: list[Fact] = Field(description="The agent's writes the person could see while it was open, in order")
    touches: list[Fact] = Field(description="Every write of the agent's on the person, the thread or the ticket")
    answer: str | None = Field(
        default=None,
        description="What the person answered: their words; for a decision, its inputs as given, else its name",
    )
    answer_facts: list[str] = Field(
        default=[],
        description="What the answer carried: a decision's inputs as given, or the facts its script step carried, "
        "which a model put in the person's words",
    )

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
    invisible = unchanged(view.events)
    # What can touch a wait, gathered once: the agent's visible writes, its messages apart and the rest by entity.
    acts = [e for e in view.events if e.actor is Actor.AGENT and e.operation in VISIBLE and e.seq not in invisible]
    said = [e for e in acts if isinstance(e.after, MessageSnapshot)]
    by_entity: dict[tuple[str, str, str], list[WorldEvent]] = {}
    for e in acts:
        by_entity.setdefault((e.entity.provider, e.entity.kind, e.entity.external_id), []).append(e)
    found: list[Ask] = []
    for o in view.obligations:
        if o.kind is not kind:
            continue
        follow_ups = [
            Fact(at=by_seq[s].sim_time, seqs=[s])
            for s in o.agent_touches
            if s in by_seq
            and by_seq[s].operation in VISIBLE
            and s not in invisible
            and (o.settled_at is None or by_seq[s].sim_time < o.settled_at)
        ]
        found.append(
            Ask(
                obligation=o,
                follow_ups=sorted(follow_ups, key=lambda f: f.at),
                touches=_touches(view, o, _candidates(o, said, by_entity, by_seq, invisible)),
                answer=_answer(view, o),
                answer_facts=_answer_facts(view, o),
            )
        )
    return found


def _candidates(
    o: Obligation,
    said: list[WorldEvent],
    by_entity: dict[tuple[str, str, str], list[WorldEvent]],
    by_seq: dict[int, WorldEvent],
    invisible: frozenset[int],
) -> list[WorldEvent]:
    """The agent's visible writes that could touch the wait `o`, in log order: its messages, its writes to the
    wait's entity, and those the ledger counted."""
    seqs = {e.seq for e in said}
    if o.entity is not None:
        seqs.update(e.seq for e in by_entity.get((o.entity.provider, o.entity.kind, o.entity.external_id), []))
    named = [*o.agent_touches, *([o.first_touch_after_settled] if o.first_touch_after_settled is not None else [])]
    seqs.update(s for s in named if s in by_seq and by_seq[s].operation in VISIBLE and s not in invisible)
    return [by_seq[s] for s in sorted(seqs) if by_seq[s].actor is Actor.AGENT]


def _touches(view: RunView, o: Obligation, candidates: list[WorldEvent]) -> list[Fact]:
    person = next((p for p in view.scenario.people if p.key == o.person), None)
    emails = {person.email} if person is not None else set()
    touched: list[Fact] = []
    for event in candidates:
        if event.seq <= o.opened_by:
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
    """What the person answered: their words; for a decision, what it carries (its inputs as given), else its name."""
    said = _said(view, o)
    if said is None:
        return None
    if said.decides is not None:
        given = [v for v in said.decides.inputs.values() if v.strip()]
        return "; ".join(given) if given else said.decides.decision
    return said.text


def _answer_facts(view: RunView, o: Obligation) -> list[str]:
    """What the answer carried: a decision's inputs as given, else the facts its words were written from."""
    said = _said(view, o)
    if said is None:
        return []
    if said.decides is not None:
        return [v for v in said.decides.inputs.values() if v.strip()]
    return list(said.facts)


def _said(view: RunView, o: Obligation) -> PersonReply | None:
    if o.settled_at is None or o.person is None:
        return None
    said = [r for r in view.replies if r.person == o.person and r.at == o.settled_at]
    return said[0] if said else None


def unchanged(events: list[WorldEvent]) -> frozenset[int]:
    """The seqs of updates that leave their entity as it was: edits nobody can see. One pass over the log."""
    last: dict[tuple[str, str, str], Snapshot] = {}
    found: set[int] = set()
    for event in sorted(events, key=lambda e: e.seq):
        key = (event.entity.provider, event.entity.kind, event.entity.external_id)
        if event.operation is Operation.UPDATE and key in last and last[key] == event.after:
            found.add(event.seq)
        if event.after is not None:
            last[key] = event.after
    return frozenset(found)


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
        out = [k for k in to if any(a.person == k and a.away_when_written_to(event.sim_time) for a in away)]
        sent.append(Sent(event=event, to=to, to_away=out))
    return sent


class Written(Model):
    """A change the agent made to the world, with what is known of it."""

    event: WorldEvent
    repeats_open_ticket: bool = Field(
        default=False, description="A ticket created with the title of one still open in the same project"
    )
    in_repeated_wake: bool = Field(default=False, description="Written in the second delivery of one wake")


_NOT_WRITES = frozenset(
    {EntityKind.DUE, EntityKind.CHANNEL, EntityKind.MEMORY, EntityKind.NEXT_WAKE, EntityKind.TRANSITION}
)
"""Not counted as writes: the run loop's own table, the agent's own memory and plan, and a transition, which is
recorded beside the write that made it (the ticket moved, the answer set) and counted as `transitions`."""
_WORD = re.compile(r"\w+")


def normalised(title: str) -> str:
    """Case, punctuation and spacing removed: what two titles are, once nobody's typing is counted."""
    return " ".join(_WORD.findall(title.casefold()))


def writes(view: RunView) -> list[Written]:
    """Every change the agent made that a person could see, in order."""
    invisible = unchanged(view.events)
    repeated = repeated_wakes(view)
    duplicates = _duplicates(view.events)
    return [
        Written(
            event=e,
            repeats_open_ticket=e.seq in duplicates,
            in_repeated_wake=e.wake in repeated,
        )
        for e in view.events
        if e.actor is Actor.AGENT
        and e.operation in VISIBLE
        and e.entity.kind not in _NOT_WRITES
        and e.seq not in invisible
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


class Moved(Model):
    """One transition of an item's state (`domain.transitions.Transition`), with the states its item had been in
    before it."""

    event: WorldEvent
    transition: Transition
    reached: list[str] = Field(
        description="Every state the item was in before this transition: earlier transitions' states, and its own "
        "`from_state`"
    )

    @property
    def at(self) -> datetime:
        return self.event.sim_time


def transitions(view: RunView) -> list[Moved]:
    """Every move in the run, by anyone, in order (`domain.transitions.moves`): each recorded transition, and each
    other write to an item in the world as the move it made."""
    by_seq = {e.seq: e for e in view.events}
    been: dict[tuple[str, str, str], list[str]] = {}
    found: list[Moved] = []
    for moved in moves(view.events):
        if moved.seq is None:
            continue
        event = by_seq[moved.seq]
        key = (moved.item.provider, moved.item.kind.value, moved.item.external_id)
        states = been.setdefault(key, [])
        if moved.from_state is not None and moved.from_state not in states:
            states.append(moved.from_state)
        found.append(Moved(event=event, transition=moved, reached=list(states)))
        if moved.to_state not in states:
            states.append(moved.to_state)
    return found


class Called(Model):
    """One of the agent's own calls, with its place among the run's calls and whether its answer was new."""

    call: RecordedCall
    position: int = Field(ge=1, description="Its place among every call the run recorded (calls.call_id)")
    answer_changed: bool = Field(
        description="Its answer differed from the answer to the agent's previous call of the same method and path, "
        "or there was none"
    )

    @property
    def at(self) -> datetime:
        return self.call.sim_time

    @property
    def route(self) -> str:
        return self.call.exchange.path.split("?", 1)[0]


def calls(view: RunView) -> list[Called]:
    """Every call the agent itself made, in order: not Minutehand's as a person, not a tunnel relayed unopened."""
    found: list[Called] = []
    last: dict[tuple[str, str, str], str] = {}
    for position, call in enumerate(view.calls or [], start=1):
        x = call.exchange
        if x.inbox_call is not None or x.tunnelled is not None:
            continue
        key = (x.method, x.host, x.path)
        answer = body_content(x.response_body if x.response_body is not None else repr(x.response_bytes))
        changed = key not in last or last[key] != answer
        last[key] = answer
        found.append(Called(call=call, position=position, answer_changed=changed))
    return found


def body_content(body: str) -> str:
    """A body as what it says: JSON with its keys in order and its spacing gone, else its text stripped."""
    try:
        return json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return body.strip()


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
