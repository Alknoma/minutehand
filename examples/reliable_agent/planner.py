"""The wake planner: the one place that decides when the agent wakes next. It reads only the agent's state and its
working hours; the model never sets a wake, so coming back is a property of the structure, not something a model
has to remember.

Every open wait carries the moment it is expected by, and the planner wakes the agent then: never before (no chasing
someone who could not yet have answered), never forgotten (no wait without a date). A request the service holds is
read again on a backoff that doubles while nothing changes, to four working hours at most: the service does not
say when it decides, so the agent looks often enough to act on a decision the same half day, and no oftener. Every moment is moved into working hours."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from moves import MOST_CHASES
from state import State
from work import WORK

ANSWER_TAKES = timedelta(days=1)  # how long a person is given to answer, in working time, before the agent looks again
FIRST_CHECK = timedelta(hours=1)  # the first look at a request the service holds
SAFETY_NET = timedelta(days=1)  # how often a service that pushes is read anyway
RETRY = timedelta(minutes=15)  # a wake whose model failed is tried again this much later
LONGEST_CHECK = timedelta(hours=4)  # and the longest: a decision it cannot be told of is seen within half a day


def in_hours(moment: datetime) -> datetime:
    """The moment, or the next opening of the working day after it."""
    zone = ZoneInfo(WORK.timezone)
    local = moment.astimezone(zone)
    opens = local.replace(hour=WORK.opens.hour, minute=WORK.opens.minute, second=0, microsecond=0)
    closes = local.replace(hour=WORK.closes.hour, minute=WORK.closes.minute, second=0, microsecond=0)
    if local.weekday() < 5 and opens <= local < closes:
        return moment
    day = opens if local < opens and local.weekday() < 5 else opens + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.astimezone(moment.tzinfo)


def expected_by(asked: datetime) -> datetime:
    """When an answer asked for at `asked` is due: a working day on."""
    due = asked + ANSWER_TAKES
    while due.astimezone(ZoneInfo(WORK.timezone)).weekday() >= 5:
        due += timedelta(days=1)
    return in_hours(due)


def next_check(read: datetime, unchanged_reads: int, *, pushes: bool = False) -> datetime:
    """When to read a held request again: an hour on, doubling while it does not change, to four hours at most. A
    service that tells the agent of each decision is read once a working day, as a safety net for a push that never
    came."""
    if pushes:
        return in_hours(read + SAFETY_NET)
    wait = min(FIRST_CHECK * (2**unchanged_reads), LONGEST_CHECK)
    return in_hours(read + wait)


def next_wake(state: State, now: datetime | None = None) -> datetime | None:
    """The earliest moment anything the agent holds open asks for it, after `now`; None when nothing does. A wait
    whose follow-ups are spent, and whose requester was told, asks for no wake: the person's answer, when it comes,
    wakes the agent. No moment at or before `now` is named, so the agent is never woken again for what it could not
    do in the wake it is in."""
    moments = [
        datetime.fromisoformat(w.expected_by)
        for about, w in state.waits.items()
        if not (w.chases >= MOST_CHASES and f"tell:requester:stuck:{about}" in state.sent)
    ]
    if state.request is not None and state.request.status in ("pending", "needs_info") and state.order is None:
        moments.append(datetime.fromisoformat(state.request.next_check))
    if state.retry_at is not None:
        moments.append(datetime.fromisoformat(state.retry_at))
    later = [m for m in moments if now is None or m > now]
    return min(later) if later else None
