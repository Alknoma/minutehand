"""The container image: the installed command as its entrypoint, run as a user other than root, running the
example with the example agent started inside it by `-- python agent.py`."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.packaging.conftest import EXAMPLE, checked, run, tool

pytestmark = [pytest.mark.packaging, pytest.mark.timeout(900)]


def _in_container(image: str, scenario: str, behaviour: str) -> subprocess.CompletedProcess[str]:
    """`docker run` as the example's README gives it: examples/ mounted read-only, the agent started inside."""
    return run(
        tool("docker"),
        "run",
        "--rm",
        "--env",
        f"AGENT_BEHAVIOUR={behaviour}",
        "--volume",
        f"{EXAMPLE.parent}:/examples:ro",
        "--workdir",
        "/examples/follow_up",
        f"{image}-example",
        "run",
        scenario,
        "--agent",
        "agent.yaml",
        "--",
        "python",
        "agent.py",
    )


def test_the_image_runs_minutehand_as_a_user_other_than_root(image: str) -> None:
    helped = run(tool("docker"), "run", "--rm", image)
    assert helped.returncode == 0, helped.stderr
    assert "usage: minutehand" in helped.stdout

    inspected = json.loads(checked(tool("docker"), "image", "inspect", image).stdout)[0]["Config"]
    assert inspected["Entrypoint"] == ["minutehand"]
    assert inspected["User"] not in ("", "root", "0")
    assert "/var/lib/minutehand" in inspected["Volumes"]


def test_the_image_passes_the_example_and_fails_the_forgetful_agent(image: str) -> None:
    passed = _in_container(image, "scenario.yaml", "diligent")
    assert passed.returncode == 0, f"{passed.stdout}\n{passed.stderr}"
    assert "\n  Passed: no check failed, and the agent reported it was done.\n" in passed.stdout, passed.stdout
    assert "\nfail (" not in passed.stdout, passed.stdout

    failed = _in_container(image, "scenario_silent.yaml", "forgetful")
    assert failed.returncode == 1, f"{failed.stdout}\n{failed.stderr}"
    out = failed.stdout
    assert "\nfail (1)\n" in out, out
    found = out[out.index("\nfail (1)") : out.index("\nscorecard")]
    assert "no_follow_up: wait on rosa expired" in found
    assert "pattern expiry_on_every_wait: An expiry on every wait." in found
