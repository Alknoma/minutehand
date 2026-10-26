"""The package as a user installs it: built by `uv build`, installed into a clean environment, and run."""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.packaging.conftest import TWINE, VERSION, Dist, Installed, checked, outside_this_project, run, tool

pytestmark = [pytest.mark.packaging, pytest.mark.timeout(900)]


def tracked_package_files() -> set[str]:
    """Every file git tracks under src/minutehand, as its path inside the wheel."""
    listed = checked("git", "ls-files", "src/minutehand").stdout.split()
    return {name.removeprefix("src/") for name in listed}


def test_uv_build_makes_an_sdist_and_a_wheel_of_this_version(dist: Dist) -> None:
    assert dist.sdist.name == f"minutehand-{VERSION}.tar.gz"
    assert dist.wheel.name == f"minutehand-{VERSION}-py3-none-any.whl"
    with tarfile.open(dist.sdist) as sdist:
        names = {member.name.split("/", 1)[1] for member in sdist.getmembers() if "/" in member.name}
    assert {"pyproject.toml", "README.md", "LICENSE", "NOTICE", "src/minutehand/cli.py"} <= names


def test_the_wheel_holds_every_file_under_src_minutehand(dist: Dist) -> None:
    with zipfile.ZipFile(dist.wheel) as wheel:
        shipped = set(wheel.namelist())
    missing = sorted(tracked_package_files() - shipped)
    assert missing == [], f"the wheel leaves out {missing}; add their pattern to [tool.hatch.build.targets.wheel]"
    assert f"minutehand-{VERSION}.dist-info/entry_points.txt" in shipped
    assert "minutehand/py.typed" in shipped


def test_the_sdist_holds_nothing_git_does_not_track(dist: Dist) -> None:
    """A scratch file, a local secret or a build leftover in the tree never reaches the sdist."""
    with tarfile.open(dist.sdist) as sdist:
        names = {m.name.split("/", 1)[1] for m in sdist.getmembers() if m.isfile()}
    tracked = set(checked("git", "ls-files").stdout.split())
    assert sorted(names - tracked - {"PKG-INFO"}) == []


def _contents(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as opened:
        return {name: opened.read(name) for name in opened.namelist()}


def test_the_wheel_built_from_the_sdist_is_the_wheel_built_from_the_tree(dist: Dist, tree_wheel: Path) -> None:
    """The sdist holds everything a wheel is built from: what PyPI's wheel installs is what a user who builds
    the sdist themselves installs."""
    from_sdist, from_tree = _contents(dist.wheel), _contents(tree_wheel)
    assert sorted(set(from_tree) ^ set(from_sdist)) == []
    differ = sorted(name for name in from_tree if from_tree[name] != from_sdist[name])
    assert differ == []


def test_the_metadata_names_the_licence_and_passes_twine_check(dist: Dist) -> None:
    with zipfile.ZipFile(dist.wheel) as wheel:
        metadata = wheel.read(f"minutehand-{VERSION}.dist-info/METADATA").decode()
        shipped = set(wheel.namelist())
    headers = metadata.split("\n\n", 1)[0].splitlines()
    written = headers[0].removeprefix("Metadata-Version: ")
    # License-Expression and License-File exist from 2.4 (PEP 639); PyPI accepts up to 2.5.
    assert written in ("2.4", "2.5"), headers[0]
    assert "License-Expression: Apache-2.0" in headers
    assert "License-File: LICENSE" in headers
    assert "License-File: NOTICE" in headers
    assert f"minutehand-{VERSION}.dist-info/licenses/LICENSE" in shipped
    assert f"minutehand-{VERSION}.dist-info/licenses/NOTICE" in shipped
    assert "Description-Content-Type: text/markdown" in headers

    twine = checked(
        tool("uv"), "tool", "run", "--from", TWINE, "twine", "check", "--strict", str(dist.sdist), str(dist.wheel)
    )
    assert twine.stdout.count("PASSED") == 2, twine.stdout


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
