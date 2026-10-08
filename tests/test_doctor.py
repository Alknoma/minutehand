"""`minutehand doctor`: the clients that would go around the proxy are named before a run, not found missing after.

The probe runs in this test's own interpreter, which holds aiohttp and urllib3 (both ignore HTTPS_PROXY as they are
used here) beside requests, httpx and urllib (which read it). Its canary is an address no packet reaches, so a
client that goes around the proxy fails at once and nothing leaves the machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from minutehand.doctor import claimed_hosts, docker_warnings, people_model

MINUTEHAND = Path(sys.executable).parent / "minutehand"


def _agent(directory: Path) -> Path:
    agent = directory / "agent.yaml"
    agent.write_text(
        yaml.safe_dump(
            {
                "name": "a",
                "wakes": [
                    {"kind": "reported", "wake_url": "http://127.0.0.1:1/w", "report_url": "http://127.0.0.1:1/r"}
                ],
                "outbound": [
                    {"host": "api.mail.example", "kind": "acknowledge"},
                    {"host": "search.localhost", "kind": "pass_through"},
                    {"host": "2001:db8::1", "kind": "pass_through"},
                ],
            }
        )
    )
    return agent


def test_the_doctor_names_each_client_that_would_bypass_the_proxy_and_each_host_it_cannot_reach(
    tmp_path: Path,
) -> None:
    done = subprocess.run(
        [str(MINUTEHAND), "doctor", "--agent", str(_agent(tmp_path)), "--json", "--", sys.executable, "agent.py"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 1, done.stderr
    found = json.loads(done.stdout)
    results = {c["library"]: c["result"] for c in found["libraries"]}
    assert {k: v for k, v in results.items() if k in ("requests", "httpx", "urllib")} == {
        "requests": "reached",
        "httpx": "reached",
        "urllib": "reached",
    }
    assert results["aiohttp"].startswith("bypassed") and results["urllib3"].startswith("bypassed")
    hosts = {h["host"]: (h["bypassed_by"], h["unreachable_by"]) for h in found["hosts"]}
    assert hosts == {"api.mail.example": ([], []), "search.localhost": ([], []), "2001:db8::1": ([], ["httpx"])}


def test_the_doctor_names_each_host_a_name_handed_out_would_send_direct_for_an_agent_in_a_container(
    tmp_path: Path,
) -> None:
    """An agent elsewhere is handed `localhost`, which the proxy cannot forward to it: every client that reads a name
    as a suffix sends `search.localhost` direct; httpx and Node read it exactly."""
    done = subprocess.run(
        [
            str(MINUTEHAND),
            "doctor",
            "--agent",
            str(_agent(tmp_path)),
            "--agent-host",
            "host.docker.internal",
            "--json",
            "--",
            sys.executable,
            "agent.py",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 1, done.stderr
    found = json.loads(done.stdout)
    assert found["no_proxy"] == ["127.0.0.1", "host.docker.internal", "localhost"]
    hosts = {h["host"]: h["bypassed_by"] for h in found["hosts"]}
    assert hosts == {
        "api.mail.example": [],
        "search.localhost": ["aiohttp", "curl", "requests", "urllib"],
        "2001:db8::1": [],
    }


def _docker_config(directory: Path, no_proxy: str) -> dict[str, str]:
    """A Docker client config in `directory` whose default proxies send `no_proxy` direct, and the environment that
    points the Docker CLI (and Minutehand) at it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps({"auths": {}, "credsStore": "desktop", "proxies": {"default": {"noProxy": no_proxy}}})
    )
    return {**os.environ, "DOCKER_CONFIG": str(directory)}


def test_a_docker_config_sending_every_host_direct_is_named_with_its_file_value_and_fix(tmp_path: Path) -> None:
    """Docker Desktop's own `"noProxy": "*"`: copied into every container's NO_PROXY, it sends every call around the
    proxy, and the run records nothing."""
    environ = _docker_config(tmp_path / "docker", "*")

    found = docker_warnings(claimed_hosts(None), environ)

    assert len(found) == 1
    assert found[0].startswith(f"{tmp_path / 'docker' / 'config.json'} sets proxies.default.noProxy to '*'")
    assert "sends every call straight to the real service" in found[0]
    assert "NO_PROXY and no_proxy with -e, Compose's environment: or env_file:" in found[0]


def test_a_docker_config_entry_covering_a_claimed_host_is_named_and_one_covering_none_is_not(tmp_path: Path) -> None:
    covering = docker_warnings(claimed_hosts(None), _docker_config(tmp_path / "a", "127.0.0.1,.slack.com,localhost"))
    under_a_wildcard = docker_warnings(["*.atlassian.net"], _docker_config(tmp_path / "b", "acme.atlassian.net"))
    harmless = docker_warnings(claimed_hosts(None), _docker_config(tmp_path / "c", "127.0.0.1,localhost,db.internal"))

    assert len(covering) == 1 and "sends calls to slack.com, *.slack.com straight to" in covering[0]
    assert len(under_a_wildcard) == 1 and "calls to *.atlassian.net" in under_a_wildcard[0]
    assert harmless == []
    assert docker_warnings(claimed_hosts(None), {**os.environ, "DOCKER_CONFIG": str(tmp_path / "none")}) == []


def test_the_doctor_warns_of_a_docker_config_that_sends_every_host_direct(tmp_path: Path) -> None:
    done = subprocess.run(
        [str(MINUTEHAND), "doctor", "--json", "--", sys.executable, "agent.py"],
        capture_output=True,
        text=True,
        timeout=120,
        env=_docker_config(tmp_path / "docker", "*"),
    )

    found = json.loads(done.stdout)
    assert len(found["docker"]) == 1 and "sets proxies.default.noProxy to '*'" in found["docker"][0], done.stderr


def test_doctor_says_whether_a_model_is_configured_to_write_what_people_say() -> None:
    configured = people_model({"MINUTEHAND_MODEL": "people-1", "MINUTEHAND_MODEL_API_KEY": "k"})
    assert configured.configured and configured.said == "people-1 at https://api.openai.com/v1"
    missing = people_model({})
    assert not missing.configured and "no model is configured" in missing.said and "is refused" in missing.said
    half = people_model({"MINUTEHAND_MODEL": "people-1"})
    assert not half.configured and "MINUTEHAND_MODEL_API_KEY is not set" in half.said
