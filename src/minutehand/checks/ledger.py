"""The obligations ledger: what the world is waiting on, derived from the world.

Nothing here reads what the agent says about itself. An ask is a message the
agent sent; an answer is a reply the people stored; a hand-off is a ticket the
agent filed or reassigned; its end is a later event by the person who holds it.

A message sent where nobody can answer it (`MessageSnapshot.answerable` False: an email through a captured
host, since people reply only through providers that push events) opens no wait: it tells, it does not ask,
whoever it went to. It still counts as a touch on a wait already open with that person, which it can see.

Whether a message asked anything is the replier's decision, never the ledger's:
a message opens a wait when the person has a reply decided to it, or when the
person is `Silent`, whose every message is a question left unanswered. A reply
withdrawn before it landed (the agent edited the message it answered) still says
the message asked something, and settles nothing: it never reached anyone. A message
a person would not answer (a thank-you, a report) asked them nothing, whether or
not they are away when it arrives.

A message is the same ask as an earlier one, and so a follow-up on it rather
than a wait of its own, when it goes to the same person in the same conversation
(provider and channel, which a thread shares) while the earlier wait is still
open. Nothing is read from the text, so this folds a second, different question
asked in the same conversation before the first was answered into the first,
and treats a reminder sent in another channel as a new ask.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta

from pydantic import AwareDatetime

from minutehand.domain.absence import first_ask, placed
from minutehand.domain.checks import Obligation, ObligationKind
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import AbsenceTrigger, DelayRange, Model, Person, Scenario, Silent, TicketState
from minutehand.domain.world import Actor, EntityKind, EntityRef, MessageSnapshot, Operation, TicketSnapshot, WorldEvent

FINISHED = frozenset({TicketState.DONE, TicketState.CANCELLED})


class Away(Model):
    """One stretch a person is away, placed on the run's clock."""

    person: str
    starts: AwareDatetime
    ends: AwareDatetime
    delegate: str | None

    def covers(self, moment: datetime) -> bool:
        return self.starts <= moment < self.ends


def recipients(event: WorldEvent, scenario: Scenario) -> list[Person]:
    """The scenario's people an agent message was addressed to, in scenario order."""
    if not isinstance(event.after, MessageSnapshot):
        return []
    emails = set(event.after.recipient_emails)
    return [p for p in scenario.people if p.email in emails]


def absences(scenario: Scenario, events: list[WorldEvent]) -> list[Away]:
    """Every absence in the scenario, anchored: at the start, or at the agent's first message to that person."""
    stretches: list[Away] = []
    for person in scenario.people:
        asked = first_ask(person.email, events)
        for absence in person.absences:
            found = placed(
                from_start=scenario.starts_at if absence.trigger is AbsenceTrigger.AT_START else None,
                asked=asked,
                starts_after=absence.starts_after,
                lasts=absence.lasts,
            )
            if found is not None:
                stretches.append(Away(person=person.key, starts=found[0], ends=found[1], delegate=absence.delegate))
    return stretches


def _ref(entity: EntityRef) -> tuple[str, EntityKind, str]:
    return entity.provider, entity.kind, entity.external_id


def _longest(person: Person) -> DelayRange:
    if isinstance(person.reply, Silent):
        return DelayRange()
    return person.reply.delay


class _Open(Model):
    """An obligation as the ledger first sees it, before its touches are counted."""

    key: str
    kind: ObligationKind
    person: Person
    entities: list[EntityRef]
    opened_at: AwareDatetime
    opened_by: int
    expected_by: AwareDatetime | None
    patience: timedelta | None = None
    settled_at: AwareDatetime | None
    primary: EntityRef
    conversation: tuple[str, str] | None = None

    def open_at(self, moment: datetime) -> bool:
        return self.settled_at is None or moment < self.settled_at


def build(
    scenario: Scenario,
    events: list[WorldEvent],
    replies: list[PersonReply],
    *,
    withdrawn: Collection[int] = (),
) -> list[Obligation]:
    """Every obligation the run opened, in the order it opened; the scenario deadline last. `withdrawn` are the
    positions in `replies` of replies withdrawn before they landed."""
    events = sorted(events, key=lambda e: e.seq)
    head = events[-1].sim_time if events else scenario.starts_at
    by_key = {p.key: p for p in scenario.people}
    by_email = {p.email: p for p in scenario.people}
    away = absences(scenario, events)
    asked = {(_ref(r.in_reply_to), r.person) for r in replies}
    answered = {(_ref(r.in_reply_to), r.person): r for i, r in enumerate(replies) if i not in withdrawn}
    decided = {(_ref(r.in_reply_to), r.person): r for r in replies}
    opened: list[_Open] = []
    holder: dict[tuple[str, EntityKind, str], str | None] = {}

    for event in events:
        after = event.after
        if event.actor is Actor.AGENT and event.operation is Operation.CREATE and isinstance(after, MessageSnapshot):
            channel = EntityRef(provider=event.entity.provider, kind=EntityKind.CHANNEL, external_id=after.channel)
            conversation = (event.entity.provider, after.channel)
            for person in recipients(event, scenario):
                reply = answered.get((_ref(event.entity), person.key))
                settled = reply.at if reply is not None and reply.at <= head else None
                earlier = next(
                    (
                        o
                        for o in opened
                        if o.kind is ObligationKind.ANSWER_FROM_PERSON
                        and o.person.key == person.key
                        and o.conversation == conversation
                        and o.open_at(event.sim_time)
                    ),
                    None,
                )
                if earlier is not None:
                    opened[opened.index(earlier)] = _joined(earlier, event.entity, settled)
                    continue
                if not after.answerable:
                    continue  # told, not asked: nobody can answer where it went
                if (_ref(event.entity), person.key) not in asked and not isinstance(person.reply, Silent):
                    continue
                opened.append(
                    _Open(
                        key=f"answer:{event.entity.provider}:{event.entity.external_id}:{person.key}",
                        kind=ObligationKind.ANSWER_FROM_PERSON,
                        person=person,
                        entities=[event.entity, channel],
                        opened_at=event.sim_time,
                        opened_by=event.seq,
                        expected_by=event.sim_time + _patience(person, decided.get((_ref(event.entity), person.key))),
                        patience=_patience(person, decided.get((_ref(event.entity), person.key))),
                        settled_at=settled,
                        primary=event.entity,
                        conversation=conversation,
                    )
                )
        if not isinstance(after, TicketSnapshot):
            continue
        previous = holder.get(_ref(event.entity))
        holder[_ref(event.entity)] = after.assignee_email
        if event.actor is Actor.PERSON and after.state in FINISHED:
            continue
        handed = event.actor is Actor.AGENT and (
            event.operation is Operation.CREATE
            or (event.operation is Operation.UPDATE and after.assignee_email != previous)
        )
        person = by_email.get(after.assignee_email) if after.assignee_email is not None else None
        if not handed or person is None or after.state in FINISHED:
            continue
        fate = next((f for f in scenario.ticket_fates if f.assignee == person.key), None)
        opened.append(
            _Open(
                key=f"work:{event.entity.provider}:{event.entity.external_id}:{person.key}:{event.seq}",
                kind=ObligationKind.WORK_WITH_PERSON,
                person=person,
                entities=[event.entity],
                opened_at=event.sim_time,
                opened_by=event.seq,
                expected_by=event.sim_time + fate.after if fate is not None else None,
                settled_at=_finished(event.entity, event.seq, events),
                primary=event.entity,
            )
        )

    ledger = [_finish(o, events, by_key, away) for o in opened]
    if scenario.deadline is not None:
        ledger.append(
            Obligation(
                key="deadline",
                kind=ObligationKind.DATE,
                opened_at=scenario.starts_at,
                opened_by=0,
                expected_by=scenario.deadline,
                settled_at=scenario.deadline if head >= scenario.deadline else None,
            )
        )
    return ledger


def _patience(person: Person, reply: PersonReply | None) -> timedelta:
    """How long the person may take: the delay their reply was decided under, when it says (a fork may have changed
    them since), else their delay in the scenario."""
    if reply is not None and reply.patience is not None:
        return reply.patience
    return _longest(person).longest


def _joined(wait: _Open, message: EntityRef, answered: datetime | None) -> _Open:
    """A follow-up on an open wait: the wait now also settles when this message is answered, if that is sooner."""
    landed = [t for t in (wait.settled_at, answered) if t is not None]
    return wait.model_copy(
        update={"entities": [*wait.entities, message], "settled_at": min(landed) if landed else None}
    )


def _finished(ticket: EntityRef, since: int, events: list[WorldEvent]) -> datetime | None:
    """When the person holding this ticket first finished or cancelled it after `since`."""
    for event in events:
        if (
            event.seq > since
            and event.entity == ticket
            and event.actor is Actor.PERSON
            and isinstance(event.after, TicketSnapshot)
            and event.after.state in FINISHED
        ):
            return event.sim_time
    return None


def _finish(o: _Open, events: list[WorldEvent], by_key: dict[str, Person], away: list[Away]) -> Obligation:
    emails = {o.person.email}
    emails.update(by_key[a.delegate].email for a in away if a.person == o.person.key and a.delegate)
    touches: list[int] = []
    after_settled: int | None = None
    for event in events:
        if event.actor is not Actor.AGENT or event.seq <= o.opened_by:
            continue
        on_entity = event.entity in o.entities
        on_person = (
            event.operation is Operation.CREATE
            and isinstance(event.after, MessageSnapshot)
            and bool(emails.intersection(event.after.recipient_emails))
        )
        if not (on_entity or on_person):
            continue
        if o.settled_at is None or event.sim_time < o.settled_at:
            touches.append(event.seq)
        elif after_settled is None:
            after_settled = event.seq
    return Obligation(
        key=o.key,
        kind=o.kind,
        person=o.person.key,
        entity=o.primary,
        opened_at=o.opened_at,
        opened_by=o.opened_by,
        expected_by=o.expected_by,
        patience=o.patience,
        settled_at=o.settled_at,
        agent_touches=touches,
        first_touch_after_settled=after_settled,
    )
