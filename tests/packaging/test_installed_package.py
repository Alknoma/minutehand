"""The packages as a user installs them: built by `uv build --all-packages`, Minutehand installed into a clean
environment and the example agent's own dependencies (minutehand-agent among them) into another, and run."""

from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.packaging.conftest import (
    AGENT_PACKAGE,
    EXAMPLE,
    TWINE,
    VERSION,
    Dist,
    Installed,
    TreeWheels,
    checked,
    outside_this_project,
    run,
    tool,
)

pytestmark = [pytest.mark.packaging, pytest.mark.timeout(900)]

NO_FOLLOW_UP = "follows_up_when_due: rosa's answer was due and no follow-up came by an hour later"
PATTERN = "pattern expiry_on_every_wait: An expiry on every wait."


def tracked_package_files() -> set[str]:
    """Every file git tracks under src/minutehand, as its path inside the wheel."""
    listed = checked("git", "ls-files", "src/minutehand").stdout.split()
    return {name.removeprefix("src/") for name in listed}


def tracked_agent_files() -> set[str]:
    """Every file git tracks under the agent package's source, as its path inside its wheel."""
    listed = checked("git", "ls-files", "src/minutehand_agent", cwd=AGENT_PACKAGE).stdout.split()
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
    assert dist.agent_sdist.name.startswith("minutehand_agent-") and dist.agent_wheel.name.endswith("-py3-none-any.whl")


def test_the_agent_wheel_holds_every_file_of_minutehand_agent_and_nothing_of_minutehand(dist: Dist) -> None:
    with zipfile.ZipFile(dist.agent_wheel) as wheel:
        shipped = set(wheel.namelist())
    assert sorted(tracked_agent_files() - shipped) == []
    assert "minutehand_agent/py.typed" in shipped
    assert [n for n in shipped if n.startswith("minutehand/")] == []


def test_the_wheel_holds_every_file_under_src_minutehand(dist: Dist) -> None:
    with zipfile.ZipFile(dist.wheel) as wheel:
        shipped = set(wheel.namelist())
    missing = sorted(tracked_package_files() - shipped)
    assert missing == [], f"the wheel leaves out {missing}; add their pattern to [tool.hatch.build.targets.wheel]"
    assert f"minutehand-{VERSION}.dist-info/entry_points.txt" in shipped
    assert "minutehand/py.typed" in shipped


def _sdist_files(sdist: Path) -> set[str]:
    with tarfile.open(sdist) as opened:
        return {m.name.split("/", 1)[1] for m in opened.getmembers() if m.isfile()}


def test_the_sdist_holds_nothing_git_does_not_track(dist: Dist) -> None:
    """A scratch file, a local secret or a build leftover in the tree never reaches the sdist."""
    tracked = set(checked("git", "ls-files").stdout.split())
    assert sorted(_sdist_files(dist.sdist) - tracked - {"PKG-INFO"}) == []
    # hatchling copies the repository's .gitignore into every sdist it builds; for the agent package it is the root's.
    agent_tracked = set(checked("git", "ls-files", cwd=AGENT_PACKAGE).stdout.split())
    assert sorted(_sdist_files(dist.agent_sdist) - agent_tracked - {"PKG-INFO", ".gitignore"}) == []


def _contents(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as opened:
        return {name: opened.read(name) for name in opened.namelist()}


def test_the_wheel_built_from_the_sdist_is_the_wheel_built_from_the_tree(dist: Dist, tree_wheels: TreeWheels) -> None:
    """The sdist holds everything a wheel is built from: what PyPI's wheel installs is what a user who builds
    the sdist themselves installs. For both distributions."""
    for built, tree in ((dist.wheel, tree_wheels.wheel), (dist.agent_wheel, tree_wheels.agent_wheel)):
        from_sdist, from_tree = _contents(built), _contents(tree)
        assert sorted(set(from_tree) ^ set(from_sdist)) == [], built.name
        differ = sorted(name for name in from_tree if from_tree[name] != from_sdist[name])
        assert differ == [], built.name


def _headers(wheel: Path, name: str) -> tuple[list[str], set[str]]:
    with zipfile.ZipFile(wheel) as opened:
        metadata = opened.read(f"{name}-{VERSION}.dist-info/METADATA").decode()
        shipped = set(opened.namelist())
    headers = metadata.split("\n\n", 1)[0].splitlines()
    written = headers[0].removeprefix("Metadata-Version: ")
    # License-Expression and License-File exist from 2.4 (PEP 639); PyPI accepts up to 2.5.
    assert written in ("2.4", "2.5"), headers[0]
    assert "License-Expression: FSL-1.1-ALv2" in headers
    assert "License-File: LICENSE.md" in headers
    assert f"{name}-{VERSION}.dist-info/licenses/LICENSE.md" in shipped
    assert "Description-Content-Type: text/markdown" in headers
    return headers, shipped


def test_the_metadata_names_the_licence_and_passes_twine_check(dist: Dist) -> None:
    headers, _ = _headers(dist.wheel, "minutehand")
    assert f"Requires-Dist: minutehand-agent=={VERSION}" in headers
    agent_headers, _ = _headers(dist.agent_wheel, "minutehand_agent")
    assert [h for h in agent_headers if h.startswith("Requires-Dist:")] == []

    files = (dist.sdist, dist.wheel, dist.agent_sdist, dist.agent_wheel)
    twine = checked(tool("uv"), "tool", "run", "--from", TWINE, "twine", "check", "--strict", *map(str, files))
    assert twine.stdout.count("PASSED") == 4, twine.stdout


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
    checked(str(installed.python), "-c", "import minutehand_agent", cwd=installed.venv, env=outside_this_project())

    helped = run(str(installed.minutehand), "--help", cwd=installed.venv, env=outside_this_project())

    assert helped.returncode == 0, helped.stderr
    assert "usage: minutehand" in helped.stdout and "run a scenario against an agent" in helped.stdout

    versioned = checked(str(installed.minutehand), "--version", cwd=installed.venv, env=outside_this_project())
    assert versioned.stdout == f"minutehand {VERSION}\n"


PLUGIN_SUITE = """
import httpx
import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed


@pytest.fixture
def minutehand_spec():
    return CreateWorld(
        seed=Seed.model_validate({"people": [{"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com"}]}),
        claims=Claims(tokens=["xoxb-installed"]),
    )


def test_a_message_posted_through_the_proxy_is_in_the_world(minutehand, minutehand_world):
    env = minutehand.environment()
    headers = {"Authorization": "Bearer xoxb-installed"}
    with httpx.Client(proxy=env["HTTPS_PROXY"], verify=env["SSL_CERT_FILE"], headers=headers) as slack:
        channel = slack.get("https://slack.com/api/conversations.list").json()["channels"][0]["id"]
        posted = slack.post("https://slack.com/api/chat.postMessage", json={"channel": channel, "text": "installed"})
    assert posted.json()["ok"] is True
    minutehand_world.assert_message(containing="installed")
"""


def test_the_pytest_plugin_serves_its_fixtures_from_the_installed_wheel(
    installed_with_pytest: Installed, tmp_path: Path
) -> None:
    """A user's suite, outside this checkout: pytest finds the plugin by its entry point in the installed wheel,
    and `minutehand` starts a server from that wheel for the session."""
    (tmp_path / "test_suite.py").write_text(PLUGIN_SUITE)
    env = {k: v for k, v in outside_this_project().items() if k != "MINUTEHAND_URL"}

    ran = run(str(installed_with_pytest.python), "-m", "pytest", "-q", "-p", "no:cacheprovider", cwd=tmp_path, env=env)

    assert ran.returncode == 0, f"{ran.stdout}\n{ran.stderr}"
    assert "1 passed" in ran.stdout
    plugin = checked(
        str(installed_with_pytest.python),
        "-c",
        "import minutehand.testing.plugin as p; print(p.__file__)",
        cwd=tmp_path,
        env=env,
    )
    assert Path(plugin.stdout.strip()).is_relative_to(installed_with_pytest.venv)


def test_the_agent_environment_holds_minutehand_agent_and_never_minutehand(agent_env: Installed) -> None:
    """What the example's README has an agent install: its Slack client and minutehand-agent, which brings nothing
    else, and Minutehand nowhere in it."""
    listed = checked(
        str(agent_env.python),
        "-c",
        "import importlib.metadata as m; print(sorted(d.metadata['Name'].lower() for d in m.distributions()))",
        env=outside_this_project(),
    )
    assert listed.stdout.strip() == "['minutehand-agent', 'slack_sdk']", listed.stdout
    missing = run(str(agent_env.python), "-c", "import minutehand", env=outside_this_project())
    assert "No module named 'minutehand'" in missing.stderr, missing.stderr


def _example(
    installed: Installed, agent_env: Installed, scenario: str, behaviour: str, state: Path
) -> subprocess.CompletedProcess[str]:
    """The example's own command line, from its folder: Minutehand from its clean environment, and the agent from
    its own, which holds slack_sdk and minutehand-agent and not Minutehand."""
    env = {**outside_this_project(), "AGENT_BEHAVIOUR": behaviour}
    argv = [str(installed.minutehand), "run", scenario, "--agent", "agent.yaml", "--state", str(state)]
    return run(*argv, "--", str(agent_env.python), "agent.py", cwd=EXAMPLE, env=env)


def test_the_installed_command_passes_the_example_and_fails_the_forgetful_agent(
    installed: Installed, agent_env: Installed, tmp_path: Path
) -> None:
    passed = _example(installed, agent_env, "scenario.yaml", "diligent", tmp_path / "state")
    assert passed.returncode == 0, f"{passed.stdout}\n{passed.stderr}"
    assert "\n  Passed: no check failed, and the agent reported it was done.\n" in passed.stdout
    assert "\nfail (" not in passed.stdout and "owen told what rosa said ('lakeside hall'): met by" in passed.stdout

    failed = _example(installed, agent_env, "scenario_silent.yaml", "forgetful", tmp_path / "state")
    assert failed.returncode == 1, f"{failed.stdout}\n{failed.stderr}"
    out = failed.stdout
    assert "\nfail (1)\n" in out, out
    found = out[out.index("\nfail (1)") : out.index("\nscorecard")]
    assert NO_FOLLOW_UP in found and PATTERN in found
