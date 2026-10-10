"""The proactive agent's `wake` package: a sub-agent proposes when to wake, and the guard holds every proposal to the
rules, whatever the model said: nothing open, no wake; something open, always a wake; never before it is due, never in
the past, always in working hours."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.recipes  # Pydantic AI is the `recipes` dependency group's
TestModel = pytest.importorskip("pydantic_ai.models.test").TestModel

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "proactive_agent"
MON_10 = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)  # Monday 10:00 in London
FRI_17 = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)  # Friday 17:00 in London


@pytest.fixture
def wake() -> Iterator[ModuleType]:
    sys.path.insert(0, str(EXAMPLE))
    try:
        module = importlib.import_module("wake")  # the example's own package, by its name
        importlib.import_module("wake.decide")
        importlib.import_module("wake.guard")
        yield module
    finally:
        sys.path.remove(str(EXAMPLE))
        for name in [n for n in sys.modules if n == "wake" or n.startswith("wake.")]:
            sys.modules.pop(name)


def test_the_guard_keeps_the_rules_whatever_was_proposed(wake: ModuleType) -> None:
    guarded = wake.guard.guarded

    due = MON_10 + timedelta(days=1)
    assert guarded(None, [], MON_10) is None, "nothing open, no wake"
    assert guarded(None, [due], MON_10) == due, "something open and nothing proposed: the earliest due moment"
    assert guarded(MON_10 + timedelta(hours=1), [due], MON_10) == due, "never before it is due"
    assert guarded(MON_10 - timedelta(days=3), [MON_10 - timedelta(days=1)], MON_10) == MON_10 + timedelta(minutes=1)
    monday_open = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)  # 09:00 in London
    assert guarded(FRI_17 + timedelta(hours=1), [FRI_17], FRI_17) == monday_open, "the weekend is skipped"


def test_a_model_proposing_too_early_is_held_to_the_due_moment_and_a_good_proposal_is_kept(
    wake: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = wake.Open(what="Sam's answer", since=MON_10, due=MON_10 + timedelta(days=1))
    early = TestModel(custom_output_args={"wake_at": MON_10.isoformat(), "why": "chase now"})
    with wake.decide.decider.override(model=early):
        assert wake.next_wake([item], MON_10) == item.due
    later = MON_10 + timedelta(days=1, hours=3)
    sensible = TestModel(custom_output_args={"wake_at": later.isoformat(), "why": "he answers in the afternoon"})
    with wake.decide.decider.override(model=sensible):
        assert wake.next_wake([item], MON_10) == later


def test_a_model_that_fails_still_leaves_a_wake(wake: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    item = wake.Open(what="Sam's answer", since=MON_10, due=MON_10 + timedelta(days=1))

    def broken(*_: object, **__: object) -> object:
        raise RuntimeError("the model API is down")

    monkeypatch.setattr(wake.decide.decider, "run_sync", broken)
    assert wake.next_wake([item], MON_10) == item.due
