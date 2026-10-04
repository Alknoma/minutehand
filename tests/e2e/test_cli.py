"""The `minutehand` command itself, as a subprocess, wrapping the agent's own command after `--`."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from minutehand.domain.scenario import Silent
from tests.e2e.support import agent_under_test, scenario

MINUTEHAND = Path(sys.executable).parent / "minutehand"


def _dump(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def _cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(MINUTEHAND), *args], capture_output=True, text=True, env=env, timeout=120)


def test_a_forgetful_agent_fails_no_follow_up_and_the_command_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    scenario_file = _dump(tmp_path / "scenario.yaml", yaml.safe_dump(scenario(Silent()).model_dump(mode="json")))
    agent_file = _dump(tmp_path / "agent.yaml", yaml.safe_dump(launched.agent.model_dump(mode="json")))
    state = tmp_path / "state"

    ran = _cli("run", str(scenario_file), "--agent", str(agent_file), "--state", str(state), "--", *launched.command,
               env=dict(os.environ))

    assert ran.returncode == 1, ran.stderr
    out = ran.stdout
    assert "because nothing more was due and the agent asked for no wake" in out
    failed = out[out.index("\nfail ("):out.index("\nscorecard")]
    assert "no_follow_up: wait on sofia expired" in failed
    assert "pattern expiry_on_every_wait: An expiry on every wait." in failed
    assert "expectations met: 1 of 2" in out
    run_id = re.search(r"^run ([0-9a-f]+):", out, re.MULTILINE)
    assert run_id is not None

    again = _cli("findings", run_id.group(1), "--state", str(state), env=dict(os.environ))
    assert again.returncode == 1 and "no_follow_up: wait on sofia expired" in again.stdout
    listed = _cli("runs", "--state", str(state), env=dict(os.environ))
    assert listed.returncode == 0 and f"{run_id.group(1)}  partner_pricing  nothing_pending" in listed.stdout


def test_an_agent_command_that_exits_before_it_listens_is_refused_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    scenario_file = _dump(tmp_path / "scenario.yaml", yaml.safe_dump(scenario(Silent()).model_dump(mode="json")))
    agent_file = _dump(tmp_path / "agent.yaml", yaml.safe_dump(launched.agent.model_dump(mode="json")))

    ran = _cli("run", str(scenario_file), "--agent", str(agent_file), "--state", str(tmp_path / "state"), "--",
               sys.executable, "-c", "import sys; print('no secret for me'); sys.exit(4)", env=dict(os.environ))

    assert ran.returncode == 2
    assert "exited 4 before http://127.0.0.1:" in ran.stderr and "no secret for me" in ran.stderr
