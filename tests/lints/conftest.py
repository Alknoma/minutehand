"""Plant a source tree in tmp_path: a lint is only a check if it is seen to fail."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Protocol

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"


class Plant(Protocol):
    def __call__(self, files: dict[str, str]) -> Path: ...


@pytest.fixture
def plant(tmp_path: Path) -> Plant:
    def write(files: dict[str, str]) -> Path:
        for rel, text in files.items():
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
        return tmp_path

    return write
