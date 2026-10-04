"""`minutehand run` with a person whose replies a model writes and no model configured, as a subprocess."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from minutehand.adapters.model.openai_compatible import VARIABLES
from minutehand.domain.scenario import Answers, DelayRange
from tests.e2e.support import agent_under_test, scenario

MINUTEHAND = Path(sys.executable).parent / "minutehand"


def test_a_written_person_with_no_model_is_refused_with_exit_2_naming_them_and_the_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    writes = Answers(delay=DelayRange(shortest=timedelta(hours=1), longest=timedelta(hours=1)))
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text(yaml.safe_dump(scenario(writes).model_dump(mode="json")))
    agent_file = tmp_path / "agent.yaml"
    agent_file.write_text(yaml.safe_dump(launched.agent.model_dump(mode="json")))

    ran = subprocess.run(
        [str(MINUTEHAND), "run", str(scenario_file), "--agent", str(agent_file), "--state", str(tmp_path / "state")],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k not in VARIABLES},
        timeout=50,
    )

    assert ran.returncode == 2
    assert "sofia" in ran.stderr and all(v in ran.stderr for v in VARIABLES)
    assert not (tmp_path / "state").exists()
