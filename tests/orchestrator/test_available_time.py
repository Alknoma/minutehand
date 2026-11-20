"""How much of a person's available time lies between two moments (`moments.available_between`). The work left on a
piece of work held back mid-way is counted in it, so it counts only their working hours outside their absences, and
it is the span `after_available` takes to reach the same moment from the same start."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from minutehand.application.moments import after_available, available_between
from minutehand.domain.scenario import Absence, Person, WorkingHours

MONDAY = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
FRIDAY_AFTERNOON = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)

NINE_TO_FIVE = Person(key="rosa", name="Rosa", email="rosa@example.com", working_hours=WorkingHours())
AWAY_FROM_TUESDAY_NOON = Person(
    key="sam",
    name="Sam",
    email="sam@example.com",
    working_hours=WorkingHours(),
    absences=[Absence(starts_after=timedelta(days=1, hours=3), lasts=timedelta(days=1))],
)
ALWAYS = Person(key="lena", name="Lena", email="lena@example.com")


def test_only_working_hours_count_across_a_weekend() -> None:
    """Friday 15:00 to Monday 11:00 holds two hours of Friday and two of Monday: the weekend counts for nothing."""
    monday_late_morning = datetime(2026, 8, 31, 11, 0, tzinfo=UTC)
    assert available_between(
        FRIDAY_AFTERNOON, monday_late_morning, NINE_TO_FIVE, MONDAY, first_ask=MONDAY
    ) == timedelta(hours=4)


def test_an_absence_counts_for_nothing_even_where_it_begins_inside_a_working_day() -> None:
    """Monday 09:00 to Wednesday 15:00, away from Tuesday noon to Wednesday noon: Monday's eight hours, Tuesday's
    three before noon, Wednesday's three after it."""
    wednesday = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
    assert available_between(MONDAY, wednesday, AWAY_FROM_TUESDAY_NOON, MONDAY, first_ask=MONDAY) == timedelta(hours=14)


@pytest.mark.parametrize("person", [NINE_TO_FIVE, AWAY_FROM_TUESDAY_NOON, ALWAYS], ids=lambda p: p.key)
@pytest.mark.parametrize("hours", [1, 7.5, 8, 9, 30, 61])
def test_it_is_the_span_after_available_takes_to_reach_the_same_moment(person: Person, hours: float) -> None:
    span = timedelta(hours=hours)
    for start in (MONDAY, FRIDAY_AFTERNOON, MONDAY + timedelta(hours=20)):
        end = after_available(start, span, person, MONDAY, first_ask=MONDAY)
        assert available_between(start, end, person, MONDAY, first_ask=MONDAY) == span


def test_none_lies_between_a_moment_and_an_earlier_one() -> None:
    assert available_between(FRIDAY_AFTERNOON, MONDAY, NINE_TO_FIVE, MONDAY, first_ask=MONDAY) == timedelta(0)
    assert available_between(MONDAY, MONDAY, ALWAYS, MONDAY, first_ask=MONDAY) == timedelta(0)
