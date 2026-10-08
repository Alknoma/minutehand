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
