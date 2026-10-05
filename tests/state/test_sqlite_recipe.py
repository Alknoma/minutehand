"""examples/state/sqlite, end to end: an agent whose state is a SQLite file, forked and restored, and a restore
that silently did nothing caught by the verify step."""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.checkpoint import Restorable
from minutehand.application.files import load_agent, load_fork, load_scenario
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import RestoreStep
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.run import StopReason
from tests.e2e.support import free_port

RECIPE = Path(__file__).parents[2] / "examples" / "state" / "sqlite"
MINUTEHAND = Path(sys.executable).parent / "minutehand"


def recipe_agent(port: int, *, restore: list[str] | None = None) -> AgentUnderTest:
    """agent.yaml as written, on a port of the test's own, its hooks run by this Python from the recipe."""
    agent = load_agent(RECIPE / "agent.yaml")
    hooks = [sys.executable, str(RECIPE / "hooks.py")]
    assert agent.state is not None
    text = agent.model_dump_json().replace("127.0.0.1:8700", f"127.0.0.1:{port}")
    moved = AgentUnderTest.model_validate_json(text)
    assert moved.state is not None
    return moved.model_copy(
        update={
            "state": moved.state.model_copy(
                update={"snapshot": [*hooks, "snapshot"], "restore": restore or [*hooks, "restore"]}
            )
        }
    )


@pytest.fixture
def port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    chosen = free_port()
    monkeypatch.setenv("PORT", str(chosen))
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "agent.db"))
    return chosen


def command() -> list[str]:
    return [sys.executable, str(RECIPE / "agent.py")]


def runs_in(world: Path) -> list[str]:
    db = sqlite3.connect(world)
    try:
        return [r[0] for r in db.execute("SELECT run_id FROM run ORDER BY rowid")]
    finally:
        db.close()


async def test_the_sqlite_recipe_forks_from_the_end_of_a_wake_with_its_restore_verified(
    tmp_path: Path, port: int
) -> None:
    state = tmp_path / "state"
    agent = recipe_agent(port)
    [parent] = await session.play(load_scenario(RECIPE / "scenario_silent.yaml"), agent, state=state, command=command())
    assert parent.record.stop is StopReason.NOTHING_PENDING
    points = session.fork_points(state, parent.record.run_id)
    assert [p.wake for p in points] == [0, 1, 2, 3, 3]
    assert all(isinstance(p.agent, Restorable) for p in points)
    after_ask = points[1]
    assert isinstance(after_ask.agent, Restorable) and after_ask.agent.report is not None
    [waiting] = after_ask.agent.report.commitments or []
    assert (waiting.key, waiting.status.value) == ("rosa_confirms_venue", "open")

    said: list[str] = []
    changes = load_fork(RECIPE / "fork_rosa_answers.yaml", parent_run=parent.record.run_id, at_seq=after_ask.seq)
    [child] = await session.fork(parent.record.run_id, changes, state=state, command=command(), progress=said.append)

    assert child.record.stop is StopReason.AGENT_DONE
    restored = session.restore_of(state, child.record.run_id)
    assert restored is not None and restored.verified and restored.checkpoint_seq == after_ask.seq
    assert [s.step for s in restored.steps] == [
        RestoreStep.STOP,
        RestoreStep.RESTORE,
        RestoreStep.START,
        RestoreStep.ANSWER,
        RestoreStep.VERIFY,
    ]
    assert "restored from" in next(s.output for s in restored.steps if s.step is RestoreStep.RESTORE)
    assert said[0].startswith("stop: stopping the agent's command")
    assert said[-1] == "verify: the report equals the one at the checkpoint"


async def test_a_restore_that_silently_did_nothing_is_refused_field_by_field_and_leaves_no_run(
    tmp_path: Path, port: int
) -> None:
    state = tmp_path / "state"
    [parent] = await session.play(
        load_scenario(RECIPE / "scenario_silent.yaml"), recipe_agent(port), state=state, command=command()
    )
    after_ask = session.fork_points(state, parent.record.run_id)[1]
    world = session.run_dir(state, parent.record.run_id) / session.WORLD
    directories = sorted(p.name for p in (state / session.RUNS).iterdir())
    does_nothing = recipe_agent(port, restore=[sys.executable, "-c", "print('restored, honestly')"])
    session.run_dir(state, parent.record.run_id).joinpath(session.AGENT).write_text(does_nothing.model_dump_json())

    changes = load_fork(RECIPE / "fork_rosa_answers.yaml", parent_run=parent.record.run_id, at_seq=after_ask.seq)
    with pytest.raises(RunRefused) as refused:
        await session.fork(parent.record.run_id, changes, state=state, command=command())

    message = str(refused.value)
    assert f"did not bring back the agent as it was at the checkpoint at seq {after_ask.seq}" in message
    # The parent ran on past the checkpoint: its last report names no next wake, where the checkpoint's did.
    assert re.search(r"next_wake: 2026-08-26T09:00:00\+00:00 at the checkpoint, none after", message), message
    assert runs_in(world) == [parent.record.run_id]
    assert sorted(p.name for p in (state / session.RUNS).iterdir()) == directories


def test_the_command_line_shows_restorable_checkpoints_and_names_each_restore_step(tmp_path: Path, port: int) -> None:
    state = tmp_path / "state"
    agent_file = tmp_path / "agent.yaml"
    agent_file.write_text(recipe_agent(port).model_dump_json())
    env = dict(os.environ)

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(MINUTEHAND), *args, "--state", str(state), "--", *command()],
            capture_output=True,
            text=True,
            env=env,
            timeout=50,
        )

    ran = cli("run", str(RECIPE / "scenario_silent.yaml"), "--agent", str(agent_file))
    assert ran.returncode == 1, ran.stderr
    found = re.search(r"^run ([0-9a-f]+):", ran.stdout, re.MULTILINE)
    assert found is not None
    after_ask = re.search(r"^  seq (\d+), after wake 1: restorable$", ran.stdout, re.MULTILINE)
    assert after_ask is not None, ran.stdout

    forked = cli(
        "fork", found.group(1), "--at", after_ask.group(1), "--changes", str(RECIPE / "fork_rosa_answers.yaml")
    )
    assert forked.returncode == 0, forked.stderr
    steps = [
        line.split("restore ", 1)[1].split(":", 1)[0] for line in forked.stderr.splitlines() if " restore " in line
    ]
    assert list(dict.fromkeys(steps)) == ["stop", "restore", "start", "answer", "verify"]
    assert f"the agent was restored from seq {after_ask.group(1)}, verified" in forked.stdout

    listed = subprocess.run(
        [str(MINUTEHAND), "runs", "--state", str(state)], capture_output=True, text=True, env=env, timeout=30
    )
    assert listed.stdout.count("  restorable at seq ") == 2, listed.stdout


def test_a_run_that_keeps_one_snapshot_lists_the_pruned_as_not_restorable_and_refuses_a_fork_from_one(
    tmp_path: Path, port: int
) -> None:
    state = tmp_path / "state"
    agent = recipe_agent(port)
    assert agent.state is not None
    agent_file = tmp_path / "agent.yaml"
    agent_file.write_text(
        agent.model_copy(update={"state": agent.state.model_copy(update={"keep": 1})}).model_dump_json()
    )
    env = dict(os.environ)

    def cli(*args: str, agent_command: bool = False) -> subprocess.CompletedProcess[str]:
        tail = ["--", *command()] if agent_command else []
        return subprocess.run(
            [str(MINUTEHAND), *args, "--state", str(state), *tail], capture_output=True, text=True, env=env, timeout=50
        )

    ran = cli("run", str(RECIPE / "scenario_silent.yaml"), "--agent", str(agent_file), agent_command=True)
    assert ran.returncode == 1, ran.stderr
    found = re.search(r"^run ([0-9a-f]+):", ran.stdout, re.MULTILINE)
    assert found is not None
    run_id = found.group(1)
    assert not list(session.run_dir(state, run_id).glob("wake-*")), "the snapshot directories were kept and removed"

    listed = cli("checkpoints", run_id)
    assert listed.returncode == 0, listed.stderr
    lines = listed.stdout.splitlines()
    assert re.fullmatch(r"seq \d+, after wake 0: restorable; snapshot of 1 files, .*", lines[0]), lines
    assert all(line.endswith("not restorable: its snapshot was pruned") for line in lines[1:3]), lines
    assert re.fullmatch(r"seq \d+, after wake 3: restorable; snapshot of 1 files, .*", lines[-1]), lines
    pruned_seq = re.match(r"seq (\d+)", lines[1])
    assert pruned_seq is not None

    refused = cli("pin", run_id, pruned_seq.group(1))
    assert refused.returncode == 2 and "already pruned" in refused.stderr
    forked = cli(
        "fork",
        run_id,
        "--at",
        pruned_seq.group(1),
        "--changes",
        str(RECIPE / "fork_rosa_answers.yaml"),
        agent_command=True,
    )
    assert forked.returncode == 2 and "is not restorable: its snapshot was pruned" in forked.stderr, forked.stderr

    first_seq = re.match(r"seq (\d+)", lines[0])
    assert first_seq is not None
    pinned = cli("pin", run_id, first_seq.group(1))
    assert pinned.returncode == 0 and "pinned: pruning keeps it" in pinned.stdout
    assert cli("checkpoints", run_id).stdout.splitlines()[0].endswith("; pinned")

    runs = cli("runs")
    assert re.search(r"on disk: .* of rows, .* of bodies it alone holds, .* of snapshots it alone holds", runs.stdout)
    collected = cli("gc")
    assert collected.returncode == 0 and re.fullmatch(
        r"freed 0 stored bodies \(0 bytes\) and 0 snapshot files \(0 bytes\) across 1 world files\n", collected.stdout
    ), collected.stdout
