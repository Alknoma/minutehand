"""The package as a user installs it: built by `uv build`, installed into a clean environment, and run."""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.packaging.conftest import EXAMPLE, Dist, Installed, checked, outside_this_project, run

pytestmark = [pytest.mark.packaging, pytest.mark.timeout(900)]

NO_FOLLOW_UP = "no_follow_up: wait on rosa expired"
PATTERN = "pattern expiry_on_every_wait: An expiry on every wait."


def tracked_package_files() -> set[str]:
    """Every file git tracks under src/minutehand, as its path inside the wheel."""
    listed = checked("git", "ls-files", "src/minutehand").stdout.split()
    return {name.removeprefix("src/") for name in listed}


def test_uv_build_makes_an_sdist_and_a_wheel(dist: Dist) -> None:
    assert dist.sdist.name.startswith("minutehand-") and dist.wheel.name.endswith("-py3-none-any.whl")
    with tarfile.open(dist.sdist) as sdist:
        names = {member.name.split("/", 1)[1] for member in sdist.getmembers() if "/" in member.name}
    assert {
        "pyproject.toml",
        "README.md",
        "LICENSE.md",
        "src/minutehand/cli.py",
        "examples/follow_up/agent.py",
    } <= names


def test_the_wheel_holds_every_file_under_src_minutehand(dist: Dist) -> None:
    with zipfile.ZipFile(dist.wheel) as wheel:
        shipped = set(wheel.namelist())
    missing = sorted(tracked_package_files() - shipped)
    assert missing == [], f"the wheel leaves out {missing}; add their pattern to [tool.hatch.build.targets.wheel]"
    assert "minutehand-0.0.1.dist-info/entry_points.txt" in shipped


def test_the_installed_command_runs_from_its_own_environment(installed: Installed) -> None:
    where = run(
        str(installed.python),
        "-c",
        "import minutehand, slack_sdk",
        cwd=installed.venv,
        env=outside_this_project(),
    )
    assert "No module named 'slack_sdk'" in where.stderr, "a dev dependency leaked into the installed environment"
    imported = checked(
        str(installed.python),
        "-c",
        "import minutehand; print(minutehand.__file__)",
        cwd=installed.venv,
        env=outside_this_project(),
    )
    assert Path(imported.stdout.strip()).is_relative_to(installed.venv)

    helped = run(str(installed.minutehand), "--help", cwd=installed.venv, env=outside_this_project())

    assert helped.returncode == 0, helped.stderr
    assert "usage: minutehand" in helped.stdout and "run a scenario against an agent" in helped.stdout


def _example(installed: Installed, scenario: str, behaviour: str, state: Path) -> subprocess.CompletedProcess[str]:
    """The example's own command line, from its folder; the agent runs in this checkout's environment, which
    has slack_sdk, and Minutehand in the clean one, which does not."""
    env = {**outside_this_project(), "AGENT_BEHAVIOUR": behaviour}
    argv = [str(installed.minutehand), "run", scenario, "--agent", "agent.yaml", "--state", str(state)]
    return run(*argv, "--", sys.executable, "agent.py", cwd=EXAMPLE, env=env)


def test_the_installed_command_passes_the_example_and_fails_the_forgetful_agent(
    installed: Installed, tmp_path: Path
) -> None:
    passed = _example(installed, "scenario.yaml", "diligent", tmp_path / "state")
    assert passed.returncode == 0, f"{passed.stdout}\n{passed.stderr}"
    assert "because the agent reported it was done" in passed.stdout and "no findings" in passed.stdout

    failed = _example(installed, "scenario_silent.yaml", "forgetful", tmp_path / "state")
    assert failed.returncode == 1, f"{failed.stdout}\n{failed.stderr}"
    out = failed.stdout
    assert "\nfail (1)\n" in out, out
    found = out[out.index("\nfail (1)") : out.index("\nscorecard")]
    assert NO_FOLLOW_UP in found and PATTERN in found
