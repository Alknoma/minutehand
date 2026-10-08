"""The version is written once, in pyproject.toml; the command and the package answer with that one."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import minutehand
from minutehand import cli

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def written() -> str:
    version: str = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    return version


def test_the_package_version_is_the_one_pyproject_writes() -> None:
    assert minutehand.__version__ == written()


def test_minutehand_version_prints_the_package_version_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["--version"])

    assert exited.value.code == 0
    assert capsys.readouterr().out == f"minutehand {written()}\n"


AGENT = PYPROJECT.parent / "packages" / "minutehand-agent"


def test_minutehand_agent_is_released_at_the_same_version_and_minutehand_pins_it() -> None:
    """`minutehand-agent` is released with `minutehand`: the same version, and `minutehand` requires exactly it, so
    the receiver and the package an agent imports always speak the same wire."""
    agent = tomllib.loads((AGENT / "pyproject.toml").read_text())["project"]
    pins = [
        d for d in tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"] if d.startswith("minutehand-agent")
    ]
    assert agent["version"] == written()
    assert pins == [f"minutehand-agent=={written()}"]


def test_minutehand_agent_ships_the_same_licence() -> None:
    assert (AGENT / "LICENSE.md").read_bytes() == (PYPROJECT.parent / "LICENSE.md").read_bytes()
