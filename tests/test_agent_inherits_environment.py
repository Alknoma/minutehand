"""The agent's command inherits Minutehand's environment, the adopter's provider keys included, plus what Minutehand
hands it; Minutehand declares, filters and withholds nothing. An agent file that tries to declare the agent's
environment is refused by name: the environment is the adopter's launch command's, not Minutehand's."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.files import FileRefused, load_agent
from minutehand.domain.agent import AgentUnderTest
from tests.support.scenarios import SANDBOX

SENTINEL = "sentinel-provider-key-never-real"

PRINTS_ITS_ENVIRONMENT = """
import json, os, sys
open(sys.argv[1], "w").write(json.dumps(dict(os.environ)))
"""


@pytest.mark.asyncio
async def test_the_agents_command_sees_the_inherited_environment_the_provider_keys_included(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    seen = tmp_path / "environment.json"
    agent = AgentUnderTest(name="inherits", sandbox=SANDBOX)
    program = session._Program(  # pyright: ignore[reportPrivateUsage]
        [sys.executable, "-c", PRINTS_ITS_ENVIRONMENT, str(seen)], {"MINUTEHAND_ON": "1"}, agent, tmp_path / "agent.log"
    )
    await program.start()
    deadline = time.monotonic() + 10
    while not seen.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    await program.stop()

    environment = json.loads(seen.read_text())
    assert environment["OPENAI_API_KEY"] == SENTINEL and environment["ANTHROPIC_API_KEY"] == SENTINEL
    assert environment["MINUTEHAND_ON"] == "1", "and Minutehand's own, on top"


def test_an_agent_file_declaring_the_agents_environment_is_refused_by_name(tmp_path: Path) -> None:
    text = "name: declares\nsandbox: {deadlines: [true], advance: [true]}\nenvironment:\n  sources: [{kind: inherit}]\n"
    with pytest.raises(FileRefused) as refused:
        load_agent(tmp_path / "agent.yaml", text=text)
    assert "environment" in str(refused.value)
