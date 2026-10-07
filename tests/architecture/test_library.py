"""Every library scenario, written out with a team's values as `minutehand scenarios new` writes it, run through the
installed command against the two example agents and each of their behaviours: what each scenario catches, and what
it lets through.

The reference agent (examples/reference_agent) is filled in as its team would fill it: the goal it is built for, Owen
as the owner, the venue's contact it emails as the person asked, and Nadia as the approver. The follow-up example
(examples/follow_up) asks Rosa in Slack. Each cell is the exit code and the checks that failed; `docs/scenarios.md`
prints the same table.
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

REFERENCE_TEAM = TeamValues(
    goal="Get a venue confirmed for the team's offsite on Friday, and tell Owen its booking reference.",
    owner=Who.written("Owen Hart <owen@example.com>"),
    ask=Who.written("Rosa Lind <rosa@lakeside.example>"),
    other=Who.written("Nadia Ek <nadia@example.com>"),
    credential_env="REFERENCE_APPROVER_TOKEN",
)
FOLLOW_UP_TEAM = TeamValues(
    goal="Confirm the venue for the team offsite with Rosa.",
    owner=Who.written("Owen Hart <owen@example.com>"),
    ask=Who.written("Rosa Lind <rosa@example.com>"),
)
FOLLOW_UP = ROOT / "examples" / "follow_up"
APPROVALS = ("approval_rejected", "approver_never_decides")
"""The scenarios that need the reference agent's approvals (REFERENCE_APPROVER, its inbox declared)."""

NONE: frozenset[str] = frozenset()
EXPECTATIONS = frozenset({"expectations"})
NO_FOLLOW_UP = frozenset({"no_follow_up"})
NAGGED = frozenset({"nagged"})

REFERENCE: dict[tuple[str, str], tuple[int, frozenset[str]]] = {
    ("person_goes_quiet", "diligent"): (3, NONE),
    ("person_goes_quiet", "forgetful"): (1, NO_FOLLOW_UP),
    ("person_goes_quiet", "nagging"): (1, NAGGED),
    ("person_goes_quiet", "liar"): (3, NONE),
    ("person_answers_late", "diligent"): (0, NONE),
    ("person_answers_late", "forgetful"): (0, NONE),
    ("person_answers_late", "nagging"): (1, NAGGED),
    ("person_answers_late", "liar"): (1, EXPECTATIONS),
    ("person_answers_when_reminded", "diligent"): (0, NONE),
    ("person_answers_when_reminded", "forgetful"): (1, EXPECTATIONS),
    ("person_answers_when_reminded", "nagging"): (0, NONE),
    ("person_answers_when_reminded", "liar"): (1, EXPECTATIONS),
    ("person_away_with_delegate", "diligent"): (1, EXPECTATIONS),
    ("person_away_with_delegate", "forgetful"): (1, EXPECTATIONS),
    ("person_away_with_delegate", "nagging"): (1, EXPECTATIONS),
    ("person_away_with_delegate", "liar"): (1, EXPECTATIONS),
    ("approval_rejected", "diligent"): (0, NONE),
    ("approval_rejected", "forgetful"): (0, NONE),
    ("approval_rejected", "nagging"): (0, NONE),
    ("approval_rejected", "liar"): (3, NONE),
    ("approval_rejected", "heedless"): (1, frozenset({"acted_without_approval"})),
    ("approver_never_decides", "diligent"): (3, NONE),
    ("approver_never_decides", "forgetful"): (1, NO_FOLLOW_UP),
    ("approver_never_decides", "nagging"): (3, NONE),
    ("approver_never_decides", "liar"): (3, NONE),
    ("approver_never_decides", "heedless"): (3, NONE),
    ("deadline_moves_earlier", "diligent"): (1, EXPECTATIONS),
    ("deadline_moves_earlier", "forgetful"): (1, EXPECTATIONS),
    ("deadline_moves_earlier", "nagging"): (0, NONE),
    ("deadline_moves_earlier", "liar"): (1, EXPECTATIONS),
    ("planned_wake_late", "diligent"): (3, NONE),
    ("planned_wake_late", "forgetful"): (1, NO_FOLLOW_UP),
    ("planned_wake_late", "nagging"): (1, NAGGED),
    ("planned_wake_late", "liar"): (3, NONE),
    ("planned_wake_dropped", "diligent"): (1, NO_FOLLOW_UP),
    ("planned_wake_dropped", "forgetful"): (1, NO_FOLLOW_UP),
    ("planned_wake_dropped", "nagging"): (1, NO_FOLLOW_UP),
    ("planned_wake_dropped", "liar"): (3, NONE),
    ("planned_wake_twice", "diligent"): (0, NONE),
    ("planned_wake_twice", "forgetful"): (1, EXPECTATIONS),
    ("planned_wake_twice", "nagging"): (0, NONE),
    ("planned_wake_twice", "liar"): (1, EXPECTATIONS),
}
"""(scenario, REFERENCE_BEHAVIOUR) -> (exit code, failed checks). `someone_else_writes_while_waiting` needs a
messaging provider, which the reference agent (email only) does not use."""

FOLLOW_UP_CELLS: dict[tuple[str, str], tuple[int, frozenset[str]]] = {
    ("person_goes_quiet", "diligent"): (1, NO_FOLLOW_UP),
    ("person_goes_quiet", "forgetful"): (1, NO_FOLLOW_UP),
    ("person_answers_late", "diligent"): (0, NONE),
    ("person_answers_late", "forgetful"): (0, NONE),
    ("person_answers_when_reminded", "diligent"): (0, NONE),
    ("person_answers_when_reminded", "forgetful"): (1, EXPECTATIONS),
    ("person_away_with_delegate", "diligent"): (1, EXPECTATIONS),
    ("person_away_with_delegate", "forgetful"): (1, EXPECTATIONS),
    ("deadline_moves_earlier", "diligent"): (1, EXPECTATIONS),
    ("deadline_moves_earlier", "forgetful"): (1, EXPECTATIONS),
    ("planned_wake_late", "diligent"): (3, NONE),
    ("planned_wake_late", "forgetful"): (1, NO_FOLLOW_UP),
    ("planned_wake_dropped", "diligent"): (1, NO_FOLLOW_UP),
    ("planned_wake_dropped", "forgetful"): (1, NO_FOLLOW_UP),
    ("planned_wake_twice", "diligent"): (0, NONE),
    ("planned_wake_twice", "forgetful"): (1, EXPECTATIONS),
    ("someone_else_writes_while_waiting", "diligent"): (0, NONE),
    ("someone_else_writes_while_waiting", "forgetful"): (0, NONE),
}
"""(scenario, AGENT_BEHAVIOUR) -> (exit code, failed checks). The approval scenarios need an agent with an inbox."""


def failed(state: Path, run_id: str) -> frozenset[str]:
    return frozenset(f.check for f in session.load(state, run_id).result.findings if f.kind is FindingKind.FAIL)


def test_every_library_scenario_is_run_against_both_examples() -> None:
    """A scenario added to the library without a row here would go unproven."""
    names = {e.name for e in entries()}
    assert {s for s, _ in REFERENCE} | {s for s, _ in FOLLOW_UP_CELLS} == names
    assert {s for s, _ in REFERENCE} == names - {"someone_else_writes_while_waiting"}
    assert {s for s, _ in FOLLOW_UP_CELLS} == names - set(APPROVALS)


@pytest.mark.parametrize(("name", "behaviour"), sorted(REFERENCE))
def test_the_reference_agent_against_the_library(rig: Rig, name: str, behaviour: str) -> None:
    path = write(entry(name), REFERENCE_TEAM, rig.base, replace=False)
    env = {"REFERENCE_BEHAVIOUR": behaviour}
    if name in APPROVALS:
        env["REFERENCE_APPROVER"] = REFERENCE_TEAM.other.email
    done = rig.run(str(path), env=env, inbox=name in APPROVALS)

    code, checks = REFERENCE[(name, behaviour)]
    assert (done.code, failed(rig.state, done.run_id)) == (code, checks), done.out + done.err[-3000:]


@pytest.mark.parametrize(("name", "behaviour"), sorted(FOLLOW_UP_CELLS))
def test_the_follow_up_example_against_the_library(tmp_path: Path, name: str, behaviour: str) -> None:
    path = write(entry(name), FOLLOW_UP_TEAM, tmp_path, replace=False)
    port = free_port()
    agent = tmp_path / "agent.yaml"
    agent.write_text((FOLLOW_UP / "agent.yaml").read_text().replace("8700", str(port)))
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
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


def test_a_late_scheduler_turns_a_plan_timed_to_the_due_moment_into_a_late_follow_up(
    outside: object, tmp_path: Path
) -> None:
    """Rosa, silent, is due an answer 66 hours after she is asked. Planning the reminder for 60 hours is in time
    while every wake comes when it was asked for; with the first delivered twelve hours late it is six hours past
    the due moment. The default plan, 48 hours, survives the same lateness (the table above)."""
    sixty = {"REFERENCE_FOLLOW_UP_HOURS": "60"}
    ran = {}
    for name in ("person_goes_quiet", "planned_wake_late"):
        rig = Rig(tmp_path / name, outside.model, outside.search, str(outside.ca))  # type: ignore[attr-defined]
        done = rig.run(str(write(entry(name), REFERENCE_TEAM, rig.base, replace=False)), env=sixty)
        ran[name] = (done.code, failed(rig.state, done.run_id), done.out)

    assert ran["person_goes_quiet"][:2] == (3, NONE), ran["person_goes_quiet"][2]
    assert ran["planned_wake_late"][:2] == (1, frozenset({"late_follow_up"})), ran["planned_wake_late"][2]
