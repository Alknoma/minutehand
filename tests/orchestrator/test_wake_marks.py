"""The agent's next wake marked with `minutehand.agent.wake`: recorded as its own plan, and the moment it is woken."""

from __future__ import annotations

from datetime import timedelta

from minutehand.adapters.agent.polled import PolledDriver
from minutehand.adapters.agent.reach import reach_for
from minutehand.domain.agent import AgentUnderTest, Marked
from minutehand.domain.run import StopReason
from minutehand.domain.world import Actor, EntityKind, NextWakeSnapshot, Operation
from tests.orchestrator.rig import T0, Rig, scenario


async def test_a_marked_next_wake_is_the_moment_the_agent_is_woken(rig: Rig) -> None:
    record, store, _ = await rig.run(scenario(tom_finishes=False), rig.agent("mark_next"))
    assert record.stop is StopReason.AGENT_DONE
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=3)]
    [marked] = [e for e in store.events() if e.entity.kind is EntityKind.NEXT_WAKE]
    assert marked.actor is Actor.AGENT and marked.after == NextWakeSnapshot(at=T0 + timedelta(hours=3))


def test_a_marked_agent_is_woken_at_its_url_and_its_wake_ends_when_the_call_returns() -> None:
    agent = AgentUnderTest(
        name="m", wakes=[Marked(wake_url="http://127.0.0.1:1/wake", wake_timeout=timedelta(seconds=9))]
    )
    main = reach_for(agent).main
    assert isinstance(main, PolledDriver) and reach_for(agent).ticks is None


async def test_each_wake_counts_its_memory_reads_and_writes_and_none_of_them_changes_the_world(rig: Rig) -> None:
    """`keep_waking` touches nothing but its memory: a get and a put per wake."""
    scn = scenario(tom_finishes=False, max_wakes=3)
    record, _, _ = await rig.run(scn, rig.agent("keep_waking"))
    assert [(w.memory_reads, w.memory_writes, w.world_changes) for w in record.wakes] == [(1, 1, 0)] * 3


async def test_each_read_of_memory_is_counted_in_its_wake_and_one_that_finds_nothing_new_is_not_kept(rig: Rig) -> None:
    """`keep_polling` reads its state five times a wake and writes it once: five reads counted, one kept."""
    scn = scenario(tom_finishes=False, max_wakes=3)
    record, store, _ = await rig.run(scn, rig.agent("keep_polling"))
    assert [(w.memory_reads, w.memory_writes) for w in record.wakes] == [(5, 1)] * 3
    kept = [e.wake for e in store.events() if e.entity.kind is EntityKind.MEMORY and e.operation is Operation.READ]
    assert kept == [1, 2, 3]
