"""The sandbox's clock held between wakes (`Sandbox.hold`, `Sandbox.release`): with a hold, the agent's clock moves
only inside a wake, so a timer of its own that comes due while Minutehand works between two wakes (reads inboxes,
plays the people, checkpoints, saves the agent's state) fires as the next wake begins, inside it; without one, the
clock runs at real speed between wakes, as it always did.

The sandbox here is the due-record poller's (`tests/orchestrator/test_due_records.py`) with a scheduler loop of the
agent's own beside it, the kind no run reads: it sleeps 30 s, writes a beat to its memory, and sleeps again. Real
time passes on the sandbox's clock at every read of it (`TICK`) and through the agent's export (`EXPORT_TAKES`),
unless the clock is held.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application import agent_state
from minutehand.application.agent_state import AGENT_STATE, WORKED
from minutehand.application.checkpoint import checkpoints
from minutehand.application.memory import Versions, remember
from minutehand.application.orchestrator import run_scenario
from minutehand.application.playback import TracePlayback
from minutehand.application.replier import PeopleReplier
from minutehand.application.rewind import fork_run
from minutehand.application.run_clock import RunClock
from minutehand.application.sandbox import SandboxReading
from minutehand.domain.agent import AgentUnderTest, Sandbox
from minutehand.domain.experiment import Fork
from minutehand.domain.memory import MemoryPut, MemoryWrite
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, EntityKind, MemorySnapshot, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store
from tests.orchestrator.test_agent_state import JOBS, _NothingToPlay, _Program, _Saving
from tests.orchestrator.test_due_records import (
    AGENT,
    HOUR,
    QUICK,
    SECOND,
    T0,
    P,
    T,
    _Answering,
    _Door,
    _late_parent,
    _PlayedPoller,
    _services,
)
from tests.orchestrator.test_due_records import scenario as jobs_scenario

TICK = timedelta(seconds=20)
"""Real time that passes on a running sandbox's clock at each read of it."""
EVERY = timedelta(seconds=30)
"""The agent's own scheduler loop: a beat, then a sleep this long."""
EXPORT_TAKES = timedelta(seconds=40)
"""Real time the agent's export takes, longer than one sleep of its loop."""

HOLDING = QUICK.model_copy(
    update={
        "hold": ["runsc", "debug", "--hold-clock", "agent"],
        "release": ["runsc", "debug", "--release-clock", "agent"],
    }
)


def _scenario() -> Scenario:
    """Jobs due at one and two hours, watched to three: the run's clock then runs on to the window's end with no wake
    to take the step."""
    return jobs_scenario(JOBS).model_copy(update={"runs_for": 3 * HOUR})


def _agent(*, holds: bool, saves: bool) -> AgentUnderTest:
    sandbox = HOLDING if holds else QUICK
    if saves:
        sandbox = sandbox.model_copy(update={"export_state": ["save", "{dir}"]})
    return AGENT.model_copy(update={"sandbox": Sandbox.model_validate(sandbox.model_dump())})


class _Scheduled(_Saving):
    """The poller's sandbox, with the agent's own scheduler loop beside its poller, and a clock that runs on at each
    read and through an export unless held. `calls` keeps every hold, release and advance, in order."""

    def __init__(self, agent: AgentUnderTest, *, at: datetime = T0) -> None:
        super().__init__(at=at)
        self._source = agent.sandbox
        self.held = False
        self.calls: list[str] = []
        self._next_beat = at + EVERY

    async def hold(self) -> SandboxReading:
        assert self.holds, "the run held a sandbox whose agent file names no hold"
        self.calls.append("hold")
        self.held = True
        return await self.read()

    async def release(self) -> None:
        assert self.holds, "the run released a sandbox whose agent file names no release"
        self.calls.append("release")
        self.held = False

    async def read(self) -> SandboxReading:
        if not self.held and self._store is not None:
            self._now_ns += int(TICK.total_seconds()) * SECOND
            self._moved(self._store, self._now())
        return SandboxReading.model_validate_json(json.dumps({"idle": True, "now_ns": self._now_ns, "held": self.held}))

    async def advance(self, nanoseconds: int) -> None:
        self.calls.append("advance")
        await super().advance(nanoseconds)

    async def export(self, directory: Path) -> str | None:
        if not self.held and self._store is not None:
            self._now_ns += int(EXPORT_TAKES.total_seconds()) * SECOND
            self._moved(self._store, self._now())
        return await super().export(directory)

    def _moved(self, store: Store, now: datetime) -> None:
        super()._moved(store, now)
        if now >= self._next_beat:
            remember(
                store,
                MemoryWrite(
                    writes=[MemoryPut(collection="beats", key=f"beat/{now.isoformat()}", value={"at": now.isoformat()})]
                ),
                Versions(),
            )
            self._next_beat = now + EVERY


async def _run(tmp_path: Path, agent: AgentUnderTest) -> tuple[RunRecord, SqliteStore, _Scheduled]:
    played = _scenario()
    clock = RunClock(T0)
    store = SqliteStore(tmp_path / "world.db", "parent", clock)
    sandbox = _Scheduled(agent)
    record = await run_scenario(
        scenario=played,
        agent=agent,
        sandbox=sandbox,
        mounts=sandbox,
        store=store,
        clock=clock,
        services=_services(build()),
        replier=PeopleReplier(played, None, []),
        agent_state=tmp_path / "state" / "parent" / AGENT_STATE,
    )
    return record, store, sandbox


def _uncounted(record: RunRecord, store: Store, *, after: int = 0) -> int:
    """The agent's memory writes after wake `after` that no wake's record counts: each was made between two wakes,
    in none."""
    writes = [
        e
        for e in store.events(kinds=[EntityKind.MEMORY], actors=[Actor.AGENT])
        if e.operation not in (Operation.READ, Operation.SEARCH) and e.wake > after
    ]
    return len(writes) - sum(w.memory_writes for w in record.wakes if w.index > after)


def _beats(store: Store) -> list[tuple[int, datetime]]:
    """Each beat of the agent's own loop: the wake it was written in, and the agent's clock as it wrote it."""
    return [
        (e.wake, datetime.fromisoformat(e.after.key.removeprefix("beat/")))
        for e in store.events(kinds=[EntityKind.MEMORY], actors=[Actor.AGENT])
        if isinstance(e.after, MemorySnapshot) and e.after.collection == "beats"
    ]


def _ticked_while_held(calls: Sequence[str]) -> bool:
    """Whether `calls` (holds, releases, advances, and `tick` for a read that moved the clock) moved the clock by a
    read between a hold and the release after it."""
    held = False
    for call in calls:
        if call == "hold":
            held = True
        elif call == "release":
            held = False
        elif call == "tick" and held:
            return True
    return False


async def test_with_a_hold_a_timer_due_between_wakes_fires_only_as_the_next_wake_begins(tmp_path: Path) -> None:
    record, store, sandbox = await _run(tmp_path, _agent(holds=True, saves=False))
    assert _uncounted(record, store) == 0, (
        f"{_uncounted(record, store)} of the agent's writes were made between wakes: its own timer fired while the "
        "run worked outside a wake"
    )
    first = next(w.index for w in record.wakes if w.sim_time == T0 + HOUR)
    after_start = [(wake, at) for wake, at in _beats(store) if wake > 1]
    assert after_start[0] == (first, T0 + HOUR), (
        f"the beat due 30 s after the start's settle was written at {after_start[0]}: it should fire as the one-hour "
        "wake's step passes it, inside that wake"
    )
    assert sandbox.calls[:3] == ["hold", "release", "hold"], "the start was not held, caught up and released"


async def test_with_a_hold_the_agents_state_is_saved_under_the_held_clock(tmp_path: Path) -> None:
    _, store, _ = await _run(tmp_path, _agent(holds=True, saves=True))
    after = [c for c in checkpoints(store).values() if c.wake > 0]
    assert after and all(c.agent.exported is not None and c.agent.exported.saved for c in after), [
        c.agent.exported for c in after
    ]


async def test_without_a_hold_the_clock_runs_between_wakes_as_before(tmp_path: Path) -> None:
    """The same agent, the same timing, an agent file naming no hold: the run never holds, the agent's own loop
    beats between wakes, and an export it beats through is discarded."""
    record, store, sandbox = await _run(tmp_path, _agent(holds=False, saves=True))
    assert sandbox.calls and set(sandbox.calls) == {"advance"}
    assert _uncounted(record, store) > 0
    exported = [c.agent.exported for c in checkpoints(store).values() if c.wake > 0]
    assert exported and all(
        e is not None and not e.saved and e.failure is not None and e.failure.startswith(WORKED) for e in exported
    )


async def test_a_fork_from_saved_state_stands_held_at_the_checkpoint_until_its_first_wake(tmp_path: Path) -> None:
    agent = _agent(holds=True, saves=True)
    record, store, _ = await _run(tmp_path, agent)
    wake = next(w.index for w in record.wakes if w.sim_time == T0 + HOUR)
    at_seq, checkpoint = max((seq, c) for seq, c in checkpoints(store).items() if c.wake == wake)
    assert checkpoint.agent.exported is not None and checkpoint.agent.exported.saved
    store.close()

    def open_parent(clock: Clock) -> SqliteStore:
        return SqliteStore(tmp_path / "world.db", "parent", clock)

    sandbox = _Scheduled(agent, at=checkpoint.now)
    [child] = await fork_run(
        fork=Fork(parent_run="parent", at_seq=at_seq),
        parent=record,
        open_parent=open_parent,
        run_id="child",
        scenario=_scenario(),
        agent=agent,
        sandbox=sandbox,
        mounts=sandbox,
        playback_for=lambda _store, _checkpoint: _NothingToPlay(),
        services=_services(build()),
        replier_for=lambda s, _pins: PeopleReplier(s, None, []),
        state_dir=tmp_path / "state",
        own=_Program(),
    )
    assert sandbox.calls[:4] == ["hold", "hold", "advance", "release"], (
        f"the fork's sandbox was driven {sandbox.calls[:4]}: held once its agent started from the saved state, held "
        "still as the fork took up the run, and advanced while held then released as its first wake began"
    )
    assert [w.sim_time - T0 for w in child.wakes if w.index > wake] == [2 * HOUR]
    forked = SqliteStore(tmp_path / "world.db", "child", RunClock(T0))
    assert _uncounted(child, forked, after=wake) == 0
    forked.close()


class _HeldPlayedPoller(_PlayedPoller):
    """The played-back poller's sandbox, holding: its clock runs on 100 ms a read only while not held."""

    def __init__(self, memory: _Answering, looks_at: datetime) -> None:
        super().__init__(memory, looks_at)
        self._source = HOLDING
        self.held = False
        self.calls: list[str] = []

    async def hold(self) -> SandboxReading:
        self.calls.append("hold")
        self.held = True
        return await self.read()

    async def release(self) -> None:
        self.calls.append("release")
        self.held = False

    async def read(self) -> SandboxReading:
        if not self.held:
            self.calls.append("tick")
            self._now_ns += self._tick
            self._looked()
        return SandboxReading.model_validate_json(json.dumps({"idle": True, "now_ns": self._now_ns, "held": self.held}))

    async def advance(self, nanoseconds: int) -> None:
        self.calls.append("advance")
        await super().advance(nanoseconds)


async def test_a_played_back_wake_is_released_and_held_where_the_runs_was(tmp_path: Path) -> None:
    """Played back with a hold, the agent's start runs unheld and is held, each wake's clock is moved while held and
    released as the wake begins, and held again once it settles: the due-record wake still ends where it ended."""
    store, wake, due = await _late_parent(tmp_path)
    at_seq = max(seq for seq, c in checkpoints(store).items() if c.wake == wake)
    before = max(seq for seq, c in checkpoints(store).items() if c.wake == wake - 1)
    memory = _Answering()
    sandbox = _HeldPlayedPoller(memory, T + P + timedelta(milliseconds=1500))

    async def start() -> None:
        return None

    await TracePlayback(
        parent=store,
        checkpoint=checkpoints(store)[at_seq],
        at_seq=at_seq,
        starts_at=T0,
        sandbox=sandbox,
        proxy=memory,
        memory=memory,
        door=_Door(),
        traffic=None,
        due=due,
    ).play(lambda said: None, start=start)
    assert memory.writes == [("jobs/a", before)]
    assert sandbox.held, "the playback left the agent's clock running at the checkpoint"
    assert not _ticked_while_held(sandbox.calls), sandbox.calls
    moved = [i for i, c in enumerate(sandbox.calls) if c == "advance"]
    assert moved and all(sandbox.calls[i + 1] == "release" for i in moved), (
        f"a played wake's clock was not moved while held and released as the wake began: {sandbox.calls}"
    )
    assert sandbox.calls.count("hold") == wake + 1, "the start and every played wake end held"


class _StuckWhileHeld(_Scheduled):
    """The scheduled poller's sandbox, whose export waits on a timer of its own: it cannot finish while the clock
    is held, as the Firestore emulator's could not."""

    async def export(self, directory: Path) -> str | None:
        while self.held:
            await asyncio.sleep(0.01)
        return await super().export(directory)


async def test_an_export_that_cannot_finish_under_a_held_clock_has_the_clock_released_and_the_run_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held, the export would wait forever: after the patience the clock is released until it ends, held again,
    and every checkpoint after a wake says whether its save was kept."""
    monkeypatch.setattr(agent_state, "HELD_EXPORT_PATIENCE", timedelta(milliseconds=50))
    agent = _agent(holds=True, saves=True)
    played = _scenario()
    clock = RunClock(T0)
    store = SqliteStore(tmp_path / "world.db", "parent", clock)
    sandbox = _StuckWhileHeld(agent)
    record = await asyncio.wait_for(
        run_scenario(
            scenario=played,
            agent=agent,
            sandbox=sandbox,
            mounts=sandbox,
            store=store,
            clock=clock,
            services=_services(build()),
            replier=PeopleReplier(played, None, []),
            agent_state=tmp_path / "state" / "parent" / AGENT_STATE,
        ),
        timeout=60,
    )
    assert record.failure is None, record.failure
    after = [c for c in checkpoints(store).values() if c.wake > 0]
    assert after and all(c.agent.exported is not None for c in after)
    assert sandbox.held, "the clock was left running after the export it was released for"
