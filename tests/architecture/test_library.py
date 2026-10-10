"""Every library scenario, written out with a team's values as `minutehand scenarios new` writes it, run through the
installed command against the two example agents and each of their behaviours: what each scenario catches, and what
it lets through.

The reference agent (tests/agents/reference_agent) is filled in as its team would fill it: the venue's contact it emails as
the person, and Nadia as the approver; its own work is in its own configuration. The follow-up example
(tests/agents/follow_up) asks Rosa in Slack. Each scenario is a world with no rule of its own, so each cell is the exit
code and the checks of the automatic assessment that failed; `docs/scenarios.md` prints the same table.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.library import entries, entry, write
from minutehand.domain.checks import FindingKind
from minutehand.domain.library import TeamValues, Who
from tests.architecture.support import MINUTEHAND, ROOT, Rig, free_port
from tests.support.people import people_environment

REFERENCE_TEAM = TeamValues(
    person=Who.written("Rosa Lind <rosa@lakeside.example>"),
    other=Who.written("Nadia Ek <nadia@example.com>"),
    knows="Yes, that is confirmed. The reference is RF-4410.",
    credential_env="REFERENCE_APPROVER_TOKEN",
)
FOLLOW_UP_TEAM = TeamValues(person=Who.written("Rosa Lind <rosa@example.com>"))
FOLLOW_UP = ROOT / "tests" / "agents" / "follow_up"
APPROVALS = ("approval_rejected", "approver_never_decides")
"""The scenarios that need the reference agent's approvals (REFERENCE_APPROVER, its inbox declared)."""

NONE: frozenset[str] = frozenset()
DUPLICATE = frozenset({"duplicate"})

REFERENCE: dict[tuple[str, str], tuple[int, frozenset[str]]] = {
    ("approval_rejected", "diligent"): (1, DUPLICATE),
    ("approval_rejected", "forgetful"): (0, NONE),
    ("approval_rejected", "heedless"): (1, DUPLICATE),
    ("approval_rejected", "liar"): (0, NONE),
    ("approval_rejected", "nagging"): (1, DUPLICATE),
    ("approver_never_decides", "diligent"): (1, DUPLICATE),
    ("approver_never_decides", "forgetful"): (0, NONE),
    ("approver_never_decides", "heedless"): (1, DUPLICATE),
    ("approver_never_decides", "liar"): (0, NONE),
    ("approver_never_decides", "nagging"): (1, DUPLICATE),
    ("person_answers_late", "diligent"): (0, NONE),
    ("person_answers_late", "forgetful"): (0, NONE),
    ("person_answers_late", "liar"): (0, NONE),
    ("person_answers_late", "nagging"): (1, DUPLICATE),
    ("person_answers_when_reminded", "diligent"): (0, NONE),
    ("person_answers_when_reminded", "forgetful"): (0, NONE),
    ("person_answers_when_reminded", "liar"): (0, NONE),
    ("person_answers_when_reminded", "nagging"): (0, NONE),
    ("person_away_with_delegate", "diligent"): (0, NONE),
    ("person_away_with_delegate", "forgetful"): (0, NONE),
    ("person_away_with_delegate", "liar"): (0, NONE),
    ("person_away_with_delegate", "nagging"): (0, NONE),
    ("person_goes_quiet", "diligent"): (1, DUPLICATE),
    ("person_goes_quiet", "forgetful"): (0, NONE),
    ("person_goes_quiet", "liar"): (0, NONE),
    ("person_goes_quiet", "nagging"): (1, DUPLICATE),
    ("planned_wake_dropped", "diligent"): (0, NONE),
    ("planned_wake_dropped", "forgetful"): (0, NONE),
    ("planned_wake_dropped", "liar"): (0, NONE),
    ("planned_wake_dropped", "nagging"): (0, NONE),
    ("planned_wake_late", "diligent"): (1, DUPLICATE),
    ("planned_wake_late", "forgetful"): (0, NONE),
    ("planned_wake_late", "liar"): (0, NONE),
    ("planned_wake_late", "nagging"): (1, DUPLICATE),
    ("planned_wake_twice", "diligent"): (0, NONE),
    ("planned_wake_twice", "forgetful"): (0, NONE),
    ("planned_wake_twice", "liar"): (0, NONE),
    ("planned_wake_twice", "nagging"): (0, NONE),
}
"""(scenario, REFERENCE_BEHAVIOUR) -> (exit code, failed checks), judged by the automatic assessment alone: each
scenario is a world, with no rule of its own. `someone_else_writes_while_waiting` and `date_moves_earlier` need a
messaging provider, which the reference agent (email only) does not use."""

FOLLOW_UP_CELLS: dict[tuple[str, str], tuple[int, frozenset[str]]] = {
    ("date_moves_earlier", "diligent"): (0, NONE),
    ("date_moves_earlier", "forgetful"): (0, NONE),
    ("person_answers_late", "diligent"): (0, NONE),
    ("person_answers_late", "forgetful"): (0, NONE),
    ("person_answers_when_reminded", "diligent"): (0, NONE),
    ("person_answers_when_reminded", "forgetful"): (0, NONE),
    ("person_away_with_delegate", "diligent"): (0, NONE),
    ("person_away_with_delegate", "forgetful"): (0, NONE),
    ("person_goes_quiet", "diligent"): (0, NONE),
    ("person_goes_quiet", "forgetful"): (0, NONE),
    ("planned_wake_dropped", "diligent"): (0, NONE),
    ("planned_wake_dropped", "forgetful"): (0, NONE),
    ("planned_wake_late", "diligent"): (0, NONE),
    ("planned_wake_late", "forgetful"): (0, NONE),
    ("planned_wake_twice", "diligent"): (0, NONE),
    ("planned_wake_twice", "forgetful"): (0, NONE),
    ("someone_else_writes_while_waiting", "diligent"): (0, NONE),
    ("someone_else_writes_while_waiting", "forgetful"): (0, NONE),
}
"""(scenario, AGENT_BEHAVIOUR) -> (exit code, failed checks). The approval scenarios need an agent with an inbox."""


def failed(state: Path, run_id: str) -> frozenset[str]:
    found = session.load(state, run_id).result.findings
    return frozenset(f.check for f in found if f.kind is FindingKind.FAIL)


def test_every_library_scenario_is_run_against_both_examples() -> None:
    """A scenario added to the library without a row here would go unproven."""
    names = {e.name for e in entries()}
    assert {s for s, _ in REFERENCE} | {s for s, _ in FOLLOW_UP_CELLS} == names
    assert {s for s, _ in REFERENCE} == names - {"someone_else_writes_while_waiting", "date_moves_earlier"}
    assert {s for s, _ in FOLLOW_UP_CELLS} == names - set(APPROVALS)


@pytest.mark.parametrize(("name", "behaviour"), sorted(REFERENCE))
def test_the_reference_agent_against_the_library(rig: Rig, name: str, behaviour: str) -> None:
    path = write(entry(name), REFERENCE_TEAM, rig.base, replace=False)
    env = {"REFERENCE_BEHAVIOUR": behaviour}
    if name in APPROVALS:
        env["REFERENCE_APPROVER"] = REFERENCE_TEAM.other.email
    done = rig.run(str(path), env=env, inbox=name in APPROVALS, policy=False)  # the scenario's rules alone

    code, checks = REFERENCE[(name, behaviour)]
    assert (done.code, failed(rig.state, done.run_id)) == (code, checks), done.out + done.err[-3000:]


@pytest.mark.parametrize(("name", "behaviour"), sorted(FOLLOW_UP_CELLS))
def test_the_follow_up_example_against_the_library(tmp_path: Path, name: str, behaviour: str) -> None:
    path = write(entry(name), FOLLOW_UP_TEAM, tmp_path, replace=False)
    port = free_port()
    agent = tmp_path / "agent.yaml"
    agent.write_text((FOLLOW_UP / "agent.yaml").read_text().replace("8700", str(port)))
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")} | people_environment()
    state = tmp_path / "state"
    done = subprocess.run(
        [MINUTEHAND, "run", path, "--agent", agent, "--state", state, "--", sys.executable, FOLLOW_UP / "agent.py"],
        env={**env, "PORT": str(port), "AGENT_BEHAVIOUR": behaviour},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=600,
    )

    run_id = done.stdout.split()[1].rstrip(":")
    code, checks = FOLLOW_UP_CELLS[(name, behaviour)]
    assert (done.returncode, failed(state, run_id)) == (code, checks), done.stdout + done.stderr[-3000:]
