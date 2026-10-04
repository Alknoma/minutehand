"""The run loop against a real agent program, real providers over HTTP, the real store and the real clock."""

from __future__ import annotations

from datetime import timedelta

from minutehand.domain.agent import Booked
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, EntityKind, Operation, TicketSnapshot
from tests.orchestrator.rig import T0, Rig, scenario


async def test_a_36_hour_reply_and_a_3_day_ticket_fate_move_the_clock_exactly_there(rig: Rig) -> None:
    record, store, clock = await rig.run(scenario(), rig.agent("ask_and_file"))

    assert clock.jumps == [T0 + timedelta(hours=36), T0 + timedelta(days=3), T0 + timedelta(days=4)]
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=36), T0 + timedelta(days=4)]
    assert record.stop is StopReason.AGENT_DONE
    # START sent a message and filed a ticket; the reply wake sent one message; the last wake only read.
    assert [w.world_changes for w in record.wakes] == [2, 1, 0]
    fate = [e for e in store.events() if e.actor is Actor.PERSON and e.entity.kind is EntityKind.TICKET]
    assert len(fate) == 1 and fate[0].sim_time == T0 + timedelta(days=3)
    assert isinstance(fate[0].after, TicketSnapshot) and fate[0].after.state is TicketState.DONE
    assert [r.text for r in store.replies()] == ["Yes, 40k."]
    assert len(rig.chat.pushed) == 1 and "Yes, 40k." in rig.chat.pushed[0]


async def test_a_silent_person_never_replies_and_the_run_ends_with_nothing_pending(rig: Rig) -> None:
    record, store, _ = await rig.run(scenario(ticket_fates=[]), rig.agent("ask_silent"))

    assert record.stop is StopReason.NOTHING_PENDING
    assert len(record.wakes) == 1
    assert store.replies() == [] and rig.chat.pushed == []
    assert not [e for e in store.events() if e.actor is Actor.PERSON]


async def test_a_silent_person_and_a_wake_past_the_deadline_end_the_run_at_the_deadline(rig: Rig) -> None:
    record, store, clock = await rig.run(
        scenario(ticket_fates=[]), rig.agent("ask_silent"), env=rig.env(NEXT_WAKE_AFTER_HOURS=str(15 * 24)),
    )

    assert record.stop is StopReason.DEADLINE_PASSED
    assert clock.jumps == [T0 + timedelta(days=14)] and record.ended_at == T0 + timedelta(days=14)
    assert len(record.wakes) == 1 and store.events()[-1].sim_time == T0 + timedelta(days=14)


async def test_with_nothing_pending_the_world_runs_on_to_the_deadline_without_waking_the_agent(rig: Rig) -> None:
    record, store, clock = await rig.run(scenario(ticket_fates=[]), rig.agent("ask_silent"))

    assert record.stop is StopReason.NOTHING_PENDING
    assert clock.jumps == [T0 + timedelta(days=14)] and record.ended_at == T0 + timedelta(days=14)
    assert [w.sim_time for w in record.wakes] == [T0]
    assert store.events()[-1].sim_time == T0 + timedelta(days=14)


async def test_max_wakes_stops_an_agent_that_always_asks_to_wake_again(rig: Rig) -> None:
    record, _, _ = await rig.run(scenario(max_wakes=3, ticket_fates=[]), rig.agent("keep_waking"))

    assert record.stop is StopReason.WAKE_LIMIT
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)]


async def test_a_booking_fires_at_its_time_and_a_cancelled_one_does_not(rig: Rig) -> None:
    record, store, clock = await rig.run(scenario(ticket_fates=[]), rig.agent("book", extra=[Booked()]))

    assert rig.sched.fired == [("kept", T0 + timedelta(hours=5))]
    assert clock.jumps == [T0 + timedelta(hours=5), T0 + timedelta(days=14)]
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=5)]
    assert record.stop is StopReason.NOTHING_PENDING
    deleted = [e for e in store.events() if e.operation is Operation.DELETE]
    assert [e.entity.external_id for e in deleted] == ["dropped"]


async def test_an_agent_that_exits_non_zero_ends_the_run_agent_failed(rig: Rig) -> None:
    record, _, _ = await rig.run(scenario(), rig.agent("fail"))

    assert record.stop is StopReason.AGENT_FAILED
    assert len(record.wakes) == 1
