"""The agent's own snapshot and restore commands. Each receives MINUTEHAND_SNAPSHOT_DIR."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from minutehand.application.refusals import AgentFailed

SNAPSHOT_DIR_ENV = "MINUTEHAND_SNAPSHOT_DIR"


def wake_dir(state_dir: Path, run_id: str, wake: int) -> Path:
    """Where the agent's state at the end of one wake of one run lives."""
    return state_dir / run_id / f"wake-{wake}"


async def run_hook(argv: list[str], directory: Path) -> None:
    """Run one hook; a non-zero exit raises with the hook's own error output."""
    directory.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        *argv,
        env={**os.environ, SNAPSHOT_DIR_ENV: str(directory)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await process.communicate()
    if process.returncode != 0:
        raise AgentFailed(f"{argv[0]} exited {process.returncode}: {err.decode(errors='replace').strip()[-2000:]}")
