"""Where the agent's snapshots live, and the snapshot command. Each command receives MINUTEHAND_SNAPSHOT_DIR.

The snapshot command fills a working directory, which the run's store keeps (each file once across the run and
its forks) and which is then removed; a restore is handed the snapshot written back out as a plain directory,
removed again once the restore is over. The restore, which is a sequence and is verified, is
`application.restore`."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from minutehand.application.refusals import AgentFailed
from minutehand.domain.agent import StateHooks
from minutehand.domain.storage import AgentSnapshot
from minutehand.ports.store import Store

SNAPSHOT_DIR_ENV = "MINUTEHAND_SNAPSHOT_DIR"

SNAPSHOT_PRUNED = "its snapshot was pruned"
"""Why a checkpoint recorded as restorable no longer is, wherever that is said."""


def wake_dir(state_dir: Path, run_id: str, wake: int) -> Path:
    """Where the snapshot command writes the agent's state at the end of one wake of one run, until the store has
    kept it."""
    return state_dir / run_id / f"wake-{wake}"


def restore_dir(state_dir: Path, run_id: str) -> Path:
    """Where a snapshot is written back out for the restore that starts `run_id`, until that restore is over."""
    return state_dir / run_id / "restoring"


async def take_snapshot(hooks: StateHooks, store: Store, state_dir: Path, wake: int) -> AgentSnapshot:
    """Run the snapshot command, keep what it wrote in the store, remove its directory, and prune to `hooks.keep`."""
    directory = wake_dir(state_dir, store.run_id, wake)
    await run_hook(hooks.snapshot, directory, limit=hooks.step_limit.total_seconds())
    kept = store.keep_snapshot(wake, directory)
    shutil.rmtree(directory)
    if hooks.keep is not None:
        store.prune(hooks.keep)
    return kept


@contextmanager
def materialised(store: Store, snapshot_of: str, wake: int, into: Path) -> Iterator[Path]:
    """One snapshot written out at `into` as the plain directory the snapshot command filled, for as long as the
    block runs."""
    if into.exists():
        shutil.rmtree(into)
    made = [p for p in into.parents if not p.exists()]
    store.materialise(snapshot_of, wake, into)
    try:
        yield into
    finally:
        shutil.rmtree(into, ignore_errors=True)
        for directory in made:  # the directories made for it, innermost first, while nothing else is in them
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()


async def run_hook(argv: list[str], directory: Path, *, limit: float) -> None:
    """Run one hook; a non-zero exit, or running past `limit` seconds, raises with the hook's own error output."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env={**os.environ, SNAPSHOT_DIR_ENV: str(directory)},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        raise AgentFailed(f"{argv[0]} could not be started: {e}") from e
    try:
        _, err = await asyncio.wait_for(process.communicate(), limit)
    except TimeoutError as e:
        process.kill()
        await process.wait()
        raise AgentFailed(f"{argv[0]} did not finish within {limit:g} s, the agent file's step_limit") from e
    if process.returncode != 0:
        raise AgentFailed(f"{argv[0]} exited {process.returncode}: {err.decode(errors='replace').strip()[-2000:]}")
