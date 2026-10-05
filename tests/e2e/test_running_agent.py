"""An agent Minutehand does not start: running before the run, configured once from `minutehand env`, with a
signing secret of its own.

It stands in for an agent in containers: its environment is all it is given, the proxy is on a fixed port it
was told in advance, under a host name that is not the address the proxy binds, and it verifies pushed events
with the secret it was configured with, which Minutehand reads from a variable of its own.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.people import SecretFromEnvironment
from minutehand.domain.run import StopReason
from minutehand.domain.world import Actor
from tests.e2e.support import (
    ANSWER,
    SECRET_VARIABLE,
    SOFIA,
    T0,
    agent_under_test,
    answers,
    free_port,
    messages,
    texts,
    world,
)
from tests.e2e.support import scenario as pricing

OWN_SECRET = "the-secret-the-agent-was-deployed-with"
MINUTEHANDS_VARIABLE = "RUNNING_AGENT_SLACK_SIGNING_SECRET"


@contextmanager
def running(command: list[str], env: dict[str, str], port: int, log: Path) -> Iterator[subprocess.Popen[bytes]]:
    """The agent's process, started by the test as a deployment would start it, and up before the run."""
    with log.open("wb") as out:
        process = subprocess.Popen(command, env=env, stdout=out, stderr=out)
        try:
            give_up = time.monotonic() + 20
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    assert process.poll() is None, log.read_text()
                    assert time.monotonic() < give_up, log.read_text()
                    time.sleep(0.05)
            yield process
        finally:
            process.terminate()
            process.wait(timeout=10)


async def test_an_agent_already_running_with_its_own_secret_is_reached_through_a_fixed_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    target = launched.agent.inbound[0].model_copy(update={"secret": SecretFromEnvironment(env=MINUTEHANDS_VARIABLE)})
    agent = launched.agent.model_copy(update={"inbound": [target]})
    listen = session.Listen(port=free_port(), agent_host="localhost")
    state = tmp_path / "state"

    configured = session.environment(agent, state=state, listen=listen)
    assert configured["HTTPS_PROXY"] == f"http://localhost:{listen.port}"
    assert SECRET_VARIABLE not in configured
    agent_port = int(launched.command[launched.command.index("--port") + 1])
    deployed = {**os.environ, **configured, SECRET_VARIABLE: OWN_SECRET}
    monkeypatch.setenv(MINUTEHANDS_VARIABLE, OWN_SECRET)

    with running(launched.command, deployed, agent_port, tmp_path / "agent.log"):
        [outcome] = await session.play(pricing(answers(after=timedelta(hours=36))), agent, state=state, listen=listen)

    record = outcome.record
    assert record.stop is StopReason.AGENT_DONE, f"{record.failure}\n{(tmp_path / 'agent.log').read_text()}"
    events = world(state, record.run_id).events()
    assert len(messages(events, Actor.AGENT, to=SOFIA)) == 2
    assert texts(messages(events, Actor.PERSON)) == [ANSWER]
    assert [m.sim_time for m in messages(events, Actor.PERSON)] == [T0 + timedelta(hours=36)]
    assert launched.state()["verified"] == [ANSWER] and launched.state()["rejected"] == 0


def test_the_environment_names_the_bundle_in_every_ca_variable_and_the_proxy_in_both_spellings(
    tmp_path: Path,
) -> None:
    agent = agent_under_test_without_generated_secret(tmp_path)
    listen = session.Listen(host="0.0.0.0", port=18080, agent_host="host.docker.internal", no_proxy=["db"])

    found = session.environment(agent, state=tmp_path / "state", listen=listen)

    bundle = (tmp_path / "state" / "ca" / "minutehand-ca-bundle.pem").resolve()
    assert bundle.is_file()
    assert {name: found[name] for name in session.CA_VARIABLES} == {name: str(bundle) for name in session.CA_VARIABLES}
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        assert found[name] == "http://host.docker.internal:18080"
    assert found["NO_PROXY"] == found["no_proxy"] == "localhost,127.0.0.1,db"


def test_binding_every_interface_hands_the_agent_loopback_unless_told_otherwise() -> None:
    assert session.Listen(host="0.0.0.0", port=8080).proxy_url(8080) == "http://127.0.0.1:8080"
    assert session.Listen(host="0.0.0.0", agent_host="proxy.internal").proxy_url(8080) == "http://proxy.internal:8080"


def test_an_environment_for_a_port_the_system_picks_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RunRefused, match="fixed port"):
        session.environment(
            agent_under_test_without_generated_secret(tmp_path), state=tmp_path, listen=session.Listen()
        )


def test_an_environment_for_an_agent_whose_secret_is_generated_per_run_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    with pytest.raises(RunRefused, match=f"generated per run for slack \\({SECRET_VARIABLE}\\)"):
        session.environment(launched.agent, state=tmp_path, listen=session.Listen(port=18080))


def agent_under_test_without_generated_secret(tmp_path: Path) -> AgentUnderTest:
    with pytest.MonkeyPatch.context() as monkeypatch:
        launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    target = launched.agent.inbound[0].model_copy(update={"secret": SecretFromEnvironment(env=MINUTEHANDS_VARIABLE)})
    return launched.agent.model_copy(update={"inbound": [target]})
