"""Property 8 — time is the world's.

Every timestamp a provider returns for something created after the world's clock was set to an unusual moment (a
different year, an odd minute and second) is that moment, in the vendor's own format (the driver parses it
strictly, to the finest resolution its documentation gives); advancing the clock moves the timestamps of what is
created afterwards, and nothing else: what was there before reads exactly as it did.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from tests.conformance.contract import Family, Property
from tests.conformance.harness import Case, Harness, absent, cases, parametrize, require, seed

type Record = Callable[[str, object], None]

UNUSUAL = datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC)
LATER = timedelta(hours=7, minutes=11)


@parametrize(cases(Property.TIME, Family.ACCOUNTS, ["new_things_carry_the_worlds_clock"]))
def test_what_is_created_carries_the_worlds_clock_and_advancing_moves_nothing_else(
    case: Case, harness: Harness, record_property: Record
) -> None:
    driver = require(case.provider, Family.ACCOUNTS)
    if absent(driver, "time.stamp", record_property):
        return
    slack = driver.time_resolution
    with harness.world(driver, seed(case.provider)) as world:
        world.advance(to=UNUSUAL)
        with harness.session(driver, world) as session:
            first, at = session.stamp("made at the unusual moment")
            assert abs(at - UNUSUAL) < slack, f"created at the world's {UNUSUAL}, the vendor says {at}"
            before = session.observe()
        world.advance(LATER)
        with harness.session(driver, world) as session:
            assert session.observe() == before, "advancing the clock changed what the vendor answers for what was there"
            assert session.stamped(first) == at, "advancing the clock moved the time of something made before"
            _, later = session.stamp("made after the clock moved")
            assert abs(later - (UNUSUAL + LATER)) < slack, f"created at {UNUSUAL + LATER}, the vendor says {later}"
