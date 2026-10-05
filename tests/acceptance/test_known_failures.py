"""The known-failures table is honest: every entry names a test that exists, and `docs/acceptance.md` carries exactly
the table `known_failures.py` holds, so the page cannot go stale while the code changes."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.acceptance.known_failures import KNOWN_FAILURES

HERE = Path(__file__).resolve().parent
PAGE = HERE.parents[1] / "docs" / "acceptance.md"


def _tests() -> set[str]:
    found: set[str] = set()
    for module in HERE.glob("test_*.py"):
        for node in ast.parse(module.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                found.add(f"{module.name}::{node.name}")
    return found


def row(test: str) -> str:
    known = KNOWN_FAILURES[test]
    return f"| `{test}` | {known.observed} | {known.promised} |"


def test_every_known_failure_names_a_test_that_exists() -> None:
    assert sorted(set(KNOWN_FAILURES) - _tests()) == []


def test_the_page_lists_exactly_the_known_failures() -> None:
    listed = [line for line in PAGE.read_text().splitlines() if line.startswith("| `test_")]
    assert listed == [row(test) for test in KNOWN_FAILURES]
