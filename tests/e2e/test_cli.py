"""The `minutehand` command itself, as a subprocess, wrapping the agent's own command after `--`."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from minutehand.domain.scenario import Silent
from tests.e2e.support import agent_under_test, scenario

MINUTEHAND = Path(sys.executable).parent / "minutehand"


def _dump(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def _cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(MINUTEHAND), *args], capture_output=True, text=True, env=env, timeout=120)


def test_a_forgetful_agent_fails_no_follow_up_and_the_command_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    scenario_file = _dump(tmp_path / "scenario.yaml", yaml.safe_dump(scenario(Silent()).model_dump(mode="json")))
    agent_file = _dump(tmp_path / "agent.yaml", yaml.safe_dump(launched.agent.model_dump(mode="json")))
    state = tmp_path / "state"

    ran = _cli(
        "run",
        str(scenario_file),
        "--agent",
        str(agent_file),
        "--state",
        str(state),
        "--",
        *launched.command,
        env=dict(os.environ),
    )

    assert ran.returncode == 1, ran.stderr
    out = ran.stdout
    assert "because nothing more was due and the agent asked for no wake" in out
    failed = out[out.index("\nfail (") : out.index("\nscorecard")]
    assert "no_follow_up: wait on sofia expired" in failed
    assert "pattern expiry_on_every_wait: An expiry on every wait." in failed
    assert "expectations met: 1 of 2" in out
    run_id = re.search(r"^run ([0-9a-f]+):", out, re.MULTILINE)
    assert run_id is not None

    again = _cli("findings", run_id.group(1), "--state", str(state), env=dict(os.environ))
    assert again.returncode == 1 and "no_follow_up: wait on sofia expired" in again.stdout
    listed = _cli("runs", "--state", str(state), env=dict(os.environ))
    assert listed.returncode == 0 and f"{run_id.group(1)}  partner_pricing  nothing_pending" in listed.stdout


def test_an_agent_command_that_exits_before_it_listens_is_refused_with_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    scenario_file = _dump(tmp_path / "scenario.yaml", yaml.safe_dump(scenario(Silent()).model_dump(mode="json")))
    agent_file = _dump(tmp_path / "agent.yaml", yaml.safe_dump(launched.agent.model_dump(mode="json")))

    ran = _cli(
        "run",
        str(scenario_file),
        "--agent",
        str(agent_file),
        "--state",
        str(tmp_path / "state"),
        "--",
        sys.executable,
        "-c",
        "import sys; print('no secret for me'); sys.exit(4)",
        env=dict(os.environ),
    )

    assert ran.returncode == 2
    assert "exited 4 before http://127.0.0.1:" in ran.stderr and "no secret for me" in ran.stderr


RUNNING_AGENT = """\
name: running_agent
goal: {kind: by_message, provider: slack}
inbound:
  - {provider: slack, url: "http://platform:8025/slack/events", secret: {kind: from_env, env: SLACK_SIGNING_SECRET}}
"""


def test_env_prints_shell_exports_for_an_agent_minutehand_does_not_start(tmp_path: Path) -> None:
    agent_file = _dump(tmp_path / "agent.yaml", RUNNING_AGENT)
    state = tmp_path / "state"

    printed = _cli(
        "env", "--agent", str(agent_file), "--proxy-port", "18080", "--state", str(state), env=dict(os.environ)
    )

    assert printed.returncode == 0, printed.stderr
    exports = dict(line.removeprefix("export ").split("=", 1) for line in printed.stdout.splitlines())
    bundle = str((state / "ca" / "minutehand-ca-bundle.pem").resolve())
    assert exports["HTTPS_PROXY"] == exports["https_proxy"] == "http://127.0.0.1:18080"
    assert exports["SSL_CERT_FILE"] == exports["REQUESTS_CA_BUNDLE"] == exports["HTTPLIB2_CA_CERTS"] == bundle
    assert "SLACK_SIGNING_SECRET" not in exports


def test_env_writes_a_compose_override_that_injects_the_variables_and_mounts_the_bundle(tmp_path: Path) -> None:
    agent_file = _dump(tmp_path / "agent.yaml", RUNNING_AGENT)
    state = tmp_path / "state"

    printed = _cli(
        "env",
        "--agent",
        str(agent_file),
        "--proxy-host",
        "0.0.0.0",
        "--proxy-port",
        "18080",
        "--agent-proxy-host",
        "host.docker.internal",
        "--format",
        "compose",
        "--service",
        "platform",
        "--service",
        "worker",
        "--no-proxy",
        "firestore",
        "--state",
        str(state),
        env=dict(os.environ),
    )

    assert printed.returncode == 0, printed.stderr
    override = yaml.safe_load(printed.stdout)
    assert sorted(override["services"]) == ["platform", "worker"]
    platform = override["services"]["platform"]
    bundle = (state / "ca" / "minutehand-ca-bundle.pem").resolve()
    assert platform["volumes"] == [f"{bundle}:/etc/minutehand/ca-bundle.pem:ro"]
    assert platform["extra_hosts"] == ["host.docker.internal:host-gateway"]
    environment = platform["environment"]
    assert environment["HTTPS_PROXY"] == "http://host.docker.internal:18080"
    assert environment["SSL_CERT_FILE"] == environment["NODE_EXTRA_CA_CERTS"] == "/etc/minutehand/ca-bundle.pem"
    assert environment["NO_PROXY"] == "localhost,127.0.0.1,platform,worker,firestore"


def test_env_for_an_agent_whose_secret_is_generated_per_run_is_refused_with_exit_2(tmp_path: Path) -> None:
    agent_file = _dump(tmp_path / "agent.yaml", RUNNING_AGENT.replace("from_env", "generated"))

    printed = _cli(
        "env", "--agent", str(agent_file), "--proxy-port", "18080", "--state", str(tmp_path), env=dict(os.environ)
    )

    assert printed.returncode == 2
    assert "generated per run for slack (SLACK_SIGNING_SECRET)" in printed.stderr
