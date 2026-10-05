"""Rewind is honest.

`minutehand fork` from a checkpoint of a finished run: the shipped reference agent (`docs/reference-agent.md`,
with its state hooks) restores, is verified, and the fork says what it changed and where it split; a restore that
brings back another moment is refused and leaves nothing behind; a run that used state Minutehand cannot rewind (an
external emulator, the AWS scheduler fake) refuses a fork after that use, saying why, and allows one before it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.acceptance.support import (
    EMULATOR,
    OWEN,
    PYTHON,
    REFERENCE,
    TOLD_ONLY,
    Finished,
    Reference,
    command_agent,
    minutehand,
    person,
    reference_agent,
    scenario,
    write_yaml,
)

RUN_HEADER = re.compile(r"^run (\w+):", re.MULTILINE)
SILENCED = {"overrides": [{"kind": "person_change", "person": "rosa", "reply": {"kind": "silent"}}]}


def run_id(finished: Finished) -> str:
    found = RUN_HEADER.search(finished.stdout)
    assert found, finished.explain()
    return found.group(1)


def checkpoint(finished: Finished, after_wake: int) -> str:
    found = re.search(rf"seq (\d+), after wake {after_wake}: restorable", finished.stdout)
    assert found, finished.explain()
    return found.group(1)


def reference_parent(tmp_path: Path, ref: Reference) -> Finished:
    """Rosa answers the reference agent's email and it tells Owen: judged only on whether Rosa was asked."""
    parent = minutehand(
        "run", str(REFERENCE / "scenario_lenient.yaml"), "--agent", str(ref.agent_file),
        "--state", str(tmp_path / "state"), *ref.options, "--", *ref.command, env=ref.env, cwd=tmp_path,
    )  # fmt: skip
    assert parent.exit == 0, parent.explain()
    return parent


def fork(tmp_path: Path, ref: Reference, parent: str, at: str, env: dict[str, str]) -> Finished:
    changes = write_yaml(tmp_path / "fork.yaml", SILENCED)
    return minutehand(
        "fork", parent, "--at", at, "--changes", str(changes), "--state", str(tmp_path / "state"),
        *ref.options, "--", *ref.command, env=env, cwd=tmp_path,
    )  # fmt: skip


def runs_listed(tmp_path: Path) -> list[str]:
    listed = minutehand("runs", "--state", str(tmp_path / "state"))
    return [line.split()[0] for line in listed.stdout.splitlines() if line and not line.startswith(" ")]


def test_a_fork_restores_is_verified_and_says_what_it_changed_and_where_it_split(tmp_path: Path) -> None:
    with reference_agent(tmp_path) as ref:
        parent = reference_parent(tmp_path, ref)
        at = checkpoint(parent, after_wake=1)
        child = fork(tmp_path, ref, run_id(parent), at, ref.env)

    assert child.exit in (0, 1, 3), child.explain()
    said = child.stdout
    assert f"the agent was restored from seq {at}, verified" in said
    assert f"forked from {run_id(parent)} at seq {at}: after wake 1" in said
    assert "what it changed:" in said and "Rosa Lind (rosa) never answers from the fork on" in said
    assert "its restore was verified" in said
    assert "against its parent, from the fork on:" in said and "verdict: passed -> " in said
    steps = re.findall(r"^minutehand fork: restore (\w+):", child.stderr, re.MULTILINE)
    assert list(dict.fromkeys(steps)) == ["stop", "restore", "start", "answer", "verify"], child.stderr


def test_a_restore_that_brings_back_another_moment_is_refused_naming_the_difference_and_leaves_no_run(
    tmp_path: Path,
) -> None:
    """`REFERENCE_RESTORE_BUG=next` restores the snapshot of the wrong checkpoint, on purpose."""
    with reference_agent(tmp_path) as ref:
        parent = reference_parent(tmp_path, ref)
        before = runs_listed(tmp_path)
        refused = fork(
            tmp_path, ref, run_id(parent), checkpoint(parent, 1), {**ref.env, "REFERENCE_RESTORE_BUG": "next"}
        )

    assert refused.exit == 2, refused.explain()
    assert "did not bring back the agent as it was at the checkpoint" in refused.stderr
    assert "next_wake:" in refused.stderr, "the refusal names the field that differs"
    assert runs_listed(tmp_path) == before == [run_id(parent)]


def _hooked_command_agent(tmp_path: Path, call: str, **declared: object) -> tuple[Path, dict[str, str]]:
    """The command agent with state hooks that keep nothing: rewinding its own state is not what is tested."""
    agent, env = command_agent(tmp_path, call, **declared)  # type: ignore[arg-type]
    written = yaml.safe_load(agent.read_text())
    written["state"] = {"snapshot": [PYTHON, "-c", "pass"], "restore": [PYTHON, "-c", "pass"], "quiet": "PT0.2S"}
    write_yaml(agent, written)
    return agent, env


def _refused_after_allowed_before(tmp_path: Path, agent: Path, env: dict[str, str]) -> tuple[Finished, Finished]:
    story = write_yaml(tmp_path / "scenario.yaml", scenario("outside_state", person("owen", OWEN, TOLD_ONLY)))
    state = str(tmp_path / "state")
    parent = minutehand("run", str(story), "--agent", str(agent), "--state", state, env=env, cwd=tmp_path)
    assert parent.exit == 0, parent.explain()
    changes = write_yaml(tmp_path / "fork.yaml", {"overrides": [{"kind": "deadline_shift", "by": "P1D"}]})
    forked = [
        minutehand("fork", run_id(parent), "--at", checkpoint(parent, wake), "--changes", str(changes), "--state", state, env=env, cwd=tmp_path)
        for wake in (1, 0)
    ]  # fmt: skip
    return forked[0], forked[1]


def test_a_fork_after_an_external_emulator_was_used_is_refused_naming_it_and_one_before_runs(tmp_path: Path) -> None:
    emulator = {
        "name": "payments",
        "upstream": {"url": "http://127.0.0.1:{port}"},
        "command": [PYTHON, str(EMULATOR), "{port}"],
        "ready": {"kind": "http", "path": "/health", "status": 200},
    }
    forward = {"host": "api.payments.example", "kind": "forward", "emulator": "payments"}
    agent, env = _hooked_command_agent(
        tmp_path, "GET https://api.payments.example/v1/customers", outbound=[forward], emulators=[emulator]
    )
    after, before = _refused_after_allowed_before(tmp_path, agent, env)
    assert after.exit == 2, after.explain()
    assert "cannot rewind the external emulator(s) it had used: payments" in after.stderr
    assert before.exit == 0, before.explain()


def test_a_fork_after_the_scheduler_fake_was_used_is_refused_naming_it_and_one_before_runs(tmp_path: Path) -> None:
    agent, env = _hooked_command_agent(
        tmp_path, "POST https://sqs.us-east-1.amazonaws.com/ Action=CreateQueue&QueueName=jobs&Version=2012-11-05"
    )
    after, before = _refused_after_allowed_before(tmp_path, agent, env)
    assert after.exit == 2, after.explain()
    assert "cannot rewind what the run had built up in aws" in after.stderr
    assert before.exit == 0, before.explain()
