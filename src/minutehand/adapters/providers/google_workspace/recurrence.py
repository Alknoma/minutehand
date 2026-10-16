"""A recurring Calendar event's instances: its `recurrence` read as RFC 5545 and expanded in its start's time zone.

`recurrence` is a "List of RRULE, EXRULE, RDATE and EXDATE lines for a recurring event, as specified in RFC5545"
(the events reference). This fake serves one RRULE line; the other three, a second RRULE, and the DTSTART and DTEND
lines the reference does not allow are refused by name, as is a rule RFC 5545 cannot read, since Google's answer to
each is not recorded. The rule is expanded from the event's start, "the start time of the first instance", in its
`start.timeZone`, "the time zone in which the recurrence is expanded", so an instance keeps its wall-clock time
across a change of offset. Each instance lasts as the first does.

An instance's id is the recurring event's, `_`, and its original start: in UTC as `YYYYMMDDTHHMMSSZ` for a timed
event, as `YYYYMMDD` for an all-day one, as recorded answers of the service show
(`CLAIMS.md`, Calendar v3).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, rrulestr

from minutehand.adapters.providers.google_workspace import calendar_wire as cal
from minutehand.adapters.providers.google_workspace import gmail_wire as mail
from minutehand.adapters.providers.google_workspace import wire

MOST_INSTANCES = 10_000
"""How many instances one expansion walks before it stops refusing to guess: Google documents no limit."""


def not_served(what: str) -> wire.Refusal:
    return mail.not_implemented(f"minutehand's Google Calendar does not serve {what}: Google's answer is not recorded")


def rule_line(lines: list[str]) -> str:
    """The one RRULE line of a `recurrence` this fake serves, checked; any other line is refused by name."""
    rules: list[str] = []
    for line in lines:
        name = line.split(":", 1)[0].split(";", 1)[0].strip().upper()
        if name in ("DTSTART", "DTEND"):
            raise not_served(f"a {name} line in recurrence, which the events reference does not allow")
        if name != "RRULE":
            raise not_served(f"the recurrence line {name}")
        rules.append(line.strip())
    if len(rules) != 1:
        raise not_served(f"recurrence with {len(rules)} RRULE lines")
    return rules[0]


def bounded(line: str) -> bool:
    """Whether the rule ends: it names a COUNT or an UNTIL."""
    parts = line.split(":", 1)[-1].split(";")
    return any(p.split("=", 1)[0].strip().upper() in ("COUNT", "UNTIL") for p in parts)


def _parsed(line: str, first: datetime) -> rrule:
    try:
        found = rrulestr(line, dtstart=first)
    except (ValueError, TypeError) as error:
        raise not_served(f"the recurrence rule {line!r} ({error})") from error
    if not isinstance(found, rrule):
        raise not_served(f"the recurrence rule {line!r}")
    return found


@dataclass(frozen=True)
class Instance:
    """One occurrence: when it starts and ends, as aware datetimes, and its id's suffix."""

    start: datetime
    end: datetime
    suffix: str


class Series:
    """A recurring event, ready to expand: its rule, its first instance, and the zone it is expanded in."""

    def __init__(self, event: cal.StoredEvent) -> None:
        if not event.recurrence:
            raise ValueError(f"event {event.id} does not recur")
        if not event.start.timeZone:
            raise ValueError(f"recurring event {event.id} has no start.timeZone, which its write requires")
        self.line = rule_line(event.recurrence)
        self.all_day = event.start.date is not None
        self._zone = ZoneInfo(event.start.timeZone)
        if self.all_day:
            assert event.start.date is not None and event.end.date is not None
            first = date.fromisoformat(event.start.date)
            self._first = datetime(first.year, first.month, first.day)
            self._length = date.fromisoformat(event.end.date) - first
        else:
            start = cal.instant(event.start, event.start.timeZone, "start").astimezone(self._zone)
            end = cal.instant(event.end, event.end.timeZone or event.start.timeZone, "end").astimezone(self._zone)
            self._first = start
            self._length = end.replace(tzinfo=None) - start.replace(tzinfo=None)
        self._rule = _parsed(self.line, self._first)
        self.bounded = bounded(self.line)

    def _instance(self, starts: datetime) -> Instance:
        if self.all_day:
            day = starts.date()
            start = datetime(day.year, day.month, day.day, tzinfo=self._zone)
            end_day = day + self._length
            end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=self._zone)
            return Instance(start=start, end=end, suffix=day.strftime("%Y%m%d"))
        end_wall = starts.replace(tzinfo=None) + self._length
        end = end_wall.replace(tzinfo=self._zone)
        return Instance(start=starts, end=end, suffix=starts.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))

    def walk(self, high: datetime | None) -> Iterator[Instance]:
        """Every instance starting before `high`, first first; with no `high`, every one of a rule that ends."""
        if high is None and not self.bounded:
            raise not_served("expanding a recurring event whose rule never ends without a timeMax")
        for walked, starts in enumerate(self._rule):
            if walked >= MOST_INSTANCES:
                raise not_served(f"a recurring event of more than {MOST_INSTANCES} instances")
            found = self._instance(starts)
            if high is not None and found.start >= high:
                return
            yield found

    def between(self, low: datetime | None, high: datetime | None) -> list[Instance]:
        """The instances that meet [low, high): an instance's end bounded by `low`, its start by `high`."""
        return [i for i in self.walk(high) if cal.overlaps(i.start, i.end, low, high)]

    def at(self, suffix: str) -> Instance | None:
        """The instance an id's suffix names, None when the rule has none there."""
        if self.all_day:
            try:
                day = datetime.strptime(suffix, "%Y%m%d")
            except ValueError:
                return None
            start = day.replace(tzinfo=self._zone)
        else:
            try:
                start = datetime.strptime(suffix, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            except ValueError:
                return None
        for found in self.walk(start + timedelta(seconds=1)):
            if found.suffix == suffix:
                return found
        return None

    def span(self) -> tuple[datetime, datetime | None]:
        """From the first instance's start to the last one's end; None for a rule that never ends."""
        first = self._instance(self._first)
        if not self.bounded:
            return first.start, None
        last = first
        for found in self.walk(None):
            last = found
        return first.start, last.end
