"""The replier for people whose replies are fixed text (`Scripted`) or who never answer (`Silent`), and when
any reply lands, whoever wrote it.

No model call: the reply to the nth ask is the scripted text for that ask. When it lands is `lands_at`, which
the model-written replier calls too: a delay drawn from the person's `DelayRange` by hashing the scenario's
seed with the asked message's id, so the same seed gives the same delays on every run and a fork. A delay
that lands inside one of the person's absences or outside their working hours is pushed to the next moment
they would answer.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from minutehand.application.refusals import RunRefused
from minutehand.domain.people import Decides, PersonReply, Press
from minutehand.domain.scenario import (
    Absence,
    AbsenceTrigger,
    Answers,
    DelayRange,
    Person,
    Scenario,
    Scripted,
    ScriptedPress,
    Silent,
    WorkingHours,
)
from minutehand.domain.world import Actor, EntityKind, InboxItemSnapshot, MessageSnapshot, Operation, WorldEvent
from minutehand.ports.clock import Clock

_MAX_PUSHES = 1000
"""Absences and working hours alternate at most this many times before a landing time is called unreachable."""


class ScriptedReplier:
    def __init__(self, scenario: Scenario) -> None:
        refuse_unworkable_hours(scenario)
        self._scenario = scenario

    async def decide(
        self, person: Person, asked: WorldEvent, history: list[WorldEvent], clock: Clock
    ) -> PersonReply | None:
        behaviour = person.reply
        if isinstance(behaviour, Silent):
            return None
        if isinstance(behaviour, Answers):
            raise RunRefused(f"{person.key}'s replies are written by a model; the scripted replier cannot write them")
        assert isinstance(behaviour, Scripted)
        if isinstance(asked.after, InboxItemSnapshot):
            return self._decides(person, behaviour, asked, history)
        asks = _asks_of(person, [e for e in history if e.seq <= asked.seq])
        scripted = next((r for r in behaviour.replies if r.to_ask == len(asks)), None)
        if scripted is None and behaviour.presses_every is not None:
            every = pressed(behaviour.presses_every, asked)
            if every is None:
                return None
            at = lands_at(self._scenario, person, asked, history, behaviour.delay)
            return PersonReply(person=person.key, in_reply_to=asked.entity, text=every.label, at=at, press=every)
        if scripted is None:
            return None
        press: Press | None = None
        if scripted.press is not None:
            press = pressed(scripted.press, asked)
            if press is None:
                return None
        return PersonReply(
            person=person.key,
            in_reply_to=asked.entity,
            text=scripted.said or (press.label if press is not None else ""),
            at=lands_at(self._scenario, person, asked, history, behaviour.delay),
            press=press,
            patience=behaviour.delay.longest,
        )

    def _decides(
        self, person: Person, behaviour: Scripted, asked: WorldEvent, history: list[WorldEvent]
    ) -> PersonReply | None:
        """The decision this person's script makes on the item: the one naming this item by its place among those
        waiting on them (in its inbox, when the script names one), else one for every item. None when the script
        has none, or names a decision the item does not allow: they cannot make what is not offered."""
        item = asked.after
        assert isinstance(item, InboxItemSnapshot)
        mine = [
            e
            for e in history
            if e.seq <= asked.seq
            and e.actor is Actor.AGENT
            and e.operation is Operation.CREATE
            and isinstance(e.after, InboxItemSnapshot)
            and e.after.person == person.key
        ]
        everywhere = len(mine)
        here = sum(1 for e in mine if isinstance(e.after, InboxItemSnapshot) and e.after.inbox == item.inbox)
        applies = [d for d in behaviour.decisions or [] if d.inbox is None or d.inbox == item.inbox]
        scripted = next(
            (
                d
                for d in applies
                if d.to_item is not None and d.to_item == (here if d.inbox is not None else everywhere)
            ),
            None,
        ) or next((d for d in applies if d.to_item is None), None)
        if scripted is None or scripted.decision not in item.decisions:
            return None
        return PersonReply(
            person=person.key,
            in_reply_to=asked.entity,
            text=decision_text(scripted.decision, scripted.inputs),
            at=lands_at(self._scenario, person, asked, history, behaviour.delay),
            decides=Decides(decision=scripted.decision, inputs=scripted.inputs),
            patience=behaviour.delay.longest,
        )


def decision_text(decision: str, inputs: dict[str, str]) -> str:
    """A decision as the reply table keeps its text: the decision, then what was given with it."""
    given = "; ".join(f"{name}: {value}" for name, value in inputs.items())
    return f"{decision} ({given})" if given else decision


def pressed(scripted: ScriptedPress, asked: WorldEvent) -> Press | None:
    """The control on the asked message whose label reads as the script says, in any case; None when the message
    carries no such control, and the person, who cannot press what is not there, does nothing."""
    if not isinstance(asked.after, MessageSnapshot):
        return None
    wanted = scripted.label.casefold()
    control = next((a for a in asked.after.actions if a.label.casefold() == wanted), None)
    if control is None:
        return None
    return Press(
        action_id=control.action_id, label=control.label, value=control.value, picks=scripted.picks, form=scripted.form
    )


def refuse_unworkable_hours(scenario: Scenario) -> None:
    """Working hours that close before they open leave no moment to answer in; refused before the run."""
    for person in scenario.people:
        hours = person.working_hours
        if hours is not None and hours.opens >= hours.closes:
            raise RunRefused(f"{person.key}: working hours open at {hours.opens} and close at {hours.closes}")


def lands_at(
    scenario: Scenario, person: Person, asked: WorldEvent, history: list[WorldEvent], delay: DelayRange
) -> datetime:
    """When this person's reply to `asked` lands, whoever wrote it: the seeded delay, then pushed past their
    absences and outside their working hours. An absence ON_FIRST_ASK starts from the first message `history`
    shows the agent sent them."""
    asks = _asks_of(person, [e for e in history if e.seq <= asked.seq])
    first_ask = asks[0].sim_time if asks else asked.sim_time
    drawn = asked.sim_time + delay_for(scenario.seed, asked, delay)
    return available_at(drawn, person, scenario.starts_at, first_ask=first_ask)


def _asks_of(person: Person, history: list[WorldEvent]) -> list[WorldEvent]:
    """Every message the agent created to this person where they could answer it, in order: a captured send
    (an email through a declared host) is not one of their asks, so it does not move their script on."""
    return [
        e
        for e in history
        if e.actor is Actor.AGENT
        and e.operation is Operation.CREATE
        and e.entity.kind is EntityKind.MESSAGE
        and isinstance(e.after, MessageSnapshot)
        and e.after.answerable
        and person.email in e.after.recipient_emails
    ]


def delay_for(seed: int, asked: WorldEvent, delay: DelayRange) -> timedelta:
    """A whole number of seconds in [shortest, longest], fixed by the seed and the asked message's identity."""
    ref = asked.entity
    digest = hashlib.sha256(f"{seed}|{ref.provider}|{ref.kind.value}|{ref.external_id}".encode()).digest()
    span = int((delay.longest - delay.shortest).total_seconds())
    return delay.shortest + timedelta(seconds=int.from_bytes(digest[:8], "big") % (span + 1))


def available_at(at: datetime, person: Person, starts_at: datetime, *, first_ask: datetime) -> datetime:
    """The first moment at or after `at` when this person is neither away nor outside their working hours."""
    windows = [_window(a, starts_at, first_ask) for a in person.absences]
    for _ in range(_MAX_PUSHES):
        moved = at
        for start, end in windows:
            if start <= moved < end:
                moved = end
        if person.working_hours is not None:
            moved = _within_hours(moved, person.working_hours)
        if moved == at:
            return at.astimezone(UTC)
        at = moved
    raise RunRefused(f"{person.key} is never available after {at}: absences and working hours leave no gap")


def _window(absence: Absence, starts_at: datetime, first_ask: datetime) -> tuple[datetime, datetime]:
    anchor = starts_at if absence.trigger is AbsenceTrigger.AT_START else first_ask
    start = anchor + absence.starts_after
    return start, start + absence.lasts


def _within_hours(at: datetime, hours: WorkingHours) -> datetime:
    local = at.astimezone(ZoneInfo(hours.timezone))
    day = local.date()
    for _ in range(8):
        opens = datetime.combine(day, hours.opens, tzinfo=local.tzinfo)
        closes = datetime.combine(day, hours.closes, tzinfo=local.tzinfo)
        working_day = not hours.weekdays_only or day.weekday() < 5
        if working_day and local < closes:
            return max(local, opens)
        day += timedelta(days=1)
        local = datetime.combine(day, hours.opens, tzinfo=local.tzinfo)
    raise AssertionError("a week always holds a working day")
