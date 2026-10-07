"""`minutehand run-all`: every scenario of a folder against the follow-up example, in parallel, each with a port and
a folder of its own filled into the agent file and the command; a summary and a timeline per person; exit 0 when
every verdict is the one its scenario expects, 1 when one differs. And `run --json`, `findings --json` and
`run-all`'s runs read as one shape."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from minutehand.domain.run import VerdictKind
from minutehand.run_all import Batch, free_port
from minutehand.session import Played

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "follow_up"
MINUTEHAND = Path(sys.executable).parent / "minutehand"
COMMAND = ["sh", "-c", f"PORT={{run.port}} RUN_DIR={{run.dir}} exec {sys.executable} {EXAMPLE / 'agent.py'}"]
NEVER_FOLLOW_UP = [{"id": "never_follows_up", "each": "ask", "count": {"follow_ups": {}}, "at_most": 0}]


def _folder(tmp_path: Path, *, silent_expects: str) -> Path:
    """Three scenarios and the agent file beside them: Rosa answers (passed); Rosa is silent and the agent is left
    waiting (`silent_expects`); Rosa is silent and a rule forbids the follow-up the agent makes (failed)."""
    folder = tmp_path / "scenarios"
    folder.mkdir()
    (folder / "agent.yaml").write_text((EXAMPLE / "agent.yaml").read_text().replace("8700", "{run.port}"))
    (folder / "answers.yaml").write_text((EXAMPLE / "scenario.yaml").read_text())
    silent = yaml.safe_load((EXAMPLE / "scenario_silent.yaml").read_text())
    (folder / "silent.yaml").write_text(yaml.safe_dump({**silent, "expect_outcome": silent_expects}))
    strict = {**silent, "name": "offsite_venue_strict", "assess": NEVER_FOLLOW_UP, "expect_outcome": "failed"}
    (folder / "strict.yaml").write_text(yaml.safe_dump(strict))
    (folder / "notes.yaml").write_text("not: a scenario\n")
    return folder


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    return subprocess.run([str(MINUTEHAND), *args], capture_output=True, text=True, env=env, timeout=300)


def test_run_all_plays_every_scenario_in_parallel_and_exits_0_when_each_verdict_is_expected(tmp_path: Path) -> None:
    folder = _folder(tmp_path, silent_expects="unfinished")
    state = tmp_path / "state"

    ran = _cli(
        "run-all", str(folder), "--agent", str(folder / "agent.yaml"), "--state", str(state), "--json", "--", *COMMAND
    )

    assert ran.returncode == 0, ran.stdout + ran.stderr
    batch = Batch.model_validate_json(ran.stdout)
    by_name = {p.scenario: p for p in batch.played}
    assert sorted(by_name) == ["offsite_venue", "offsite_venue_silent", "offsite_venue_strict"], "notes.yaml skipped"
    assert [by_name[n].verdict for n in sorted(by_name)] == [
        VerdictKind.PASSED,
        VerdictKind.UNFINISHED,
        VerdictKind.FAILED,
    ]
    assert all(p.matched for p in batch.played)
    rosa = next(t for t in by_name["offsite_venue"].timeline if t.person == "rosa")
    assert [m.what.split(":")[0] for m in rosa.moments][:2] == ["agent", "they"]
    logs = {Path(p.log).parent for p in batch.played}
    assert len(logs) == 3, "each scenario ran in a folder of its own"
    assert not list(folder.glob(".agent.*")), "the filled agent files were removed"

    said = _cli("run-all", str(folder), "--agent", str(folder / "agent.yaml"), "--state", str(state), "--", *COMMAND)
    assert said.returncode == 0, said.stdout + said.stderr
    assert "  ok      offsite_venue_strict" in said.stdout and "every scenario as expected" in said.stdout
    assert "\n  rosa\n    +0d00h00m  agent: " in said.stdout and "  they: The lakeside hall" in said.stdout


def test_run_all_exits_1_when_a_verdict_differs_from_the_one_its_scenario_expects(tmp_path: Path) -> None:
    folder = _folder(tmp_path, silent_expects="passed")

    ran = _cli(
        "run-all",
        str(folder),
        "--agent",
        str(folder / "agent.yaml"),
        "--state",
        str(tmp_path / "state"),
        "--",
        *COMMAND,
    )

    assert ran.returncode == 1, ran.stdout + ran.stderr
    assert "  DIFFERS offsite_venue_silent" in ran.stdout
    assert "differs from what it expects: offsite_venue_silent" in ran.stdout


def test_run_and_findings_print_one_json_shape(tmp_path: Path) -> None:
    state = tmp_path / "state"
    agent = tmp_path / "agent.yaml"
    port = str(free_port())
    agent.write_text((EXAMPLE / "agent.yaml").read_text().replace("8700", port))
    ran = _cli(
        "run",
        str(EXAMPLE / "scenario.yaml"),
        "--agent",
        str(agent),
        "--state",
        str(state),
        "--json",
        "--",
        "sh",
        "-c",
        f"PORT={port} exec {sys.executable} {EXAMPLE / 'agent.py'}",
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    played = Played.model_validate_json(ran.stdout)
    [outcome] = played.outcomes

    found = _cli("findings", outcome.record.run_id, "--state", str(state), "--json")
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout).keys() == json.loads(ran.stdout).keys()
    assert Played.model_validate_json(found.stdout) == played
