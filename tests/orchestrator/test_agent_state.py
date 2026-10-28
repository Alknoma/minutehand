"""The agent's own state, saved at each checkpoint by its `sandbox.export_state`, and a fork that starts the agent
from it instead of playing its recorded inputs back (`application.agent_state`).

The sandbox is the due-record poller's (`tests/orchestrator/test_due_records.py`), and what it saves as the agent's
state is its clock: enough to tell which checkpoint a folder belongs to.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.agent_state import AGENT_STATE, TAKING, WORKED
from minutehand.application.checkpoint import Checkpoint, checkpoints
from minutehand.application.interruption import Interruption
from minutehand.application.memory import Versions, remember
from minutehand.application.orchestrator import run_scenario
from minutehand.application.refusals import RunRefused
from minutehand.application.replier import PeopleReplier
from minutehand.application.restore import Playback, Played, Restored, RestoreStep
from minutehand.application.rewind import RESTORE_RECORD, fork_run
from minutehand.application.run_clock import RunClock
from minutehand.application.sandbox import SandboxReading
from minutehand.domain.agent import AgentUnderTest, Sandbox
from minutehand.domain.experiment import Fork
from minutehand.domain.memory import MemoryPut, MemoryWrite
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.templates import AGENT_STATE_VARIABLE, STARTS_AT_VARIABLE
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store
from tests.orchestrator.test_due_records import AGENT, HOUR, T0, Act, _NothingToPlay, _Poller, _services, close, job
from tests.orchestrator.test_due_records import scenario as jobs_scenario
from tests.support.scenarios import SANDBOX

SAVING = AgentUnderTest(
    name=AGENT.name,
    sandbox=Sandbox(deadlines=SANDBOX.deadlines, advance=SANDBOX.advance, export_state=["save", "{dir}"]),
    due=AGENT.due,
)
JOBS = {"jobs/a": job(HOUR), "jobs/b": job(2 * HOUR)}


class _Saving(_Poller):
    """The poller's sandbox, whose `export_state` saves its clock into the folder it is given, after a moment of
    real work (a real command is a process the run awaits); `failing` are the saves, counted from 1, that fail."""

    def __init__(
        self,
        act: Act = close,
        *,
        at: datetime = T0,
        failing: Sequence[int] = (),
        while_saving: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(act, at=at)
        self.saves = 0
        self._failing = set(failing)
        self._while_saving = while_saving

    async def export(self, directory: Path) -> str | None:
        self.saves += 1
        if self._while_saving is not None:
            self._while_saving(self.saves)
        await asyncio.sleep(0.01)
        if self.saves in self._failing:
            (directory / "half").write_text("written before it failed")
            return "save exited 1: no space left on device"
        (directory / "clock").write_text(str(self._now_ns))
        return None


class _Program:
    """`OwnProgram` with no process: what it was started with is kept."""

    command: Sequence[str] = ("agent", "--fixed")

    def __init__(self) -> None:
        self.started: list[dict[str, str]] = []

    async def start(self, environment: Mapping[str, str] | None = None) -> None:
        self.started.append(dict(environment or {}))

    async def stop(self) -> None:
        return None


class _Played(_NothingToPlay):
    """`Playback` that says whether it was asked to play."""

    def __init__(self) -> None:
        self.played = 0

    async def play(self, progress: object, *, start: object) -> Played:
        self.played += 1
        return await super().play(progress, start=start)


async def _parent(
    tmp_path: Path, agent: AgentUnderTest, sandbox: _Saving, interruption: Interruption | None = None
) -> tuple[RunRecord, SqliteStore]:
    played = jobs_scenario(JOBS)
    clock = RunClock(T0)
    store = SqliteStore(tmp_path / "world.db", "parent", clock)
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
        interruption=interruption,
    )
    return record, store


def _open_parent(tmp_path: Path) -> Callable[[Clock], Store]:
    def opened(clock: Clock) -> Store:
        return SqliteStore(tmp_path / "world.db", "parent", clock)

    return opened


def _after(store: Store, record: RunRecord, at: timedelta) -> tuple[int, Checkpoint]:
    wake = next(w.index for w in record.wakes if w.sim_time == T0 + at)
    return max((seq, c) for seq, c in checkpoints(store).items() if c.wake == wake)


async def _fork(
    tmp_path: Path,
    record: RunRecord,
    at_seq: int,
    playback: Playback,
    own: _Program,
    *,
    code_changed: Sequence[str] = (),
    interruption: Interruption | None = None,
) -> list[RunRecord]:
    at = checkpoints(_open_parent(tmp_path)(RunClock(T0)))[at_seq].now
    sandbox = _Saving(at=at)
    return await fork_run(
        fork=Fork(parent_run="parent", at_seq=at_seq),
        parent=record,
        open_parent=_open_parent(tmp_path),
        run_id="child",
        scenario=jobs_scenario(JOBS),
        agent=SAVING,
        sandbox=sandbox,
        mounts=sandbox,
        playback_for=lambda _store, _checkpoint: playback,
        services=_services(build()),
        replier_for=lambda s, _pins: PeopleReplier(s, None, []),
        state_dir=tmp_path / "state",
        own=own,
        code_changed=code_changed,
        interruption=interruption,
    )


async def test_the_agents_state_is_saved_at_every_checkpoint_after_a_wake_and_named_for_its_seq(tmp_path: Path) -> None:
    sandbox = _Saving()
    _, store = await _parent(tmp_path, SAVING, sandbox)
    folder = tmp_path / "state" / "parent" / AGENT_STATE
    held = checkpoints(store)
    setup = [seq for seq, c in held.items() if c.wake == 0]
    assert all(held[seq].agent.exported is None for seq in setup), "setup saved state before the sandbox existed"
    after = {seq: c for seq, c in held.items() if c.wake > 0}
    assert after and all(c.agent.exported is not None and c.agent.exported.saved for c in after.values())
    assert sorted(p.name for p in folder.iterdir()) == sorted(str(seq) for seq in after), (
        "each checkpoint after a wake has its own folder, named for its seq, and nothing is left being taken"
    )
    for seq, checkpoint in after.items():
        saved = int((folder / str(seq) / "clock").read_text())
        assert saved == int(checkpoint.now.timestamp()) * 1_000_000_000, f"seq {seq} holds another moment's state"
    assert sandbox.saves == len(after)


async def test_a_failed_save_is_kept_with_its_checkpoint_and_the_run_goes_on(tmp_path: Path) -> None:
    record, store = await _parent(tmp_path, SAVING, _Saving(failing=[2]))
    assert record.stop is not StopReason.AGENT_FAILED and record.failure is None
    failed = [(seq, c) for seq, c in checkpoints(store).items() if c.agent.exported and not c.agent.exported.saved]
    assert len(failed) == 1
    seq, checkpoint = failed[0]
    assert checkpoint.agent.exported is not None
    assert checkpoint.agent.exported.failure == "save exited 1: no space left on device"
    folder = tmp_path / "state" / "parent" / AGENT_STATE
    assert not (folder / str(seq)).exists() and not (folder / TAKING).exists()


async def test_no_state_is_saved_for_an_agent_file_that_names_no_export(tmp_path: Path) -> None:
    sandbox = _Saving()
    _, store = await _parent(tmp_path, AGENT, sandbox)
    assert sandbox.saves == 0
    assert all(c.agent.exported is None for c in checkpoints(store).values())
    assert not (tmp_path / "state" / "parent" / AGENT_STATE).exists()


async def test_a_signal_while_a_checkpoint_is_taken_waits_for_it_and_ends_the_run_interrupted(tmp_path: Path) -> None:
    interruption = Interruption()
    sandbox = _Saving(while_saving=lambda n: interruption.receive("SIGTERM") if n == 2 else None)
    record, store = await _parent(tmp_path, SAVING, sandbox, interruption=interruption)
    assert record.stop is StopReason.INTERRUPTED
    assert [w.sim_time - T0 for w in record.wakes] == [timedelta(0), HOUR], "the run went on after the signal"
    seq, checkpoint = _after(store, record, HOUR)
    assert checkpoint.agent.exported is not None and checkpoint.agent.exported.saved, (
        "the checkpoint being taken when the signal came was abandoned: the newest checkpoint must be whole"
    )
    assert (tmp_path / "state" / "parent" / AGENT_STATE / str(seq) / "clock").is_file()
    assert record.failure is not None and "SIGTERM" in record.failure and f"seq {seq}" in record.failure


async def test_a_signal_mid_wake_abandons_the_wake_and_its_last_checkpoint_forks(tmp_path: Path) -> None:
    interruption = Interruption()
    poll = _Saving.read

    class _Stopped(_Saving):
        async def read(self) -> SandboxReading:
            if self._now_ns >= int((T0 + 2 * HOUR).timestamp()) * 1_000_000_000:
                interruption.receive("SIGTERM")
                await asyncio.sleep(1)  # the signal lands while the wake waits on its sandbox
            return await poll(self)

    record, store = await _parent(tmp_path, SAVING, _Stopped(), interruption=interruption)
    assert record.stop is StopReason.INTERRUPTED
    assert [w.sim_time - T0 for w in record.wakes] == [timedelta(0), HOUR]
    at_seq, _ = _after(store, record, HOUR)
    assert max(checkpoints(store)) == at_seq, "the abandoned wake wrote a checkpoint"
    store.close()
    [child] = await _fork(tmp_path, record, at_seq, _Played(), _Program())
    assert [w.sim_time - T0 for w in child.wakes if w.index > 2] == [2 * HOUR], "the fork did not carry the run on"


async def test_a_fork_at_saved_state_starts_the_agent_from_it_and_plays_nothing_back(tmp_path: Path) -> None:
    record, store = await _parent(tmp_path, SAVING, _Saving())
    at_seq, checkpoint = _after(store, record, HOUR)
    store.close()
    own, playback = _Program(), _Played()
    changed = ["its command: agent -> agent --fixed"]
    [child] = await _fork(tmp_path, record, at_seq, playback, own, code_changed=changed)
    assert playback.played == 0, "the fork played the agent's recorded inputs back over its saved state"
    folder = (tmp_path / "state" / "parent" / AGENT_STATE / str(at_seq)).resolve()
    assert own.started == [
        {
            AGENT_STATE_VARIABLE: str(folder),
            STARTS_AT_VARIABLE: str(int(checkpoint.now.timestamp() * 1_000_000_000)),
        }
    ]
    restored = Restored.model_validate_json((tmp_path / "state" / "child" / RESTORE_RECORD).read_text())
    assert restored.agent_state == str(folder) and restored.program_changes == changed
    assert RestoreStep.PLAYBACK not in [s.step for s in restored.steps] and not restored.verified
    assert [w.sim_time - T0 for w in child.wakes if w.index > checkpoint.wake] == [2 * HOUR]


async def test_without_saved_state_a_fork_plays_the_agent_back(tmp_path: Path) -> None:
    record, store = await _parent(tmp_path, SAVING, _Saving(failing=[2]))
    at_seq, checkpoint = _after(store, record, HOUR)
    assert checkpoint.agent.exported is not None and not checkpoint.agent.exported.saved
    store.close()
    own, playback = _Program(), _Played()
    await _fork(tmp_path, record, at_seq, playback, own)
    assert playback.played == 1
    restored = Restored.model_validate_json((tmp_path / "state" / "child" / RESTORE_RECORD).read_text())
    assert restored.agent_state is None and restored.verified


async def test_a_fork_whose_saved_state_is_gone_is_refused(tmp_path: Path) -> None:
    record, store = await _parent(tmp_path, SAVING, _Saving())
    at_seq, _ = _after(store, record, HOUR)
    store.close()
    folder = tmp_path / "state" / "parent" / AGENT_STATE / str(at_seq)
    for kept in folder.iterdir():
        kept.unlink()
    folder.rmdir()
    with pytest.raises(RunRefused, match=f"saved the agent's own state into {folder}, which is gone"):
        await _fork(tmp_path, record, at_seq, _Played(), _Program())


async def test_a_fork_stopped_while_its_agent_is_brought_back_is_refused(tmp_path: Path) -> None:
    record, store = await _parent(tmp_path, AGENT, _Saving())
    at_seq, _ = _after(store, record, HOUR)
    store.close()
    interruption = Interruption()

    class _Interrupted(_NothingToPlay):
        async def play(self, progress: object, *, start: object) -> Played:
            interruption.receive("SIGINT")
            await asyncio.sleep(1)
            return await super().play(progress, start=start)

    with pytest.raises(RunRefused, match="stopped by SIGINT while its agent was being brought back"):
        await _fork(tmp_path, record, at_seq, _Interrupted(), _Program(), interruption=interruption)
    with sqlite3.connect(tmp_path / "world.db") as db:
        held = [r[0] for r in db.execute("SELECT run_id FROM run")]
    assert held == ["parent"], "the refused fork left a run behind"


class _Stirring(_Saving):
    """The poller's sandbox, whose own timer comes due while save `stirs` runs: the agent's next cycle begins in the
    middle of the export and writes to its memory."""

    def __init__(self, stirs: int) -> None:
        super().__init__()
        self._stirs = stirs

    async def export(self, directory: Path) -> str | None:
        if self.saves + 1 == self._stirs and self._store is not None:
            remember(
                self._store,
                MemoryWrite(writes=[MemoryPut(collection="due", key="jobs/c", value=job(None))]),
                Versions(),
            )
        return await super().export(directory)


async def test_a_save_the_agent_worked_through_is_discarded_and_a_fork_there_plays_the_agent_back(
    tmp_path: Path,
) -> None:
    """What the export saved holds part of one moment and part of the next, beside a world that holds neither: the
    checkpoint says the state was not saved and why, nothing is left in its folder, and a fork there plays back."""
    record, store = await _parent(tmp_path, SAVING, _Stirring(stirs=2))
    at_seq, checkpoint = _after(store, record, HOUR)
    exported = checkpoint.agent.exported
    assert exported is not None and not exported.saved, "a save the agent worked through was kept"
    assert exported.failure == f"{WORKED}: it wrote to the run's log"
    folder = tmp_path / "state" / "parent" / AGENT_STATE
    assert not (folder / str(at_seq)).exists() and not (folder / TAKING).exists()
    others = [c for seq, c in checkpoints(store).items() if c.wake > 0 and seq != at_seq]
    assert others and all(c.agent.exported is not None and c.agent.exported.saved for c in others)
    store.close()
    own, playback = _Program(), _Played()
    await _fork(tmp_path, record, at_seq, playback, own)
    assert playback.played == 1, "a fork at a mixed save started the agent from it"


class _BusyAfterExport(_Saving):
    """The poller's sandbox, whose export leaves it busy for a few readings after the command returns (a real
    store still writing its export out), with nothing of the agent's moving."""

    def __init__(self) -> None:
        super().__init__()
        self._busy = 0

    async def export(self, directory: Path) -> str | None:
        failed = await super().export(directory)
        self._busy = 3
        return failed

    async def read(self) -> SandboxReading:
        reading = await super().read()
        if self._busy:
            self._busy -= 1
            return reading.model_copy(update={"idle": False})
        return reading


async def test_a_save_whose_export_leaves_the_sandbox_busy_for_a_moment_is_kept(tmp_path: Path) -> None:
    """The sandbox is busy right after the export with the export's own work, not the agent's: the save waits for it
    to fall idle and keeps the state, where one reading taken as the command returns would discard every save."""
    record, store = await _parent(tmp_path, SAVING, _BusyAfterExport())
    _, checkpoint = _after(store, record, HOUR)
    assert checkpoint.agent.exported is not None and checkpoint.agent.exported.saved, checkpoint.agent.exported
