"""`minutehand run-all`: every scenario of a folder against the follow-up example, in parallel, each with a port and
a folder of its own filled into the agent file and the command; a summary and a timeline per person; exit 0 when
every verdict is the one its scenario expects, 1 when one differs. And `run --json`, `findings --json` and
`run-all`'s runs read as one shape."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from minutehand import cli
from minutehand.domain.run import VerdictKind
from minutehand.run_all import Batch, batches
from minutehand.session import Played, reading
from tests.ports import free_port
from tests.support.people import people_environment

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "tests" / "agents" / "follow_up"
MINUTEHAND = Path(sys.executable).parent / "minutehand"
COMMAND = ["sh", "-c", f"PORT={{run.port}} RUN_DIR={{run.dir}} exec {sys.executable} {EXAMPLE / 'agent.py'}"]
NEVER_FOLLOW_UP = [{"id": "never_follows_up", "each": "ask", "count": {"follow_ups": {}}, "at_most": 0}]


def _folder(tmp_path: Path, *, silent_expects: str) -> Path:
    """Three worlds and the agent file beside them: Rosa answers (passed); Rosa is silent, which a world with no
    rule of its own passes (`silent_expects`); Rosa is silent and a rule forbids the follow-up the agent makes
    (failed)."""
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
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")} | people_environment()
    return subprocess.run([str(MINUTEHAND), *args], capture_output=True, text=True, env=env, timeout=300)


def test_run_all_plays_every_scenario_in_parallel_and_exits_0_when_each_verdict_is_expected(tmp_path: Path) -> None:
    folder = _folder(tmp_path, silent_expects="passed")
    state = tmp_path / "state"

    ran = _cli(
        "run-all", str(folder), "--agent", str(folder / "agent.yaml"), "--state", str(state), "--json", "--", *COMMAND
    )

    assert ran.returncode == 0, ran.stdout + ran.stderr
    batch = Batch.model_validate_json(ran.stdout)
    by_name = {p.scenario: p for p in batch.played}
    assert sorted(by_name) == ["offsite_venue", "offsite_venue_silent", "offsite_venue_strict"], "notes.yaml skipped"
    assert [by_name[n].samples[0].verdict for n in sorted(by_name)] == [
        VerdictKind.PASSED,
        VerdictKind.PASSED,
        VerdictKind.FAILED,
    ]
    assert all(p.matched for p in batch.played)
    rosa = next(t for t in by_name["offsite_venue"].samples[0].timeline if t.person == "rosa")
    assert [m.what.split(":")[0] for m in rosa.moments][:2] == ["agent", "they"]
    logs = {Path(p.samples[0].log).parent for p in batch.played}
    assert len(logs) == 3, "each scenario ran in a folder of its own"
    assert not list(folder.glob(".agent.*")), "the filled agent files were removed"

    said = _cli("run-all", str(folder), "--agent", str(folder / "agent.yaml"), "--state", str(state), "--", *COMMAND)
    assert said.returncode == 0, said.stdout + said.stderr
    assert "  ok      offsite_venue_strict" in said.stdout and "every scenario as expected" in said.stdout
    assert "\n  rosa\n    +0d00h00m  agent: " in said.stdout and "  they: The lakeside hall" in said.stdout


def test_run_all_exits_1_when_a_verdict_differs_from_the_one_its_scenario_expects(tmp_path: Path) -> None:
    folder = _folder(tmp_path, silent_expects="failed")

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


def _templated_agent(folder: Path, *, also: str = "") -> Path:
    """The reference agent's file with every port it names, its inbox's among them, written as `{run.port}`."""
    text = (ROOT / "tests" / "agents" / "reference_agent" / "agent.yaml").read_text()
    for port in sorted(set(re.findall(r"127\.0\.0\.1:(\d+)", text))):
        text = text.replace(f"127.0.0.1:{port}", "127.0.0.1:{run.port}")
    agent = folder / "agent.yaml"
    agent.write_text(text + also)
    return agent


def test_run_all_reads_an_agent_file_whose_inbox_urls_name_the_run_port(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "scenarios"
    folder.mkdir()
    agent = _templated_agent(folder)
    assert "/approvals?approver=" in agent.read_text() and "{run.port}/approvals" in agent.read_text()

    exited = cli.main(["run-all", str(folder), "--agent", str(agent), "--state", str(tmp_path / "state")])

    said = capsys.readouterr().err
    assert "holds no scenario file" in said, f"the agent file was read past its placeholders: {said}"
    assert exited == 2


def test_run_all_still_refuses_an_agent_file_that_is_wrong_besides_its_placeholders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "scenarios"
    folder.mkdir()
    agent = _templated_agent(folder, also="unknown_field: true\n")

    exited = cli.main(["run-all", str(folder), "--agent", str(agent), "--state", str(tmp_path / "state")])

    said = capsys.readouterr().err
    assert "unknown_field" in said and "holds no scenario file" not in said
    assert exited == 2


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


def test_run_all_samples_each_scenario_under_seeds_from_a_base_and_counts_each_verdict(tmp_path: Path) -> None:
    folder = _folder(tmp_path, silent_expects="passed")
    strict = yaml.safe_load((folder / "strict.yaml").read_text())
    (folder / "strict.yaml").unlink()
    rated = {**strict, "name": "offsite_venue_rated", "expect_outcome": {"passed": ">= 0.5"}}
    (folder / "rated.yaml").write_text(yaml.safe_dump(rated))

    ran = _cli(
        "run-all",
        str(folder),
        "--agent",
        str(folder / "agent.yaml"),
        "--state",
        str(tmp_path / "state"),
        "--samples",
        "3",
        "--seed",
        "100",
        "--json",
        "--",
        *COMMAND,
    )

    assert ran.returncode == 1, ran.stdout + ran.stderr
    printed = Batch.model_validate_json(ran.stdout)
    assert printed.samples == 3 and batches(tmp_path / "state") == [printed], "kept beside its runs for the viewer"
    by_name = {p.scenario: p for p in printed.played}
    answers = by_name["offsite_venue"]
    assert [s.seed for s in answers.samples] == [100, 101, 102]
    assert answers.counts["passed"] == 3 and answers.failing_seeds == [] and answers.matched
    assert by_name["offsite_venue_silent"].counts["passed"] == 3 and by_name["offsite_venue_silent"].matched
    rated_played = by_name["offsite_venue_rated"]
    assert rated_played.counts["failed"] == 3 and rated_played.failing_seeds == [100, 101, 102]
    assert not rated_played.matched, "none of three passed, and at least half must"

    said = _cli(
        "run-all",
        str(folder),
        "--agent",
        str(folder / "agent.yaml"),
        "--state",
        str(tmp_path / "state"),
        "--samples",
        "2",
        "--seed",
        "7",
        "--",
        *COMMAND,
    )
    assert "  DIFFERS offsite_venue_rated" in said.stdout and "failing seeds: 7, 8" in said.stdout
    assert "minutehand run <scenario> --seed <seed>" in said.stdout


def test_run_seed_is_recorded_printed_and_reproduces_every_reply_moment(tmp_path: Path) -> None:
    scenario = yaml.safe_load((EXAMPLE / "scenario.yaml").read_text())
    rosa = next(p for p in scenario["people"] if p["key"] == "rosa")
    rosa["reply_within"] = {"min": "PT6H", "max": "PT40H"}
    path = tmp_path / "windowed.yaml"
    path.write_text(yaml.safe_dump(scenario))
    state = tmp_path / "state"
    landed: list[list[datetime]] = []
    for n, seed in enumerate(("11", "11", "12")):
        port = str(free_port())
        agent = tmp_path / f"agent-{n}.yaml"
        agent.write_text((EXAMPLE / "agent.yaml").read_text().replace("8700", port))
        ran = _cli(
            "run", str(path), "--agent", str(agent), "--state", str(state), "--seed", seed, "--",
            "sh", "-c", f"PORT={port} exec {sys.executable} {EXAMPLE / 'agent.py'}",
        )  # fmt: skip
        assert ran.returncode in (0, 1), ran.stdout + ran.stderr
        first = ran.stdout.splitlines()[0]
        assert first.endswith(f"(seed {seed})"), first
        run_id = first.split()[1].rstrip(":")
        assert json.loads((state / "runs" / run_id / "record.json").read_text())["seed"] == int(seed)
        with reading(state, run_id) as world:
            landed.append([r.at for r in world.replies()])
    assert landed[0] == landed[1] and landed[0] != landed[2], "the same seed lands every reply alike; another does not"
