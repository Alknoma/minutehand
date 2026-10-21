"""The examples of docs/approvals.md: every file under docs/approvals loads with every load-time check, with the agent
file it plays under, as a run reads them."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from minutehand.application.files import load_fork

pytestmark = pytest.mark.timeout(240)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "docs" / "approvals"
MINUTEHAND = Path(sys.executable).parent / "minutehand"
FORKS = {"flip_to_approve.yaml", "believes_approved.yaml"}

OWEN, NADIA, MARTA = "owen@example.com", "nadia@example.com", "marta@example.com"


# -- the files and the guide --------------------------------------------------------------------------------------


def _example_files() -> list[Path]:
    return sorted(p for p in EXAMPLES.rglob("*.yaml") if p.name not in FORKS)


def _validate(*files: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    return subprocess.run(
        [str(MINUTEHAND), "validate", *map(str, files)], capture_output=True, text=True, env=env, timeout=120
    )


GROUPS = {
    # an agent file and the scenarios it plays: validated together, so each scenario's rules are read as a run
    # reads them (a person a rule names that the scenario lacks, an `assess_off` naming nothing, are refused)
    "inbox/agent.yaml": ["inbox/approved.yaml", "inbox/rejected.yaml", "inbox/never_decides.yaml",
                         "inbox/timing/decision_timing.yaml"],
    "chat/agent.yaml": ["chat/approved.yaml", "chat/rejected.yaml"],
    "chat/teams_agent.yaml": ["chat/approved.yaml", "chat/rejected.yaml"],
    "email/agent.yaml": ["email/rejected.yaml", "email/away.yaml", "email/budget_changes.yaml"],
    "service/agent.yaml": ["service/never_decides.yaml"],
    "service/slack_fallthrough.yaml": [],
    "": ["calendar/declined_invitation.yaml", "tracker/approved_by_ticket.yaml"],
}  # fmt: skip


def test_every_example_file_loads_with_every_load_time_check() -> None:
    grouped = {EXAMPLES / f for agent, scenarios in GROUPS.items() for f in [agent, *scenarios] if f}
    assert grouped == set(_example_files()), "every example file is in a group"
    for agent, scenarios in GROUPS.items():
        files = [EXAMPLES / f for f in [agent, *scenarios] if f]
        checked = _validate(*files)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert checked.stdout.count(": a valid ") == len(files), checked.stdout
    for name in FORKS:
        load_fork(EXAMPLES / "inbox" / name, parent_run="parent", at_seq=12)


def test_a_rule_naming_someone_the_scenario_lacks_is_refused_with_its_agent_file() -> None:
    """What the grouping above guards: the team's rules name the backup approver, whom the calendar scenario lacks."""
    checked = _validate(EXAMPLES / "inbox" / "agent.yaml", EXAMPLES / "calendar" / "declined_invitation.yaml")
    assert checked.returncode == 1
    assert "rule chases_at_most_twice names marta, who is not in the scenario" in checked.stdout + checked.stderr
