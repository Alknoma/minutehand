"""What a user installs, built from this tree: the sdist and wheel, a clean environment holding the wheel,
and the container image.

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


@dataclass(frozen=True)
class Installed:
    """A fresh virtual environment holding the wheel and its dependencies, and nothing from the dev group."""

    venv: Path

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def minutehand(self) -> Path:
        return self.venv / "bin" / "minutehand"


@pytest.fixture(scope="session")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Dist:
    """What `uv build` makes, as the release workflow runs it: the sdist, and the wheel built FROM that sdist."""
    out = tmp_path_factory.mktemp("dist")
    checked(tool("uv"), "build", "--out-dir", str(out))
    sdists, wheels = sorted(out.glob("*.tar.gz")), sorted(out.glob("*.whl"))
    assert len(sdists) == 1 and len(wheels) == 1, sorted(p.name for p in out.iterdir())
    return Dist(sdist=sdists[0], wheel=wheels[0])


@pytest.fixture(scope="session")
def tree_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The wheel built straight from this tree, without the sdist between."""
    out = tmp_path_factory.mktemp("tree_wheel")
    checked(tool("uv"), "build", "--wheel", "--out-dir", str(out))
    wheels = sorted(out.glob("*.whl"))
    assert len(wheels) == 1, sorted(p.name for p in out.iterdir())
    return wheels[0]


def _installed(wheel: Path, venv: Path, *also: str) -> Installed:
    uv = tool("uv")
    checked(uv, "venv", "--python", sys.executable, str(venv))
    checked(uv, "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel), *also)
    return Installed(venv=venv)


@pytest.fixture(scope="session")
def installed(dist: Dist, tmp_path_factory: pytest.TempPathFactory) -> Installed:
    return _installed(dist.wheel, tmp_path_factory.mktemp("installed") / "venv")


@pytest.fixture(scope="session")
def installed_with_pytest(dist: Dist, tmp_path_factory: pytest.TempPathFactory) -> Installed:
    """The wheel and pytest, in an environment of their own: a user's suite, with the plugin found by its entry
    point. Separate from `installed`, which must hold the wheel's dependencies and nothing else."""
    return _installed(dist.wheel, tmp_path_factory.mktemp("installed_with_pytest") / "venv", "pytest")


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
