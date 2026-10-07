"""Commands a scenario runs on the agent's own machine at moments of the run's clock (`Scenario.machine`)."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

from minutehand.domain.scenario import MachineCommand, Model
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.store import Store

NOW_ENV = "MINUTEHAND_NOW"
OUTPUT_KEPT = 4000


class MachineRan(Model):
    """One machine command as it ran: what it was, its exit status and its output, both streams, trimmed."""

    said: str
    argv: list[str]
    exit_code: int
    output: str


async def run_machine(command: MachineCommand, now: datetime) -> MachineRan:
    """Run it with MINUTEHAND_NOW set to `now`. One that cannot start, or outruns its limit, ran and failed."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            env={**os.environ, NOW_ENV: now.isoformat()},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        return MachineRan(said=command.said, argv=command.argv, exit_code=127, output=str(e))
    try:
        out, _ = await asyncio.wait_for(process.communicate(), command.limit.total_seconds())
    except TimeoutError:
        process.kill()
        out, _ = await process.communicate()
        text = out.decode(errors="replace") + "\n(killed after its limit)"
        return MachineRan(said=command.said, argv=command.argv, exit_code=-9, output=text[-OUTPUT_KEPT:])
    code = process.returncode if process.returncode is not None else -1
    return MachineRan(
        said=command.said, argv=command.argv, exit_code=code, output=out.decode(errors="replace")[-OUTPUT_KEPT:]
    )


def record_machine(store: Store, position: int, ran: MachineRan) -> None:
    """The command's run as a change to the agent's machine, by the scenario."""
    store.apply(
        Change(
            entity=EntityRef(provider="minutehand", kind=EntityKind.RECORD, external_id=f"machine:{position}"),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            body=ran.model_dump_json(),
            after=RecordSnapshot(resource="machine", text=f"{ran.said}: exit {ran.exit_code}"),
        )
    )
