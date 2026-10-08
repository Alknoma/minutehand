"""When a person answers, whoever writes their words: every moment a reply or a decision lands, drawn from the run's
seed, and the asks it answers.

A moment is drawn from a seed made of the run's seed, the person and the ask it answers (the asked message or item's
identity), so the same seed gives the same moments on every run and every fork, and two people asked the same
message draw apart. Two ways to draw:

- `reply_within` (a person's, or a scripted step's or decision's own `within`): a span drawn uniformly between the
  window's `min` and `max`, counted in the person's AVAILABLE time after the ask: time inside their working hours
  and outside their absences. Replies spread over the hours a person works instead of piling up when they start.
- otherwise their reply's `delay`: a span drawn uniformly between its shortest and longest, in calendar time after
  the ask, then pushed past any absence and into their working hours.

A follow-up (a message to a person in a conversation where they already owe an answer) is not a new ask: the
answer owed is the answer to the ask it was drawn for, in that ask's thread. It changes nothing unless the person
declares `reminded`: then a moment is drawn again from `reminded.sooner_within`, after the follow-up, and the answer
moves there when that is sooner. Never later.

A person away while a delegate covers sends an automatic reply at once, saying so, once per absence: it is no answer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from minutehand.application.refusals import RunRefused
from minutehand.domain.clock import Drawn, DrawnFrom
from minutehand.domain.people import PersonReply, Press, Writing
from minutehand.domain.scenario import (
    Absence,
    AbsenceTrigger,
    DelayRange,
    Person,
    Scenario,
    ScriptedPress,
    Window,
    WorkingHours,
)
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    InboxItemSnapshot,
    MessageSnapshot,
    Operation,
    WorldEvent,
)

_MAX_PUSHES = 1000
"""Absences and working hours alternate at most this many times before a landing time is called unreachable."""

Owed = tuple[EntityRef, datetime]
"""An answer a person owes: the message it answers, and the moment it lands."""


def refuse_unworkable_hours(scenario: Scenario) -> None:
    """Working hours that close before they open leave no moment to answer in; refused before the run."""
    for person in scenario.people:
        hours = person.working_hours
        if hours is not None and hours.opens >= hours.closes:
            raise RunRefused(f"{person.key}: working hours open at {hours.opens} and close at {hours.closes}")


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


# -- the asks ----------------------------------------------------------------------------------------------------


def messages_to(person: Person, history: Sequence[WorldEvent]) -> list[WorldEvent]:
    """Every message the agent created to this person where they could answer it, in order: a captured send
    (an email through a declared host) is not one, so it does not move their script on."""
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


def same_conversation(earlier: MessageSnapshot, earlier_id: str, later: MessageSnapshot) -> bool:
    """Whether `later` goes on the conversation `earlier` opened: the same channel, and the same thread or in the
    thread of `earlier` itself. Two top-level messages of one direct conversation are one conversation."""
    if earlier.channel != later.channel:
        return False
    return later.thread_of == earlier.thread_of or later.thread_of == earlier_id


def follow_up_of(message: WorldEvent, sent: Sequence[WorldEvent], owed: Sequence[Owed]) -> WorldEvent | None:
    """The ask `message` follows up: an earlier message in the same conversation whose answer is owed and had not
    landed when `message` was sent. None: `message` is an ask of its own."""
    after = message.after
    assert isinstance(after, MessageSnapshot)
    landing = dict(owed)
    for e in reversed(sent):
        before = e.after
        if e.seq >= message.seq or not isinstance(before, MessageSnapshot):
            continue
        if e.entity.provider != message.entity.provider or not same_conversation(before, e.entity.external_id, after):
            continue
        if e.entity in landing and landing[e.entity] > message.sim_time:
            return e
    return None


def asks_of(person: Person, history: Sequence[WorldEvent], owed: Sequence[Owed]) -> list[WorldEvent]:
    """The person's asks, in order: every message the agent sent them that was not a follow-up on an answer they
    owed. The nth of these is what a script's step `to_ask: n` answers."""
    sent = messages_to(person, history)
    return [m for m in sent if follow_up_of(m, sent, owed) is None]


def items_of(person: Person, history: Sequence[WorldEvent], inbox: str | None) -> list[WorldEvent]:
    """Every item the agent's product put before this person, in order; in one inbox when it is named."""
    return [
        e
        for e in history
        if e.actor is Actor.AGENT
        and e.operation is Operation.CREATE
        and isinstance(e.after, InboxItemSnapshot)
        and e.after.person == person.key
        and (inbox is None or e.after.inbox == inbox)
    ]


def first_ask(person: Person, history: Sequence[WorldEvent], asked: WorldEvent) -> datetime:
    """When the agent first wrote to this person, up to `asked`: where an absence that starts on a first ask starts."""
    sent = messages_to(person, [e for e in history if e.seq <= asked.seq])
    return sent[0].sim_time if sent else asked.sim_time


# -- drawing -----------------------------------------------------------------------------------------------------


def _drawn_seconds(seed: int, person: Person, ref: EntityRef, salt: str, span: timedelta) -> int:
    """A whole number of seconds in [0, span], fixed by the seed, the person and the ask's identity."""
    said = f"{seed}|{person.key}|{ref.provider}|{ref.kind.value}|{ref.external_id}|{salt}"
    digest = hashlib.sha256(said.encode()).digest()
    return int.from_bytes(digest[:8], "big") % (int(span.total_seconds()) + 1)


def draw(
    scenario: Scenario,
    person: Person,
    asked: WorldEvent,
    history: Sequence[WorldEvent],
    *,
    within: Window | None,
    delay: DelayRange,
) -> Drawn:
    """The moment this person answers `asked`: within `within` of their available time when given (a step's, or
    their own `reply_within`), else their `delay` in calendar time pushed to when they are available."""
    window = within or person.reply_within
    anchor = first_ask(person, history, asked)
    if window is not None:
        offset = window.min + timedelta(
            seconds=_drawn_seconds(scenario.seed, person, asked.entity, "window", window.max - window.min)
        )
        lands = after_available(asked.sim_time, offset, person, scenario.starts_at, first_ask=anchor)
        return Drawn(
            source=DrawnFrom.WINDOW,
            seed=scenario.seed,
            asked_at=asked.sim_time,
            window=window,
            offset=offset,
            lands_at=lands,
        )
    offset = delay.shortest + timedelta(
        seconds=_drawn_seconds(scenario.seed, person, asked.entity, "delay", delay.longest - delay.shortest)
    )
    lands = available_at(asked.sim_time + offset, person, scenario.starts_at, first_ask=anchor)
    return Drawn(
        source=DrawnFrom.DELAY, seed=scenario.seed, asked_at=asked.sim_time, delay=delay, offset=offset, lands_at=lands
    )


def sooner(
    scenario: Scenario, person: Person, follow_up: WorldEvent, history: Sequence[WorldEvent], owed: datetime
) -> Drawn | None:
    """The moment drawn again when `follow_up` reminds this person of an answer they owe that lands at `owed`: from
    their `reminded.sooner_within`, after the follow-up, when that is sooner; None when they declare no reminder or
    the draw is not sooner."""
    if person.reminded is None:
        return None
    window = person.reminded.sooner_within
    offset = window.min + timedelta(
        seconds=_drawn_seconds(scenario.seed, person, follow_up.entity, "reminded", window.max - window.min)
    )
    anchor = first_ask(person, history, follow_up)
    lands = after_available(follow_up.sim_time, offset, person, scenario.starts_at, first_ask=anchor)
    if lands >= owed:
        return None
    return Drawn(
        source=DrawnFrom.REMINDED,
        seed=scenario.seed,
        asked_at=follow_up.sim_time,
        window=window,
        offset=offset,
        lands_at=lands,
    )


def pinned(scenario: Scenario, asked: WorldEvent, after: timedelta) -> Drawn:
    """A moment a fork pins: exactly `after` the ask, nothing drawn."""
    return Drawn(
        source=DrawnFrom.PINNED,
        seed=scenario.seed,
        asked_at=asked.sim_time,
        offset=after,
        lands_at=(asked.sim_time + after).astimezone(UTC),
    )


# -- availability --------------------------------------------------------------------------------------------------


def _windows(person: Person, starts_at: datetime, first_asked: datetime) -> list[tuple[datetime, datetime]]:
    return [_window(a, starts_at, first_asked) for a in person.absences]


def _window(absence: Absence, starts_at: datetime, first_asked: datetime) -> tuple[datetime, datetime]:
    anchor = starts_at if absence.trigger is AbsenceTrigger.AT_START else first_asked
    start = anchor + absence.starts_after
    return start, start + absence.lasts


def available_at(at: datetime, person: Person, starts_at: datetime, *, first_ask: datetime) -> datetime:
    """The first moment at or after `at` when this person is neither away nor outside their working hours."""
    windows = _windows(person, starts_at, first_ask)
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


def after_available(
    start: datetime, span: timedelta, person: Person, starts_at: datetime, *, first_ask: datetime
) -> datetime:
    """The moment `span` of this person's available time has passed after `start`: counted only while they are
    inside their working hours and outside their absences. A span of none is the first moment they are available."""
    windows = _windows(person, starts_at, first_ask)
    at = available_at(start, person, starts_at, first_ask=first_ask)
    left = span
    for _ in range(_MAX_PUSHES):
        end = _stretch_end(at, person, windows)
        if end is None or at + left < end:
            return (at + left).astimezone(UTC)
        left -= end - at
        at = available_at(end, person, starts_at, first_ask=first_ask)
    raise RunRefused(f"{person.key} has no {span} of available time after {start}: absences and hours leave too little")


def _stretch_end(at: datetime, person: Person, windows: list[tuple[datetime, datetime]]) -> datetime | None:
    """When the available stretch `at` is in ends: their working day closes, or an absence starts. None: never."""
    ends = [start for start, _ in windows if start > at]
    hours = person.working_hours
    if hours is not None:
        local = at.astimezone(ZoneInfo(hours.timezone))
        ends.append(datetime.combine(local.date(), hours.closes, tzinfo=local.tzinfo))
    return min(ends) if ends else None


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


# -- away with a delegate ------------------------------------------------------------------------------------------


def automatic_reply(
    scenario: Scenario,
    person: Person,
    asked: WorldEvent,
    history: Sequence[WorldEvent],
    replies: Sequence[PersonReply],
) -> PersonReply | None:
    """The automatic reply a person away while a delegate covers sends at once to a message that reaches them, once
    per absence: that they are away, until when, and whom to contact. None when they are not away then, nobody
    covers, or they already sent it in this absence."""
    if not isinstance(asked.after, MessageSnapshot):
        return None
    anchor = first_ask(person, history, asked)
    for absence in person.absences:
        if absence.delegate is None:
            continue
        start, end = _window(absence, scenario.starts_at, anchor)
        if not start <= asked.sim_time < end:
            continue
        if any(r.person == person.key and r.writing is Writing.AUTOMATIC and start <= r.at < end for r in replies):
            return None
        delegate = next(p for p in scenario.people if p.key == absence.delegate)
        why = f" ({absence.reason})" if absence.reason else ""
        until = end.astimezone(UTC)
        text = (
            f"Automatic reply: {person.name} is away{why} until {until:%A} {until.day} {until:%B %Y, %H:%M} UTC. "
            f"For anything urgent, please contact {delegate.name} ({delegate.email})."
        )
        return PersonReply(
            person=person.key,
            in_reply_to=asked.entity,
            text=text,
            at=asked.sim_time,
            writing=Writing.AUTOMATIC,
            drawn=Drawn(
                source=DrawnFrom.AUTOMATIC,
                seed=scenario.seed,
                asked_at=asked.sim_time,
                offset=timedelta(0),
                lands_at=asked.sim_time,
            ),
        )
    return None
