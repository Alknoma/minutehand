"""Where the secret that signs pushed events comes from: made for the run, or the agent's own."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from minutehand.application.files import load_agent
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import AgentUnderTest, GoalByMessage
from minutehand.domain.people import GeneratedSecret, InboundTarget, SecretFromEnvironment, SigningSecret
from minutehand.session import signing_for

URL = "http://127.0.0.1:9/slack/events"


def _agent(secret: SigningSecret | None) -> AgentUnderTest:
    return AgentUnderTest(
        name="pushed",
        goal=GoalByMessage(provider="slack"),
        inbound=[InboundTarget(provider="slack", url=URL, secret=secret)],
    )


def test_a_generated_secret_is_fresh_per_run_and_handed_to_the_agent_under_its_variable() -> None:
    agent = _agent(GeneratedSecret(env="AGENT_SLACK_SIGNING_SECRET"))
    first, second = signing_for(agent), signing_for(agent)

    assert first.for_agent == {"AGENT_SLACK_SIGNING_SECRET": first.by_provider["slack"]}
    assert first.by_provider["slack"] != second.by_provider["slack"]
    assert "AGENT_SLACK_SIGNING_SECRET" not in os.environ


def test_the_agents_own_secret_is_read_from_minutehands_variable_and_handed_to_no_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNING_AGENT_SLACK_SECRET", "configured-in-the-agents-container")

    signing = signing_for(_agent(SecretFromEnvironment(env="RUNNING_AGENT_SLACK_SECRET")))

    assert signing.by_provider == {"slack": "configured-in-the-agents-container"}
    assert signing.for_agent == {}


def test_the_agents_own_secret_from_a_variable_that_is_not_set_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNNING_AGENT_SLACK_SECRET", raising=False)

    with pytest.raises(RunRefused, match="RUNNING_AGENT_SLACK_SECRET, which is not set"):
        signing_for(_agent(SecretFromEnvironment(env="RUNNING_AGENT_SLACK_SECRET")))


def test_a_target_naming_no_secret_is_still_signed_and_hands_nothing_to_the_agent() -> None:
    signing = signing_for(_agent(None))

    assert len(signing.by_provider["slack"]) == 32 and signing.for_agent == {}


def test_an_agent_file_says_where_its_secret_comes_from(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        "name: running\ngoal: {kind: by_message, provider: slack}\n"
        f"inbound:\n  - {{provider: slack, url: '{URL}', secret: {{kind: from_env, env: SLACK_SIGNING_SECRET}}}}\n"
    )

    assert load_agent(path).inbound[0].secret == SecretFromEnvironment(env="SLACK_SIGNING_SECRET")
