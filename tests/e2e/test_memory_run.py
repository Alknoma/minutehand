"""The agent's memory through whole runs of a real agent process: it remembers across wakes through
`minutehand.agent.store`, a fork from a middle checkpoint starts from exactly the memory the checkpoint holds with no
hooks, the agent's own database is never opened, and state kept outside the store is named where it shows."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.checkpoint import Remembered, checkpoints
from minutehand.application.dues import due_entries
from minutehand.application.memory import digest, memory_of
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import Verification
from minutehand.domain.agent import OwnDatabase
from minutehand.domain.clock import DueClosed
from minutehand.domain.experiment import Fork, MemoryEdit
from minutehand.domain.memory import SeededMemory
from minutehand.domain.run import StopReason
from minutehand.domain.world import Actor, EntityKind
from tests.e2e.support import (
    FOLLOW_UP,
    QUESTION,
    SOFIA,
    T0,
    THANKS,
    agent_under_test,
    answers,
    messages,
    scenario,
    texts,
    world,
)

LATE = answers(to_ask=2, after=timedelta(hours=12))
"""Sofia answers only the follow-up, half a day after it: the agent asks, follows up two days later, hears back."""


async def test_a_fork_from_the_middle_starts_from_the_memory_its_checkpoint_holds_with_no_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    state = tmp_path / "state"
    [parent] = await session.play(scenario(LATE), launched.agent, state=state, command=launched.command)
    run_id = parent.record.run_id
    assert parent.record.stop is StopReason.AGENT_DONE
    assert texts(messages(world(state, run_id).events(), Actor.AGENT, to=SOFIA)) == [QUESTION, FOLLOW_UP, THANKS]
    remembered = launched.state(state, run_id)
    assert remembered["follow_ups"] == 1 and remembered["status"] == "done"
    assert [w.memory_writes > 0 for w in parent.record.wakes] == [True, True, True]
    assert all(w.memory_reads > 0 for w in parent.record.wakes)

    middle = next(p for p in session.fork_points(state, run_id) if p.wake == 1)
    assert isinstance(middle.agent, Remembered)
    parent_world = world(state, run_id)
    at_checkpoint = memory_of(parent_world.events(), until=middle.seq)
    assert at_checkpoint != memory_of(parent_world.events())
    assert json.loads(at_checkpoint[("default", "state")])["follow_ups"] == 0
    assert digest(at_checkpoint) == checkpoints(parent_world)[middle.seq].agent.memory

    [child] = await session.fork(
        run_id, Fork(parent_run=run_id, at_seq=middle.seq), state=state, command=launched.command
    )

    child_world = world(state, child.record.run_id, root=run_id)
    assert memory_of(child_world.events(), until=middle.seq) == at_checkpoint
    restored = session.restore_of(state, child.record.run_id)
    assert restored is not None and restored.verified
    assert restored.verified_by == [Verification.MEMORY, Verification.REPORT]
    assert restored.memory == digest(at_checkpoint)
    # From the checkpoint on it acts on that memory: it follows up once more, as it did, and hears back.
    after = [e for e in child_world.events() if e.seq > middle.seq]
    assert texts(messages(after, Actor.AGENT, to=SOFIA)) == [FOLLOW_UP, THANKS]
    assert launched.state(state, child.record.run_id, root=run_id)["follow_ups"] == 1
    assert not launched.state_file.exists(), "the agent's own database was opened while Minutehand played it"


async def test_an_agent_that_keeps_state_outside_the_store_has_its_fork_refused_saying_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its next wake is in a file of its own as well: by the parent's end the file says none, and a fork from the
    first wake, whose memory says two days on, starts against it."""
    monkeypatch.setenv("OUTSIDE_FILE", str(tmp_path / "next_wake.json"))
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    state = tmp_path / "state"
    [parent] = await session.play(scenario(LATE), launched.agent, state=state, command=launched.command)
    run_id = parent.record.run_id
    middle = next(p for p in session.fork_points(state, run_id) if p.wake == 1)

    with pytest.raises(RunRefused) as refused:
        await session.fork(run_id, Fork(parent_run=run_id, at_seq=middle.seq), state=state, command=launched.command)

    said = str(refused.value)
    assert f"the agent after the fork is not the agent at the checkpoint at seq {middle.seq}" in said
    assert f"next_wake: {(T0 + timedelta(days=2)).isoformat()} at the checkpoint, none after" in said
    assert "state the agent keeps outside `minutehand.agent.store`" in said
    assert [o.record.run_id for o in session.runs(state)] == [run_id], "the refused fork left no run behind"


async def test_an_own_database_is_fresh_and_empty_for_every_run_and_named_as_outside_forks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    agent = launched.agent.model_copy(update={"own_databases": [OwnDatabase(env="OWN_DB")]})
    state = tmp_path / "state"
    first, second = await session.play(scenario(LATE), agent, state=state, samples=2, command=launched.command)

    for outcome in (first, second):
        own = session.run_dir(state, outcome.record.run_id) / "own" / "OWN_DB.sqlite"
        assert own.is_file(), "the agent wrote to the database it was handed for this run"
        [note] = [n for n in outcome.result.notes if "OWN_DB" in n]
        assert str(own) in note and "a fork starts with it empty" in note
        held = checkpoints(world(state, outcome.record.run_id))
        setup, after_start = list(held)[:2]
        assert held[setup].agent.outside == [], "nothing was in it before the agent's first wake"
        [outside] = held[after_start].agent.outside
        assert outside.startswith("the agent's own database in OWN_DB held ")
    point = next(p for p in session.fork_points(state, first.record.run_id) if p.wake == 1)
    [child] = await session.fork(
        first.record.run_id,
        Fork(parent_run=first.record.run_id, at_seq=point.seq),
        state=state,
        command=launched.command,
    )
    account = session.fork_account(state, child.record.run_id)
    assert account is not None and account.restore is not None
    assert "outside its memory, which the fork did not get: the agent's own database in OWN_DB" in account.restore.words
    assert not (session.run_dir(state, child.record.run_id) / "own" / "OWN_DB.sqlite").exists(), (
        "the fork's own database starts empty: the agent took no goal in it after the checkpoint"
    )


def test_a_run_never_reads_production_memory_only_the_scenarios(tmp_path: Path) -> None:
    """The scenario's `memory:` is all a run's memory starts from: seeded as the scenario's, before the first wake."""
    from minutehand.adapters.store.sqlite import SqliteStore
    from minutehand.application import memory
    from minutehand.application.run_clock import RunClock
    from minutehand.domain.memory import SeededMemory

    store = SqliteStore(tmp_path / "w.db", "r", RunClock(T0))
    memory.seed(store, [SeededMemory(key="asks/sam", value={"status": "asked"})])
    [seeded] = [e for e in store.events() if e.entity.kind is EntityKind.MEMORY]
    assert seeded.actor is Actor.SCENARIO and seeded.wake == 0
    assert memory_of(store.events()) == {("default", "asks/sam"): '{"status":"asked"}'}


async def test_a_fork_that_marks_the_person_answered_in_memory_drops_the_follow_up_the_agent_had_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the end of wake 1 the agent had planned to follow up with Sofia two days on. The fork writes her answer
    into its memory: asked again, the agent plans nothing, and its old wake is replaced rather than fired."""
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    state = tmp_path / "state"
    [parent] = await session.play(scenario(LATE), launched.agent, state=state, command=launched.command)
    run_id = parent.record.run_id
    middle = next(p for p in session.fork_points(state, run_id) if p.wake == 1)
    held = json.loads(memory_of(world(state, run_id).events(), until=middle.seq)[("default", "state")])
    assert held["answer"] is None and held["next_wake"] == (T0 + timedelta(days=2)).isoformat()
    answered = MemoryEdit(put=[SeededMemory(key="state", value={**held, "answer": "Yes, 40k a year."})])

    [child] = await session.fork(
        run_id, Fork(parent_run=run_id, at_seq=middle.seq, overrides=[answered]), state=state, command=launched.command
    )

    assert [w.sim_time for w in child.record.wakes if w.index > 1] == [], "the stale follow-up wake fired"
    child_world = world(state, child.record.run_id, root=run_id)
    after = [e for e in child_world.events() if e.seq > middle.seq]
    assert texts(messages(after, Actor.AGENT, to=SOFIA)) == []
    replaced = [d for d in due_entries(child_world) if d.closed is DueClosed.REPLACED and d.due.ref == "next_wake"]
    assert [d.due.at for d in replaced] == [T0 + timedelta(days=2)]
    restored = session.restore_of(state, child.record.run_id)
    assert restored is not None and restored.replanned is not None and restored.replanned.next_wake is None
    edited = [e for e in after if e.entity.kind is EntityKind.MEMORY and e.actor is Actor.SCENARIO]
    assert len(edited) == 1


async def test_a_fork_without_memory_changes_still_compares_the_report_and_replaces_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    state = tmp_path / "state"
    [parent] = await session.play(scenario(LATE), launched.agent, state=state, command=launched.command)
    run_id = parent.record.run_id
    middle = next(p for p in session.fork_points(state, run_id) if p.wake == 1)
    [child] = await session.fork(
        run_id, Fork(parent_run=run_id, at_seq=middle.seq), state=state, command=launched.command
    )
    restored = session.restore_of(state, child.record.run_id)
    assert restored is not None and restored.verified and restored.replanned is None
    child_world = world(state, child.record.run_id, root=run_id)
    assert not [d for d in due_entries(child_world) if d.closed is DueClosed.REPLACED and d.closed_wake == 1]
