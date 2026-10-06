"""`minutehand doctor`: the clients that would go around the proxy are named before a run, not found missing after.

The probe runs in this test's own interpreter, which holds aiohttp and urllib3 (both ignore HTTPS_PROXY as they are
used here) beside requests, httpx and urllib (which read it). Its canary is an address no packet reaches, so a
client that goes around the proxy fails at once and nothing leaves the machine.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

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
