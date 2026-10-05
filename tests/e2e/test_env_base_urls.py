"""An agent file's base URLs reach the agent: printed by `minutehand env` for an agent Minutehand does not start,
each in its own variable, against the proxy as the agent reaches it."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand.domain.agent import AgentUnderTest

MINUTEHAND = Path(sys.executable).parent / "minutehand"

AGENT = """\
name: running_agent
goal: {kind: by_message, provider: slack}
inbound:
  - {provider: slack, url: "http://platform:8025/slack/events", secret: {kind: from_env, env: SLACK_SIGNING_SECRET}}
base_urls:
  - {host: api.github.com, env: GITHUB_API_URL}
  - {host: app.asana.com, env: ASANA_BASE_URL, path: /api/1.0}
"""


def _env(tmp_path: Path, *more: str) -> subprocess.CompletedProcess[str]:
    agent = tmp_path / "agent.yaml"
    agent.write_text(AGENT)
    return subprocess.run(
        [str(MINUTEHAND), "env", "--agent", str(agent), "--proxy-port", "18080", "--no-receive-telemetry"]
        + ["--state", str(tmp_path / "state"), *more],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=120,
    )


def test_env_prints_each_declared_base_url_in_its_variable(tmp_path: Path) -> None:
    printed = _env(tmp_path)
    assert printed.returncode == 0, printed.stderr
    lines = printed.stdout.splitlines()
    assert "export GITHUB_API_URL=http://127.0.0.1:18080/_host/api.github.com" in lines
    assert "export ASANA_BASE_URL=http://127.0.0.1:18080/_host/app.asana.com/api/1.0" in lines


def test_a_container_is_handed_base_urls_on_the_name_it_reaches_the_proxy_by(tmp_path: Path) -> None:
    printed = _env(
        tmp_path, "--proxy-host", "0.0.0.0", "--agent-proxy-host", "host.docker.internal", "--format", "compose",
        "--service", "platform",
    )  # fmt: skip
    assert printed.returncode == 0, printed.stderr
    assert "GITHUB_API_URL: http://host.docker.internal:18080/_host/api.github.com" in printed.stdout


def test_two_base_urls_in_one_variable_are_refused() -> None:
    with pytest.raises(ValidationError, match="two base URLs are handed out in one variable"):
        AgentUnderTest.model_validate(
            {
                "name": "a",
                "goal": {"kind": "by_message", "provider": "slack"},
                "inbound": [{"provider": "slack", "url": "http://a:1/e", "secret": {"kind": "generated", "env": "S"}}],
                "base_urls": [{"host": "api.github.com", "env": "API"}, {"host": "slack.com", "env": "API"}],
            }
        )
