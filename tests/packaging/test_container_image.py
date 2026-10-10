"""The container image: the installed command as its entrypoint, run as a user other than root, running the
example with the example agent started inside it by `-- python agent.py`."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.packaging.conftest import EXAMPLE, checked, run, tool

pytestmark = [pytest.mark.packaging, pytest.mark.timeout(900)]


def _in_container(image: str, scenario: str, behaviour: str) -> subprocess.CompletedProcess[str]:
    """`docker run` as the example's README gives it: examples/ mounted read-only, the agent started inside, and,
    since Rosa's words are a model's, the recipes' stand-in model started beside it, as the README runs it offline."""
    script = (
        "python ../recipes/fake_model.py >/dev/null & "
        "until python -c 'import socket; socket.create_connection((\"127.0.0.1\", 8790), 1)' 2>/dev/null; "
        "do sleep 0.2; done; "
        'exec minutehand run "$0" --agent agent.yaml -- python agent.py'
    )
    return run(
        tool("docker"),
        "run",
        "--rm",
        "--env",
        f"AGENT_BEHAVIOUR={behaviour}",
        *("--env", "MINUTEHAND_MODEL_BASE_URL=http://127.0.0.1:8790/v1"),
        *("--env", "MINUTEHAND_MODEL=people"),
        *("--env", "MINUTEHAND_MODEL_API_KEY=offline"),
        "--volume",
        f"{EXAMPLE.parent}:/examples:ro",
        "--workdir",
        "/examples/follow_up",
        "--entrypoint",
        "sh",
        f"{image}-example",
        "-c",
        script,
        scenario,
    )


def test_the_image_runs_minutehand_as_a_user_other_than_root_and_serves_by_default(image: str) -> None:
    helped = run(tool("docker"), "run", "--rm", image, "--help")
    assert helped.returncode == 0, helped.stderr
    assert "usage: minutehand" in helped.stdout

    inspected = json.loads(checked(tool("docker"), "image", "inspect", image).stdout)[0]["Config"]
    assert inspected["Entrypoint"] == ["minutehand"]
    assert inspected["Cmd"] == ["serve", "--host", "0.0.0.0"]
    assert set(inspected["ExposedPorts"]) == {"8080/tcp", "8081/tcp", "4318/tcp"}
    assert inspected["User"] not in ("", "root", "0")
    assert "/var/lib/minutehand" in inspected["Volumes"]


def test_the_image_passes_the_example_and_fails_the_forgetful_agent(image: str) -> None:
    passed = _in_container(image, "scenario.yaml", "diligent")
    assert passed.returncode == 0, f"{passed.stdout}\n{passed.stderr}"
    assert "\n  Passed: no check failed in the window;" in passed.stdout, passed.stdout
    assert "\nfail (" not in passed.stdout, passed.stdout

    failed = _in_container(image, "scenario_team_policy.yaml", "forgetful")  # the team's own rule: a follow-up is owed
    assert failed.returncode == 1, f"{failed.stdout}\n{failed.stderr}"
    out = failed.stdout
    assert "\nfail (1)\n" in out, out
    found = out[out.index("\nfail (1)") : out.index("\nscorecard")]
    assert "follows_up_when_due: rosa was asked two days ago and no follow-up came" in found
    assert "pattern expiry_on_every_wait: An expiry on every wait." in found
