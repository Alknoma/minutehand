"""Every test here carries the `acceptance` marker, and every test `known_failures.py` lists is a strict expected
failure: it fails the run if it passes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.acceptance.known_failures import KNOWN_FAILURES

HERE = Path(__file__).resolve().parent


def known_id(item: pytest.Item) -> str:
    """`<file>.py::<function>`: the key `KNOWN_FAILURES` uses, with any parameters left off."""
    function = getattr(item, "originalname", item.name)
    return f"{item.path.relative_to(HERE).as_posix()}::{function}"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if not item.path.is_relative_to(HERE):
            continue
        item.add_marker(pytest.mark.acceptance)
        known = KNOWN_FAILURES.get(known_id(item))
        if known is not None:
            item.add_marker(pytest.mark.xfail(strict=True, reason=f"known failure: {known.observed}"))
