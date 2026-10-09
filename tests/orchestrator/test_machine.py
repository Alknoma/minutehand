"""Commands a scenario runs on the agent's own machine at moments of the run's clock (`Scenario.machine`)."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand.application.dues import due_entries
from minutehand.domain.clock import DueClosed, DueSource
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import MachineCommand, Seed
from minutehand.domain.world import Actor, RecordSnapshot
from tests.orchestrator.rig import T0, Rig, scenario


def drops(file: Path) -> list[str]:
    """A command that writes the simulated moment it was run at into `file`."""
    script = f"import os, pathlib; pathlib.Path({str(file)!r}).write_text(os.environ['MINUTEHAND_NOW'])"
    return [sys.executable, "-c", script]


async def test_a_machine_command_runs_at_its_moment_knowing_it_and_wakes_nobody(rig: Rig, tmp_path: Path) -> None:
    grown = tmp_path / "cache-grew.txt"
    command = MachineCommand(after=timedelta(days=3), argv=drops(grown), said="the cache grows by 4 GB")
    record, store, _ = await rig.run(scenario(tom_finishes=False, machine=[command]), rig.agent("ask_silent"))

    assert grown.read_text() == (T0 + timedelta(days=3)).isoformat()
    assert [w.sim_time for w in record.wakes] == [T0], "the agent is not woken: it finds the change when it looks"
    [ran] = [e for e in store.events() if isinstance(e.after, RecordSnapshot) and e.after.resource == "machine"]
    assert ran.actor is Actor.SCENARIO and ran.sim_time == T0 + timedelta(days=3)
    assert isinstance(ran.after, RecordSnapshot) and ran.after.text == "the cache grows by 4 GB: exit 0"
    [entry] = [e for e in due_entries(store) if e.source is DueSource.MACHINE]
    assert (entry.closed, entry.closed_at) == (DueClosed.FIRED, T0 + timedelta(days=3))


async def test_a_machine_command_that_fails_stops_the_run_as_the_environments_failure(rig: Rig) -> None:
    broken = MachineCommand(
        after=timedelta(hours=2),
        argv=[sys.executable, "-c", "import sys; print('disk full'); sys.exit(3)"],
        said="a download appears",
    )
    record, _, _ = await rig.run(scenario(tom_finishes=False, machine=[broken]), rig.agent("keep_waking"))

    assert record.stop is StopReason.ENVIRONMENT_FAILED
    assert record.failure is not None and "'a download appears' exited 3: disk full" in record.failure
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=1)], "nothing after the failure"


def test_a_seed_with_machine_commands_is_refused() -> None:
    written = {
        "people": [{"key": "owen", "name": "Owen", "email": "owen@example.com", "reply": {"kind": "silent"}}],
        "machine": [{"after": "PT1H", "argv": ["true"], "said": "nothing"}],
    }
    with pytest.raises(ValidationError, match="machine commands run at moments of the run loop's clock"):
        Seed.model_validate(written)
