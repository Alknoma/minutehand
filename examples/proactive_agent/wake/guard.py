"""The rules every wake keeps, whatever the decider proposed:

- **something open is never left without a wake**: no proposal, or one that names none, falls back to the earliest
  moment anything open is due;
- **nothing open, no wake**: the agent is not woken to do nothing;
- **never before an answer can be due**: a person is not chased before the time they were given has passed;
- **never in the past**, and **always in working hours**: a moment out of hours moves to the next opening."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/London")
OPENS, CLOSES = time(9, 0), time(17, 30)
SOONEST = timedelta(minutes=1)


def in_hours(moment: datetime) -> datetime:
    """The moment, or the next working-day opening after it."""
    local = moment.astimezone(ZONE)
    day = local.replace(hour=OPENS.hour, minute=OPENS.minute, second=0, microsecond=0)
    if local.weekday() < 5 and day <= local < local.replace(hour=CLOSES.hour, minute=CLOSES.minute):
        return moment
    if local >= day:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.astimezone(moment.tzinfo)


def guarded(proposed: datetime | None, due: Sequence[datetime], now: datetime) -> datetime | None:
    """The proposal, held to the rules; `due` is when each open thing may next be acted on."""
    if not due:
        return None
    earliest = min(due)
    moment = proposed if proposed is not None else earliest
    moment = max(moment, earliest, now + SOONEST)
    return in_hours(moment)
