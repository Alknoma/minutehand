"""Property 7 — state lives only in the record.

After a sequence of changes, `reset` returns the vendor API's answers to exactly the seeded answers; a second world
opened from the same seed, the same way, in the same server answers exactly as a world in a fresh server does (no
state leaks between worlds or survives outside the record); two worlds changed at the same moment never see each
other's changes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from tests.conformance.contract import Family, Property
from tests.conformance.harness import Case, Harness, absent, cases, fresh_server, parametrize, require, seed, tag

type Record = Callable[[str, object], None]


@parametrize(cases(Property.STATE, Family.ACCOUNTS, ["reset_returns_the_seeded_answers"]))
def test_reset_returns_the_vendors_answers_to_exactly_the_seeded_ones(case: Case, harness: Harness) -> None:
    driver = require(case.provider, Family.ACCOUNTS)
    with harness.world(driver, seed(case.provider)) as world:
        with harness.session(driver, world) as session:
            seeded = session.observe()
            for n in range(3):
                session.change(f"change number {n}")
            changed = session.observe()
        assert changed != seeded, "three changes left the vendor's answers as seeded: the driver's change shows nothing"
        world.reset()
        with harness.session(driver, world) as session:
            assert session.observe() == seeded, "after reset the vendor answers other than it did when seeded"


@parametrize(cases(Property.STATE, Family.ACCOUNTS, ["a_world_reopened_answers_as_in_a_fresh_server"]))
def test_a_world_opened_again_in_a_used_server_answers_exactly_as_in_a_fresh_server(
    case: Case, harness: Harness
) -> None:
    driver = require(case.provider, Family.ACCOUNTS)
    claims = tag()
    spec = driver.world(seed(case.provider), claims)
    with harness.world(driver, {}, spec=spec) as world, harness.session(driver, world) as session:
        session.change("left behind by the first world")
    with harness.world(driver, {}, spec=spec) as world, harness.session(driver, world) as session:
        used = session.observe()
    with fresh_server() as client:
        fresh = Harness(client)
        with fresh.world(driver, {}, spec=spec) as world, fresh.session(driver, world) as session:
            untouched = session.observe()
    assert "left behind" not in used, "a change of a closed world is answered in a new world"
    assert used == untouched, "a world in a used server answers differently from the same world in a fresh server"


@parametrize(cases(Property.STATE, Family.ACCOUNTS, ["concurrent_worlds_never_see_each_other"]))
def test_two_worlds_changed_at_the_same_moment_never_see_each_others_changes(
    case: Case, harness: Harness, record_property: Record
) -> None:
    driver = require(case.provider, Family.ACCOUNTS)
    if absent(driver, "state.concurrent", record_property):
        return
    with harness.world(driver, seed(case.provider)) as one, harness.world(driver, seed(case.provider)) as two:
        with harness.session(driver, one) as first, harness.session(driver, two) as second:
            start = threading.Barrier(2)
            failed: list[BaseException] = []

            def change(session_label: tuple[object, str]) -> None:
                session, label = session_label
                try:
                    start.wait(10)
                    session.change(label)  # type: ignore[attr-defined]
                except BaseException as e:
                    failed.append(e)

            threads = [
                threading.Thread(target=change, args=((first, "only in world one"),)),
                threading.Thread(target=change, args=((second, "only in world two"),)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            assert not failed, failed
            seen_one, seen_two = first.observe(), second.observe()
    assert "only in world one" in seen_one and "only in world two" in seen_two, "a world does not see its own change"
    assert "only in world two" not in seen_one, "world one answers with world two's change"
    assert "only in world one" not in seen_two, "world two answers with world one's change"
