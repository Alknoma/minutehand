"""Recurring events: a `daily` or `weekly` pattern over a range of `endDate`, `noEnd` or `numbered`, and the occurrences
they define (https://learn.microsoft.com/en-us/graph/api/resources/recurrencepattern and `recurrencerange`).

A weekly pattern repeats on `daysOfWeek` in every `interval`-th week, a week starting on `firstDayOfWeek` (sunday by
default); a daily one every `interval` days. The first occurrence may be the range's `startDate` or later, which must be
the date the event starts on. `endDate` is inclusive and `numbered` counts the occurrences from the first. The other four
pattern types are refused by name: this provider holds the two.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta

from minutehand.adapters.providers.microsoft import wire
from minutehand.domain.errors import NotServed

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SCAN = 20000
"""The most occurrences one call looks through; a window further out than that is refused by name."""


def kept(sent: wire.SentRecurrence, starts: datetime) -> wire.Recurrence:
    """The recurrence as Graph keeps it: day names in lower case, the defaults the page documents filled in. Refuses
    by name what is not a daily or weekly pattern over a range that starts when the event does."""
    pattern, span = sent.pattern, sent.range
    for name in (*(pattern.model_extra or {}), *(span.model_extra or {})):
        raise NotServed(
            f"the recurrence property {name!r}: only type, interval, daysOfWeek and firstDayOfWeek, and a range, are held"
        )
    if pattern.type not in ("daily", "weekly"):
        raise NotServed(f"a {pattern.type!r} recurrence: only daily and weekly are held")
    if pattern.interval < 1:
        raise NotServed("a recurrence whose interval is not positive: Graph's answer is not documented")
    days = [d.lower() for d in pattern.daysOfWeek]
    first = (pattern.firstDayOfWeek or "sunday").lower()
    if pattern.type == "weekly" and (not days or any(d not in DAYS for d in days) or first not in DAYS):
        raise NotServed(
            "a weekly recurrence without daysOfWeek, or naming a day that is none: Graph's answer is not documented"
        )
    if pattern.type == "daily" and (days or pattern.firstDayOfWeek is not None):
        raise NotServed(
            "a daily recurrence naming daysOfWeek or firstDayOfWeek: the page requires type and interval only"
        )
    if span.type not in ("endDate", "noEnd", "numbered"):
        raise NotServed(f"a recurrence range of the type {span.type!r}: Graph names endDate, noEnd and numbered")
    if span.recurrenceTimeZone is not None and span.recurrenceTimeZone.upper() not in ("UTC", "ETC/UTC"):
        raise NotServed(f"a recurrence in the time zone {span.recurrenceTimeZone!r}: only UTC is held")
    try:
        begins = date.fromisoformat(span.startDate)
        ends = date.fromisoformat(span.endDate) if span.endDate is not None else None
    except ValueError as e:
        raise NotServed("a recurrence range date that is not yyyy-mm-dd: Graph's answer is not documented") from e
    if begins != starts.date():
        raise NotServed(
            "a recurrence whose startDate is not the date the event starts on: the page requires them equal"
        )
    if span.type == "endDate" and (ends is None or ends < begins):
        raise NotServed(
            "an endDate range with no endDate, or one before its startDate: Graph's answer is not documented"
        )
    if span.type == "numbered" and (span.numberOfOccurrences is None or span.numberOfOccurrences < 1):
        raise NotServed("a numbered range without a positive numberOfOccurrences: Graph's answer is not documented")
    if span.type != "endDate" and ends is not None:
        raise NotServed(f"an endDate on a range of the type {span.type!r}: Graph's answer is not documented")
    if span.type != "numbered" and span.numberOfOccurrences is not None:
        raise NotServed(f"numberOfOccurrences on a range of the type {span.type!r}: Graph's answer is not documented")
    return wire.Recurrence(
        pattern=wire.RecurrencePattern(
            type=pattern.type,  # type: ignore[arg-type]
            interval=pattern.interval,
            daysOfWeek=days,
            firstDayOfWeek=first,
        ),
        range=wire.RecurrenceRange(
            type=span.type,  # type: ignore[arg-type]
            startDate=span.startDate,
            endDate=span.endDate,
            numberOfOccurrences=span.numberOfOccurrences or 0,
        ),
    )


def dates(recurrence: wire.Recurrence) -> Iterator[date]:
    """The days the event occurs on, in order, from the first."""
    pattern, span = recurrence.pattern, recurrence.range
    begins = date.fromisoformat(span.startDate)
    last = date.fromisoformat(span.endDate) if span.type == "endDate" and span.endDate is not None else None
    for count, day in enumerate(_days(pattern, begins)):
        if (last is not None and day > last) or (span.type == "numbered" and count >= span.numberOfOccurrences):
            return
        yield day


def _days(pattern: wire.RecurrencePattern, begins: date) -> Iterator[date]:
    if pattern.type == "daily":
        step = 0
        while True:
            yield begins + timedelta(days=step * pattern.interval)
            step += 1
    first = DAYS.index(pattern.firstDayOfWeek)
    offsets = sorted((DAYS.index(d) - first) % 7 for d in set(pattern.daysOfWeek))
    week = begins - timedelta(days=(begins.weekday() - first) % 7)
    while True:
        for offset in offsets:
            day = week + timedelta(days=offset)
            if day >= begins:
                yield day
        week += timedelta(weeks=pattern.interval)


def between(
    recurrence: wire.Recurrence, starts: datetime, ends: datetime, window_start: datetime, window_end: datetime
) -> list[tuple[int, datetime, datetime]]:
    """The occurrences of an event that starts at `starts` and ends at `ends` that overlap the window, each with
    its position in the series."""
    at, length = starts.astimezone(UTC).timetz(), ends - starts
    found: list[tuple[int, datetime, datetime]] = []
    for position, day in enumerate(dates(recurrence)):
        if position >= SCAN:
            raise NotServed(f"a window more than {SCAN} occurrences into a series: Graph's answer is not documented")
        began = datetime.combine(day, time(at.hour, at.minute, at.second, at.microsecond), tzinfo=UTC)
        if began >= window_end:
            break
        if began + length > window_start:
            found.append((position, began, began + length))
    return found


def occurrence(
    recurrence: wire.Recurrence, starts: datetime, ends: datetime, position: int
) -> tuple[datetime, datetime] | None:
    """The `position`-th occurrence (from 0), or None when the series ends before it."""
    at, length = starts.astimezone(UTC).timetz(), ends - starts
    for index, day in enumerate(dates(recurrence)):
        if index == position:
            began = datetime.combine(day, time(at.hour, at.minute, at.second, at.microsecond), tzinfo=UTC)
            return began, began + length
        if index >= SCAN:
            break
    return None
