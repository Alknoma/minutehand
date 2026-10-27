"""The agent's own state, saved at a checkpoint by the agent file's `sandbox.export_state` and started from by a
fork: how a fork carries a run on with an agent whose code changed, which playing its recorded inputs back cannot.

    <state>/runs/<run_id>/agent-state/<seq>/   what `export_state` saved at the checkpoint at <seq> of <run_id>
    <state>/runs/<run_id>/agent-state/taking/  the one being saved, before its checkpoint is written

At every checkpoint after a wake (and the one the clock runs on to), and only once `SandboxClock.settle` has said the
sandbox is idle with nothing of the agent's in flight (the reading that ends the wake; nothing else is checked), the
run loop runs `export_state` with `{dir}` filled with an empty `taking/`, writes the checkpoint saying
whether it saved (`Remembered.exported`), and only then names the folder for the checkpoint's seq, since the seq is
the log's to give and is known once the checkpoint is in it. A command that fails leaves no folder, and the
checkpoint keeps what it said; the run goes on. Setup's checkpoint saves nothing: the sandbox does not exist yet.

A save is the agent's state at one quiet instant, the one the checkpoint's world is at, or it is nothing. The wake
ended inside the agent's own sleep, and the sandbox's clock runs on at real speed while the export runs: a poller
whose timer comes due before the export ends starts its next cycle in the middle of it, and the folder would hold
part of one moment and part of the next, beside a world that holds neither. So the run reads around the export
whether the agent stirred: the sandbox idle as it began and as it ended, no call of the agent's sent, answered or in
flight (`Traffic.activity`, `Traffic.waiting`), no read of its memory (`HoldsMemory.memory_reads`) and no event of the
agent's in the log between the two. Anything moved, and the folder is removed and the checkpoint says the state was
not saved, and why (`ExportedState.failure`: the agent worked during the export): a fork there plays the agent back,
as from any checkpoint without one, and never starts it from a mixed state. With a hold (`Sandbox.hold`) the export
runs under a clock held since the wake settled, so no timer of the agent's comes due during it, and the reads around
it are the guard that none did.

A fork at a checkpoint whose state was saved (`saved_at`) starts the agent's program with `MINUTEHAND_AGENT_STATE`
naming the folder, its sandbox born at the checkpoint's moment (`MINUTEHAND_STARTS_AT_NS`), waits until the sandbox
is idle, holds its clock there when the agent file names a hold, and carries on from the checkpoint (`StateImport`, `application.restore`). Nothing of the parent's record is
played into the agent, so its program may be another than the parent's: the difference is kept with the fork
(`program_changes`) and refuses nothing. Whatever the agent does as it starts is the fork's, answered by the fork's
world as it stands at the checkpoint. What the agent imports is the agent's own word: nothing proves it stands where
its parent stood, and the fork says so (`Restored.unverified`).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from minutehand.application.checkpoint import CHECKPOINT, Checkpoint, ExportedState
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import OUTPUT_KEPT, OwnProgram
from minutehand.application.sandbox import SandboxClock, moving
from minutehand.application.traffic import Traffic
from minutehand.domain.templates import AGENT_STATE_VARIABLE, STARTS_AT_VARIABLE
from minutehand.domain.world import Actor
from minutehand.ports.store import Store

AGENT_STATE = "agent-state"
"""In a run's directory: the agent's own state saved at each of the run's checkpoints, a folder per seq."""

TAKING = "taking"
"""In `AGENT_STATE`: the state being saved, before the checkpoint it belongs to is written."""


WORKED = "the agent worked during the export"
"""How a checkpoint says its save was discarded: the agent stirred while it ran (`save`)."""


async def save(
    sandbox: SandboxClock, folder: Path, store: Store, traffic: Traffic | None, reads: Callable[[], int]
) -> ExportedState:
    """Run the agent file's `export_state` into an empty `folder/taking/`: what the checkpoint keeps of it. A
    failure removes what it left there, and so does an agent that stirred while it ran (`WORKED`): its sandbox not
    idle as the export began or as it ended, a call of its sent, answered or in flight, a read of its memory
    (`reads`, every one the agent made, counted), or an event of its in `store`."""
    taking = folder / TAKING
    shutil.rmtree(taking, ignore_errors=True)  # a run stopped while saving left it half filled
    taking.mkdir(parents=True)
    began = await sandbox.read()
    calls, head, read = moving(traffic), store.head(), reads()
    failed = await sandbox.export(taking.resolve())
    if failed is not None:
        shutil.rmtree(taking, ignore_errors=True)
        return ExportedState(saved=False, failure=failed[-OUTPUT_KEPT:])
    ended = await sandbox.read()
    stirred = [
        said
        for said, moved in [
            ("its sandbox was not idle as the export began", not began.idle),
            ("its sandbox was not idle as the export ended", not ended.idle),
            ("it sent or was answered a call", moving(traffic) != calls or calls[1]),
            ("it read its memory", reads() != read),
            ("it wrote to the run's log", bool(store.events(since=head, actors=[Actor.AGENT]))),
        ]
        if moved
    ]
    if stirred:
        shutil.rmtree(taking, ignore_errors=True)
        return ExportedState(saved=False, failure=f"{WORKED}: {'; '.join(stirred)}")
    return ExportedState(saved=True)


def keep(folder: Path, seq: int) -> None:
    """Name what `save` saved for the checkpoint written at `seq`."""
    (folder / TAKING).rename(folder / str(seq))


def saved_at(store: Store, at_seq: int, checkpoint: Checkpoint, runs: Path) -> Path | None:
    """Where the agent's own state was saved at the checkpoint at `at_seq` (`runs/<run>/agent-state/<seq>/`, `run`
    being whichever of the store's lineage wrote the checkpoint), or None when nothing was. Refused when the
    checkpoint says it saved and the folder is gone: a fork from it would play the agent back where it was asked to
    start it from its own state."""
    exported = checkpoint.agent.exported
    if exported is None or not exported.saved:
        return None
    written = next(
        (e for e in store.events(since=at_seq - 1, until=at_seq) if e.entity == CHECKPOINT and e.seq == at_seq), None
    )
    if written is None:
        raise RunRefused(f"run {store.run_id} has no checkpoint written at seq {at_seq}")
    folder = runs / written.run_id / AGENT_STATE / str(at_seq)
    if not folder.is_dir():
        raise RunRefused(
            f"the checkpoint at seq {at_seq} of run {written.run_id} saved the agent's own state into {folder}, which "
            "is gone: put it back, or remove the run and fork an earlier one"
        )
    return folder.resolve()


@dataclass(frozen=True)
class StateImport:
    """`application.restore.ImportsState`: the agent's program started from its own state saved at the checkpoint,
    its sandbox born at the checkpoint's moment `at`, waited for until it is idle, and held there with a hold."""

    directory: Path
    at: datetime
    sandbox: SandboxClock
    traffic: Traffic | None

    async def start(self, own: OwnProgram) -> None:
        await own.start(
            {
                AGENT_STATE_VARIABLE: str(self.directory),
                STARTS_AT_VARIABLE: str(int(self.at.timestamp() * 1_000_000_000)),
            }
        )
        await self.sandbox.sync(self.at)
        if await self.sandbox.settle(self.traffic) is None:
            raise RunRefused(
                f"the agent's sandbox did not fall idle within its settle limit after the agent started from its own "
                f"state saved at the checkpoint ({self.directory})"
            )
        if self.sandbox.holds:
            # its start ran unheld, as the parent's did; from here it stands at the checkpoint, held as its parent's
            # clock stood there, until the fork's first wake begins
            await self.sandbox.hold()
