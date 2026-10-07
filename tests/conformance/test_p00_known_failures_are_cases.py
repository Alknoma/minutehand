"""The known-failures table names only cases the suite runs, once each: a strict expected failure catches an entry
whose case started passing, and this catches one whose case no longer exists (renamed, or its provider gone), which
would otherwise sit in the table forever excusing nothing."""

from __future__ import annotations

import importlib
from pathlib import Path

from tests.conformance.harness import Case
from tests.conformance.known_failures import KNOWN

HERE = Path(__file__).parent


def every_case() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for path in sorted(HERE.glob("test_p*.py")):
        module = importlib.import_module(f"tests.conformance.{path.stem}")
        for name in dir(module):
            test = getattr(module, name)
            if not name.startswith("test_") or not callable(test):
                continue
            for mark in getattr(test, "pytestmark", []):
                if mark.name == "parametrize" and mark.args[0] == "case":
                    found |= {(c.provider, c.prop.value, c.case) for c in mark.args[1] if isinstance(c, Case)}
    return found


def test_every_known_failure_names_a_case_the_suite_runs_and_names_it_once() -> None:
    named = [(k.provider, k.prop.value, k.case) for k in KNOWN]
    stale = sorted(set(named) - every_case())
    assert not stale, f"the known-failures table names cases the suite does not run: {stale}"
    twice = sorted({n for n in named if named.count(n) > 1})
    assert not twice, f"the known-failures table names these twice: {twice}"
    assert all(k.observed.strip() for k in KNOWN), "an entry gives no observed failure"
