"""Where the agent's snapshots live, and the snapshot command. Each command receives MINUTEHAND_SNAPSHOT_DIR.

The restore, which is a sequence and is verified, is `application.restore`."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from minutehand.application.refusals import AgentFailed

SNAPSHOT_DIR_ENV = "MINUTEHAND_SNAPSHOT_DIR"


def wake_dir(state_dir: Path, run_id: str, wake: int) -> Path:
    """Where the agent's state at the end of one wake of one run lives."""
    return state_dir / run_id / f"wake-{wake}"


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
