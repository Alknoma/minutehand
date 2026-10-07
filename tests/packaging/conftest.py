"""What a user installs, built from this tree: the sdist and wheel of `minutehand` and of `minutehand-agent`, a
clean environment holding Minutehand, another holding the example agent's own dependencies, and the container image.

Each is built once per session. These tests run only with `-m packaging`: they need a package index to
install the wheel's dependencies, and Docker to build the image.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "follow_up"
AGENT_PACKAGE = ROOT / "packages" / "minutehand-agent"
"""The distribution `minutehand-agent`: what an agent installs in its own environment."""
VERSION: str = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
TWINE = "twine==7.0.0"
"""The checker PyPI's own upload docs name; pinned so a release checks what this suite checked."""
IMAGE = "minutehand-pkg-test"
"""The image a user runs; `IMAGE-example` is the same plus the example agent's one library."""


def tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise RuntimeError(f"{name} is not on PATH; the packaging tests need it")
    return found


def run(*argv: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)


def checked(*argv: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    done = run(*argv, cwd=cwd, env=env)
    assert done.returncode == 0, f"{' '.join(argv)} exited {done.returncode}:\n{done.stdout}\n{done.stderr}"
    return done


def outside_this_project() -> dict[str, str]:
    """This process's environment without what makes this checkout importable or names its virtualenv."""
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME")}


@dataclass(frozen=True)
class Dist:
    sdist: Path
    wheel: Path
    agent_sdist: Path
    agent_wheel: Path


@dataclass(frozen=True)
class Installed:
    """A fresh virtual environment holding the wheels and their dependencies, and nothing from the dev group."""

    venv: Path

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def minutehand(self) -> Path:
        return self.venv / "bin" / "minutehand"


def _one(out: Path, pattern: str) -> Path:
    found = sorted(out.glob(pattern))
    assert len(found) == 1, (pattern, sorted(p.name for p in out.iterdir()))
    return found[0]


@pytest.fixture(scope="session")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Dist:
    """What `uv build --all-packages` makes, as the release workflow runs it: each distribution's sdist, and its wheel
    built FROM that sdist."""
    out = tmp_path_factory.mktemp("dist")
    checked(tool("uv"), "build", "--all-packages", "--out-dir", str(out))
    built = sorted(p.name for p in out.iterdir() if not p.name.startswith("."))  # uv writes a .gitignore there
    assert len(built) == 4, built
    return Dist(
        sdist=_one(out, f"minutehand-{VERSION}.tar.gz"),
        wheel=_one(out, f"minutehand-{VERSION}-*.whl"),
        agent_sdist=_one(out, f"minutehand_agent-{VERSION}.tar.gz"),
        agent_wheel=_one(out, f"minutehand_agent-{VERSION}-*.whl"),
    )


@dataclass(frozen=True)
class TreeWheels:
    wheel: Path
    agent_wheel: Path


@pytest.fixture(scope="session")
def tree_wheels(tmp_path_factory: pytest.TempPathFactory) -> TreeWheels:
    """The wheels built straight from this tree, without the sdist between."""
    out = tmp_path_factory.mktemp("tree_wheel")
    checked(tool("uv"), "build", "--all-packages", "--wheel", "--out-dir", str(out))
    return TreeWheels(
        wheel=_one(out, f"minutehand-{VERSION}-*.whl"), agent_wheel=_one(out, f"minutehand_agent-{VERSION}-*.whl")
    )


def _installed(venv: Path, *requirements: str) -> Installed:
    uv = tool("uv")
    checked(uv, "venv", "--python", sys.executable, str(venv))
    checked(uv, "pip", "install", "--python", str(venv / "bin" / "python"), *requirements)
    return Installed(venv=venv)


@pytest.fixture(scope="session")
def installed(dist: Dist, tmp_path_factory: pytest.TempPathFactory) -> Installed:
    """Minutehand's wheel, with minutehand-agent's (its one dependency not yet on the index) beside it."""
    return _installed(tmp_path_factory.mktemp("installed") / "venv", str(dist.wheel), str(dist.agent_wheel))


@pytest.fixture(scope="session")
def installed_with_pytest(dist: Dist, tmp_path_factory: pytest.TempPathFactory) -> Installed:
    """The wheel and pytest, in an environment of their own: a user's suite, with the plugin found by its entry
    point. Separate from `installed`, which must hold the wheel's dependencies and nothing else."""
    venv = tmp_path_factory.mktemp("installed_with_pytest") / "venv"
    return _installed(venv, str(dist.wheel), str(dist.agent_wheel), "pytest")


@pytest.fixture(scope="session")
def agent_env(dist: Dist, tmp_path_factory: pytest.TempPathFactory) -> Installed:
    """The example agent's own environment, as a user makes it: its Slack client and minutehand-agent
    (`pip install slack_sdk minutehand-agent`, the wheel standing in for the index), and never minutehand."""
    return _installed(tmp_path_factory.mktemp("agent_env") / "venv", "slack_sdk", str(dist.agent_wheel))


@pytest.fixture(scope="session")
def image() -> Iterator[str]:
    """The image a user runs, and its `example` target, built from this tree and removed afterwards."""
    docker = tool("docker")
    checked(docker, "build", "--tag", IMAGE, ".")
    checked(docker, "build", "--target", "example", "--tag", f"{IMAGE}-example", ".")
    try:
        yield IMAGE
    finally:
        run(docker, "image", "rm", "--force", IMAGE, f"{IMAGE}-example")
