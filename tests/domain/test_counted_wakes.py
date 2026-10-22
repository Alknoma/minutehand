"""Which wakes count against the run's wake limit (`domain.run.counts_against_limit`): a wake the agent's own timer
alone began, in which it made no call and wrote nothing, is its runtime's timer coming round, and in a run with an
end it is not counted."""

from __future__ import annotations

import pytest

from minutehand.domain.run import counts_against_limit

IDLE = {"calls": 0, "world_changes": 0, "memory_writes": 0}


def test_an_idle_timer_wake_in_a_run_with_an_end_is_not_counted() -> None:
    assert not counts_against_limit(timer_only=True, ends=True, **IDLE)


def test_an_idle_timer_wake_in_a_run_without_an_end_is_counted() -> None:
    assert counts_against_limit(timer_only=True, ends=False, **IDLE)


def test_an_idle_wake_something_else_began_is_counted() -> None:
    assert counts_against_limit(timer_only=False, ends=True, **IDLE)


@pytest.mark.parametrize("did", ["calls", "world_changes", "memory_writes"])
def test_a_timer_wake_in_which_the_agent_did_something_is_counted(did: str) -> None:
    assert counts_against_limit(timer_only=True, ends=True, **{**IDLE, did: 1})
