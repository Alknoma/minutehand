"""`minutehand run --judge`, as a subprocess: judged checks run only when asked for, and asked for with no
model they are blocked, never passed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from minutehand.adapters.model.environment import VARIABLES
from minutehand.domain.scenario import Silent
from tests.e2e.support import agent_under_test, scenario

MINUTEHAND = Path(sys.executable).parent / "minutehand"
JUDGED_BLOCKED = ("asked_about: no model is configured",)


def _without_a_model() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in VARIABLES}


@pytest.mark.parametrize("judge", [True, False])
def test_judged_checks_are_blocked_without_a_model_only_when_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, judge: bool
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text(yaml.safe_dump(scenario(Silent()).model_dump(mode="json")))
    agent_file = tmp_path / "agent.yaml"
    agent_file.write_text(yaml.safe_dump(launched.agent.model_dump(mode="json")))

    ran = subprocess.run(
        [
            str(MINUTEHAND),
            "run",
            str(scenario_file),
            "--agent",
            str(agent_file),
            "--state",
            str(tmp_path / "state"),
            *(["--judge"] if judge else []),
            "--",
            *launched.command,
        ],
        capture_output=True,
        text=True,
        env=_without_a_model(),
        timeout=50,
    )

    assert ran.returncode == 1, ran.stderr
    assert [line in ran.stdout for line in JUDGED_BLOCKED] == [judge]
