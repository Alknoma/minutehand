"""examples/state/postgres through the command line, as a person runs it: `minutehand run` and `minutehand fork` with
`databases:` in the agent file (with `digest: {}` declared), then `minutehand gc --agent` dropping a base nothing
needs and keeping the run's, and `minutehand rm` dropping the run's base with the run and its fork. Each command is
the installed `minutehand` in a process of its own. Run with -m docker."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ports import free_port
from tests.state.postgres import create, digest, exists

pytestmark = pytest.mark.docker

RECIPE = Path(__file__).parents[2] / "examples" / "state" / "postgres"
SQLITE = Path(__file__).parents[2] / "examples" / "state" / "sqlite"
MINUTEHAND = Path(sys.executable).parent / "minutehand"


def minutehand(*argv: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(MINUTEHAND), *argv], cwd=cwd, env=env, capture_output=True, text=True, timeout=120, check=False
    )


def agent_file(tmp_path: Path, server: str, database: str, port: int) -> Path:
    """agent.yaml as written, on ports of the test's own, fronting the test's database, with a digest declared."""
    text = (RECIPE / "agent.yaml").read_text(encoding="utf-8")
    text = text.replace("127.0.0.1:8701", f"127.0.0.1:{port}").replace("127.0.0.1:6543", f"127.0.0.1:{free_port()}")
    text = text.replace("postgres://postgres:secret@127.0.0.1:5432/follow_up", f"{server}/{database}")
    text = text.replace("    base: {kind: template}", "    base: {kind: template}\n    digest: {}")
    written = tmp_path / "agent.yaml"
    written.write_text(text, encoding="utf-8")
    return written


@pytest.mark.timeout(180)
def test_run_and_fork_from_the_command_line_put_the_database_back_and_rm_drops_its_base(
    tmp_path: Path, server: str, database: str
) -> None:
    port = free_port()
    agent = agent_file(tmp_path, server, database, port)
    state = tmp_path / "state"
    env = {**os.environ, "PORT": str(port)}
    program = ["--", sys.executable, str(RECIPE / "agent.py")]

    played = minutehand(
        "run", str(SQLITE / "scenario_silent.yaml"), "--agent", str(agent), "--state", str(state), *program,
        cwd=tmp_path, env=env,
    )  # fmt: skip
    assert played.returncode in (0, 1), played.stdout + played.stderr  # 1: the silent scenario fails its checks
    match = re.search(r"^run (\S+):", played.stdout, re.MULTILINE)
    assert match is not None, played.stdout
    run_id = match.group(1)
    base = f"{database}_mh_{run_id}"
    assert exists(server, base)

    listed = minutehand("checkpoints", run_id, "--state", str(state), cwd=tmp_path, env=env)
    assert listed.returncode == 0, listed.stderr
    found = re.search(r"^seq (\d+), after wake 1:", listed.stdout, re.MULTILINE)
    assert found is not None, listed.stdout
    at = found.group(1)

    forked = minutehand(
        "fork", run_id, "--at", at, "--changes", str(SQLITE / "fork_rosa_answers.yaml"), "--state", str(state),
        *program, cwd=tmp_path, env=env,
    )  # fmt: skip
    assert forked.returncode == 0, forked.stdout + forked.stderr
    assert re.search(
        r"minutehand fork: restore database: database app made again from base \S+, \d+ transaction\(s\) "
        r"\(\d+ statement\(s\)\) replayed and matched, in [\d.]+ s; its digest equals the checkpoint's",
        forked.stderr,
    ), forked.stderr
    assert "minutehand fork: restore verify: the report equals the one at the checkpoint" in forked.stderr
    child = re.search(r"^run (\S+):", forked.stdout, re.MULTILINE)
    assert child is not None and child.group(1) != run_id
    assert "the agent was restored from seq" in forked.stdout and "verified" in forked.stdout

    # A fork alone cannot be removed: its record is in its root's world file.
    refused = minutehand("rm", child.group(1), "--state", str(state), cwd=tmp_path, env=env)
    assert refused.returncode != 0
    assert f"is a fork of run {run_id}" in refused.stdout + refused.stderr

    # A base nothing records (a run directory removed by hand) is an orphan; the run's own base is needed.
    stray = f"{database}_mh_stray"
    create(server, stray)
    swept = minutehand("gc", "--state", str(state), "--agent", str(agent), cwd=tmp_path, env=env)
    assert swept.returncode == 0, swept.stderr
    assert f"base {stray} of database app dropped" in swept.stdout
    assert not exists(server, stray) and exists(server, base)
    held = digest(server, database)

    removed = minutehand("rm", run_id, "--state", str(state), cwd=tmp_path, env=env)
    assert removed.returncode == 0, removed.stderr
    assert "removed 2 runs" in removed.stdout and child.group(1) in removed.stdout
    assert f"base {base} of database app dropped" in removed.stdout
    assert not exists(server, base)
    assert digest(server, database) == held  # the agent's own database is never touched
    assert not (state / "runs" / run_id).exists() and not (state / "runs" / child.group(1)).exists()
