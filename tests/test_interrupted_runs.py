"""A run stopped before it ended by itself is kept, and carried on from its last checkpoint with the agent's code
changed: `minutehand run` under SIGTERM (`docker stop`), and `minutehand close` for one whose process was killed.

The agent's sandbox here is two shell commands over a file holding its clock, as the patched gVisor's are over its
own: the agent's command writes the moment it is born at (`MINUTEHAND_STARTS_AT_NS`), `advance` moves it, `deadlines`
reads it, and `export_state` saves it as the agent's own state. The agent says, each time it starts, which saved
state it was handed (`MINUTEHAND_AGENT_STATE`) and what it found there. Nothing wakes it but three due records in
its memory, a day apart; from the second day on, each read of its sandbox takes a fifth of a second, so a signal sent
then lands in a wake.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand import cli, session
from minutehand.adapters.store import sharing
from minutehand.application.agent_state import AGENT_STATE
from minutehand.application.checkpoint import Remembered, checkpoints
from minutehand.application.restore import RestoreStep
from minutehand.domain.checks import Severity
from minutehand.domain.run import StopReason, VerdictKind

DAY_TWO = int(datetime(2026, 8, 26, 9, tzinfo=UTC).timestamp()) * 1_000_000_000

SCENARIO = """\
name: carried_on
starts_at: "2026-08-24T09:00:00Z"
runs_for: P4D
people:
  - {key: owen, name: Owen, email: owen@example.com, reply: {kind: silent}}
memory:
  - {collection: due, key: jobs/a, value: {state: open, run_at: "2026-08-25T09:00:00+00:00"}}
  - {collection: due, key: jobs/b, value: {state: open, run_at: "2026-08-26T09:00:00+00:00"}}
  - {collection: due, key: jobs/c, value: {state: open, run_at: "2026-08-27T09:00:00+00:00"}}
"""

AGENT = """\
name: resumable
sandbox:
  deadlines: [sh, -c, 'n=$(cat {clock}); [ "$n" -ge {day_two} ] && sleep 0.2; echo "{{\\"idle\\": true, \\"now_ns\\": $n}}"']
  advance: [sh, -c, 'echo $(( $(cat {clock}) + $1 )) > {clock}', advance, "{{nanoseconds}}"]
  export_state: [sh, -c, 'cp {clock} "$1/clock"', export, "{{dir}}"]
due:
  - {{collection: due, prefix: "jobs/", where: {{state: open}}, field: run_at}}
"""

PROGRAM = """\
echo $$ > {pid}
printf '%s' "$MINUTEHAND_STARTS_AT_NS" > {clock}
echo "{version} state=${{MINUTEHAND_AGENT_STATE:-none}} found=$(cat "$MINUTEHAND_AGENT_STATE/clock" 2>/dev/null)" >> {seen}
exec sleep 600
"""


class _World:
    """The files of one test's agent, and the commands that run it."""

    def __init__(self, tmp_path: Path) -> None:
        self.state = tmp_path / "state"
        self.clock, self.seen, self.pid = tmp_path / "clock", tmp_path / "seen", tmp_path / "pid"
        self.scenario, self.agent = tmp_path / "scenario.yaml", tmp_path / "agent.yaml"
        self.scenario.write_text(SCENARIO)
        self.agent.write_text(AGENT.format(clock=self.clock, day_two=DAY_TWO))
        self.programs = tmp_path

    def program(self, version: str) -> list[str]:
        script = self.programs / f"agent_{version}.sh"
        script.write_text(PROGRAM.format(pid=self.pid, clock=self.clock, seen=self.seen, version=version))
        return ["sh", str(script)]

    def run(self) -> subprocess.Popen[str]:
        command = [sys.executable, "-m", "minutehand", "run", str(self.scenario), "--agent", str(self.agent)]
        return subprocess.Popen(
            [*command, "--state", str(self.state), "--", *self.program("v1")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def on_day_two(self, run: subprocess.Popen[str]) -> None:
        """Wait until the run's clock has reached the second day: the third wake has begun."""
        give_up = time.monotonic() + 60
        while time.monotonic() < give_up and run.poll() is None:
            with contextlib.suppress(OSError, ValueError):
                if int(self.clock.read_text()) >= DAY_TWO:
                    return
            time.sleep(0.005)
        raise AssertionError(f"the run never reached its second day: {run.communicate()}")

    def run_id(self) -> str:
        [run_id] = [d.name for d in (self.state / session.RUNS).iterdir()]
        return run_id

    def stop_agent(self) -> None:
        """The agent's program and its group, which a killed Minutehand could not stop."""
        with contextlib.suppress(OSError, ValueError):
            os.killpg(int(self.pid.read_text()), signal.SIGKILL)


@pytest.mark.timeout(180)
def test_a_run_stopped_by_sigterm_is_kept_and_carried_on_from_its_saved_state_with_changed_code(
    tmp_path: Path,
) -> None:
    world = _World(tmp_path)
    run = world.run()
    try:
        world.on_day_two(run)
        run.send_signal(signal.SIGTERM)
        out, err = run.communicate(timeout=60)
    finally:
        run.kill()
        world.stop_agent()
    assert run.returncode == 3, f"exit {run.returncode}:\n{out}\n{err}"
    assert "Traceback" not in err and "Exception ignored" not in err, err
    run_id = world.run_id()
    outcome = session.load(world.state, run_id)
    assert outcome.record.stop is StopReason.INTERRUPTED
    assert outcome.result.verdict.kind is VerdictKind.UNFINISHED
    assert [w.sim_time.day for w in outcome.record.wakes] == [24, 25], "the run went on past the signal"
    points = session.checkpoints_of(world.state, run_id)
    last = points[-1]
    assert last.wake == 2 and isinstance(last.agent, Remembered)
    assert last.agent.exported is not None and last.agent.exported.saved
    assert outcome.record.failure is not None and "SIGTERM" in outcome.record.failure
    assert outcome.record.failure in outcome.result.notes
    saved = session.run_dir(world.state, run_id) / AGENT_STATE / str(last.seq)
    assert (saved / "clock").read_text().strip() == str(_now_ns(world, run_id, last.seq))

    carried = subprocess.run(
        [
            *(sys.executable, "-m", "minutehand", "replay", run_id, "--at", str(last.seq)),
            *("--state", str(world.state), "--", *world.program("v2")),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    world.stop_agent()
    assert carried.returncode == 0, f"{carried.stdout}\n{carried.stderr}"
    [child] = [d.name for d in (world.state / session.RUNS).iterdir() if d.name != run_id]
    restored = session.restore_of(world.state, child)
    assert restored is not None and restored.agent_state == str(saved.resolve())
    assert RestoreStep.PLAYBACK not in [s.step for s in restored.steps], "the fork played the agent back"
    assert restored.program_changes, "the agent's program changed, and the fork did not say so"
    assert world.seen.read_text().splitlines() == [
        "v1 state=none found=",
        f"v2 state={saved.resolve()} found={_now_ns(world, run_id, last.seq)}",
    ]
    kept = session.load(world.state, child)
    assert kept.record.stop is StopReason.WINDOW_ENDED
    assert [w.sim_time.day for w in kept.record.wakes] == [24, 25, 26, 27]


@pytest.mark.timeout(180)
def test_close_keeps_a_killed_run_and_refuses_one_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _World(tmp_path)
    run = world.run()
    try:
        world.on_day_two(run)
        run_id = world.run_id()
        assert cli.main(["close", run_id, "--state", str(world.state)]) == 2
        assert "is being written now" in capsys.readouterr().err, "a run still running was closed under its writer"
        run.kill()  # SIGKILL: nothing of Minutehand's runs after it
        run.communicate(timeout=60)
    finally:
        run.kill()
        world.stop_agent()
    with pytest.raises(session.RunRefused, match=f"run {run_id} under .* has kept no record"):
        session.load(world.state, run_id)
    monkeypatch.setattr(sharing, "STALE", 0.0)  # its writer's mark, refreshed no more, taken as gone at once
    assert cli.main(["close", run_id, "--state", str(world.state), "--json"]) == 3
    outcome = session.load(world.state, run_id)
    assert outcome.record.stop is StopReason.INTERRUPTED
    assert outcome.record.failure is not None and "minutehand close" in outcome.record.failure
    assert [w.index for w in outcome.record.wakes] == [1, 2]
    last = session.checkpoints_of(world.state, run_id)[-1]
    assert last.wake == 2 and isinstance(last.agent, Remembered)
    assert f"seq {last.seq}" in outcome.record.failure
    assert not any(f.severity is Severity.ERROR for f in outcome.result.findings)
    assert cli.main(["close", run_id, "--state", str(world.state)]) == 2, "a kept run was closed again"


def _now_ns(world: _World, run_id: str, seq: int) -> int:
    """The moment of the checkpoint at `seq`, as the sandbox's clock read it."""
    with session.reading(world.state, run_id) as kept:
        return int(checkpoints(kept)[seq].now.timestamp()) * 1_000_000_000
