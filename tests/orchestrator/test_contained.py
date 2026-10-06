"""An agent in a sandbox whose clock the run owns (`Contained`): its own timers are its wakes, and the sandbox's clock
moves with the run's."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.application.dues import due_entries
from minutehand.domain.agent import Contained
from minutehand.domain.clock import DueClosed, DueSource
from minutehand.domain.world import Actor, MessageSnapshot
from tests.orchestrator.rig import AGENTS, T0, Rig, scenario

HOUR_NS = 3600 * 10**9


def sandbox(rig: Rig, state: Path, *timers_after_hours: float) -> Contained:
    state.write_text(
        json.dumps(
            {"now_ns": 0, "timers": [int(h * HOUR_NS) for h in timers_after_hours], "base": rig.base, "fired_at_ns": []}
        )
    )
    fake = [sys.executable, str(AGENTS / "fake_sandbox.py"), str(state)]
    return Contained(
        deadlines=[*fake, "deadlines"], advance=[*fake, "advance", "{nanoseconds}"], quiet=timedelta(milliseconds=5)
    )


async def test_the_agents_own_timer_is_its_next_wake_and_the_sandbox_clock_moves_with_the_runs(
    rig: Rig, tmp_path: Path
) -> None:
    state = tmp_path / "sandbox.json"
    agent = rig.agent("ask_silent", extra=[sandbox(rig, state, 36)])
    record, store, _ = await rig.run(scenario(ticket_fates=[], deadline_after=timedelta(days=3)), agent)

    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=36)]
    assert record.wakes[1].world_changes == 1, "the timer's follow-up is the agent's change in that wake"
    [timer] = [e for e in due_entries(store) if e.source is DueSource.TIMER]
    assert (timer.due.at, timer.closed) == (T0 + timedelta(hours=36), DueClosed.FIRED)
    follow_up = [e for e in store.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)][-1]
    assert follow_up.sim_time == T0 + timedelta(hours=36)
    held = json.loads(state.read_text())
    assert held["fired_at_ns"] == [36 * HOUR_NS]
    assert held["now_ns"] == 72 * HOUR_NS, "the sandbox reached the deadline the run ran on to, three days in"


def test_a_contained_source_without_a_place_for_the_step_is_refused() -> None:
    with pytest.raises(ValueError, match=r"`advance` must hold \{nanoseconds\}"):
        Contained(deadlines=["true"], advance=["true"])


async def test_a_contained_run_is_scored_with_its_timer_read_as_the_agents_own_plan(rig: Rig, tmp_path: Path) -> None:
    from minutehand.checks.runner import evaluate, view_of

    agent = rig.agent("ask_silent", extra=[sandbox(rig, tmp_path / "sandbox.json", 100)])
    scn = scenario(ticket_fates=[], deadline_after=timedelta(days=5))
    record, store, _ = await rig.run(scn, agent)

    view = view_of(scn, store.events(), record.wakes, store.replies(), dues=due_entries(store))
    found = evaluate(view, stop=record.stop).findings
    late = [f.message for f in found if f.check == "late_follow_up"]
    assert late == [
        "followed up 34 hours after the wait expired; when it expired, the agent's own next wake was 1 day 10 hours "
        "later (its own timer, read from its sandbox in wake 1)"
    ], [(f.check, f.message) for f in found]


async def test_a_timer_that_does_nothing_is_no_wake_and_the_one_that_acts_is(rig: Rig, tmp_path: Path) -> None:
    state = tmp_path / "sandbox.json"
    agent = rig.agent("ask_silent", extra=[sandbox(rig, state, 1, 2, 3, 36)])
    held = json.loads(state.read_text())
    held["quiet_timers"] = [h * HOUR_NS for h in (1, 2, 3)]  # a runtime's housekeeping: they fire and do nothing
    state.write_text(json.dumps(held))
    record, _, _ = await rig.run(scenario(ticket_fates=[], deadline_after=timedelta(days=3)), agent)

    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=36)]
    assert [w.index for w in record.wakes] == [1, 2], "the housekeeping took no wake's number"
