"""The scenario's dispatch rules: one of the agent's own wakes delivered late, twice, or never, and the table
recording each decision when the wake's moment came."""

from __future__ import annotations

from datetime import timedelta

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.checkpoint import PendingWake
from minutehand.application.dues import Dues, due_entries
from minutehand.checks.acted_on_repeated_wake import ActedOnRepeatedWake
from minutehand.checks.runner import view_of
from minutehand.domain.agent import AgentUnderTest, Booked, Polled, WakeReason
from minutehand.domain.clock import Due, DueClosed, DueKind, DueSource
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import DispatchFault, DispatchRule, PlannedBy
from tests.orchestrator.rig import T0, Rig, scenario
from tests.orchestrator.test_http_agents import TickingAgent, _run
from tests.orchestrator.world import RecordingClock, serving


def rule(
    wakes: PlannedBy, fault: DispatchFault, *, nth: int | None = None, minutes: float | None = None
) -> DispatchRule:
    return DispatchRule(
        wakes=wakes, fault=fault, nth=nth, by=timedelta(minutes=minutes) if minutes is not None else None
    )


async def test_a_reported_wake_made_late_is_held_back_and_delivered_late_in_its_place(rig: Rig) -> None:
    scn = scenario(
        max_wakes=3, ticket_fates=[], dispatch=[rule(PlannedBy.REPORTED, DispatchFault.LATE, nth=1, minutes=30)]
    )
    record, store, _ = await rig.run(scn, rig.agent("keep_waking"))

    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(minutes=90), T0 + timedelta(minutes=150)]
    first, late = [e for e in due_entries(store) if e.source is DueSource.REPORTED][:2]
    assert (first.due.at, first.closed, first.fault) == (T0 + timedelta(hours=1), DueClosed.DELAYED, DispatchFault.LATE)
    assert (late.due.at, late.asked_for, late.closed) == (
        T0 + timedelta(minutes=90),
        T0 + timedelta(hours=1),
        DueClosed.FIRED,
    )


async def test_a_reported_wake_dropped_is_never_delivered_and_the_agent_is_not_woken_again(rig: Rig) -> None:
    scn = scenario(ticket_fates=[], dispatch=[rule(PlannedBy.REPORTED, DispatchFault.DROPPED)])
    record, store, _ = await rig.run(scn, rig.agent("keep_waking"))

    assert record.stop is StopReason.NOTHING_PENDING and [w.sim_time for w in record.wakes] == [T0]
    [dropped] = [e for e in due_entries(store) if e.source is DueSource.REPORTED]
    assert (dropped.closed, dropped.closed_at, dropped.fault) == (
        DueClosed.DROPPED,
        T0 + timedelta(hours=1),
        DispatchFault.DROPPED,
    )


async def test_a_wake_delivered_twice_to_an_agent_with_no_guard_is_reviewed(rig: Rig) -> None:
    scn = scenario(
        max_wakes=3,
        ticket_fates=[],
        dispatch=[rule(PlannedBy.REPORTED, DispatchFault.TWICE, nth=1, minutes=10)],
    )
    record, store, _ = await rig.run(scn, rig.agent("keep_writing"))

    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=2), T0 + timedelta(hours=2, minutes=10)]
    view = view_of(scn, store.events(), record.wakes, store.replies(), dues=due_entries(store))
    [finding] = ActedOnRepeatedWake().run(view).findings
    assert finding.wake == 3 and len(finding.evidence) == 1
    assert finding.message == (
        f"wake 3 was the second delivery of the agent's own wake for {(T0 + timedelta(hours=2)):%Y-%m-%d %H:%M} UTC, "
        "10 minutes after the first, and the agent changed the world 1 time in it"
    )


async def test_a_booking_made_late_reaches_the_scheduler_late(rig: Rig) -> None:
    scn = scenario(ticket_fates=[], dispatch=[rule(PlannedBy.BOOKED, DispatchFault.LATE, minutes=60)])
    booked = Booked(take_limit=timedelta(seconds=0.1))
    await rig.run(scn, rig.agent("book", extra=[booked]))

    assert rig.sched.fired == [("kept", T0 + timedelta(hours=6))]


async def test_a_dropped_tick_leaves_the_rhythm_going_and_a_doubled_one_starts_no_second_rhythm(rig: Rig) -> None:
    rules = [
        rule(PlannedBy.POLLED, DispatchFault.TWICE, nth=1, minutes=30),
        rule(PlannedBy.POLLED, DispatchFault.DROPPED, nth=2),
    ]
    agent = TickingAgent()
    async with serving(agent.app()) as base:
        under_test = AgentUnderTest(name="ticker", wakes=[Polled(wake_url=f"{base}/tick", every=timedelta(hours=2))])
        scn = scenario(ticket_fates=[], deadline_after=timedelta(hours=9), dispatch=rules)
        await _run(rig, scn, under_test, "polled")

    hours = [(t - T0).total_seconds() / 3600 for r, t in agent.ticks if r is WakeReason.TICK]
    assert hours == [2, 2.5, 6, 8]


async def test_a_resumed_table_counts_the_wakes_that_already_fell_due_before_it(rig: Rig) -> None:
    clock = RecordingClock(T0)
    store = SqliteStore(rig.tmp / "count.db", "root", clock)
    rules = [rule(PlannedBy.REPORTED, DispatchFault.DROPPED, nth=2)]

    def wake(hours: float) -> PendingWake:
        return PendingWake(
            due=Due(at=T0 + timedelta(hours=hours), kind=DueKind.AGENT_WAKE, ref="next_wake"), reason=WakeReason.DUE
        )

    before = Dues(store, clock, rules)
    before.enter(wake(1))
    clock.jump(T0 + timedelta(hours=1))
    assert before.dispatch([wake(1).due]).delivered  # the first: no rule for it
    before.enter(wake(2))

    after = Dues(store, clock, rules)
    after.resume(before.items)
    clock.jump(T0 + timedelta(hours=2))
    dispatched = after.dispatch([wake(2).due])
    assert dispatched.delivered == [] and len(dispatched.withheld) == 1, "the second reported wake is the one dropped"


async def test_a_rule_for_the_nth_wake_wins_over_one_for_each(rig: Rig) -> None:
    clock = RecordingClock(T0)
    store = SqliteStore(rig.tmp / "nth.db", "root", clock)
    each_late = rule(PlannedBy.REPORTED, DispatchFault.LATE, minutes=10)
    second_dropped = rule(PlannedBy.REPORTED, DispatchFault.DROPPED, nth=2)
    dues = Dues(store, clock, [each_late, second_dropped])
    for hours in (1, 2):
        wake = PendingWake(
            due=Due(at=T0 + timedelta(hours=hours), kind=DueKind.AGENT_WAKE, ref="next_wake"), reason=WakeReason.DUE
        )
        dues.enter(wake)
        clock.jump(wake.due.at)
        dues.dispatch([wake.due])

    faults = [(e.fault, e.closed) for e in due_entries(store) if e.asked_for is None]
    assert faults == [(DispatchFault.LATE, DueClosed.DELAYED), (DispatchFault.DROPPED, DueClosed.DROPPED)]


async def test_a_wake_delivered_twice_to_an_agent_that_writes_nothing_on_it_is_not_reviewed(rig: Rig) -> None:
    scn = scenario(
        max_wakes=3, ticket_fates=[], dispatch=[rule(PlannedBy.REPORTED, DispatchFault.TWICE, nth=1, minutes=10)]
    )
    record, store, _ = await rig.run(scn, rig.agent("keep_waking"))

    assert len(record.wakes) == 3
    view = view_of(scn, store.events(), record.wakes, store.replies(), dues=due_entries(store))
    assert ActedOnRepeatedWake().run(view).findings == []


async def test_a_booking_delivered_twice_reaches_the_queue_twice_and_finishes_its_occurrence_once(rig: Rig) -> None:
    scn = scenario(ticket_fates=[], dispatch=[rule(PlannedBy.BOOKED, DispatchFault.TWICE, minutes=1)])
    await rig.run(scn, rig.agent("book", extra=[Booked(take_limit=timedelta(seconds=0.1))]))

    five = T0 + timedelta(hours=5)
    assert rig.sched.fired == [("kept", five), ("kept", five + timedelta(minutes=1))]
    assert rig.sched.advanced == [("kept", five + timedelta(minutes=1))]


async def test_a_booking_dropped_reaches_no_queue_and_its_occurrence_is_still_finished(rig: Rig) -> None:
    scn = scenario(ticket_fates=[], dispatch=[rule(PlannedBy.BOOKED, DispatchFault.DROPPED)])
    record, _, _ = await rig.run(scn, rig.agent("book", extra=[Booked(take_limit=timedelta(seconds=0.1))]))

    assert rig.sched.fired == [] and rig.sched.advanced == [("kept", T0 + timedelta(hours=5))]
    assert [w.sim_time for w in record.wakes] == [T0]
