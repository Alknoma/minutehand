"""A run in which Minutehand broke answering a call (`CallOutcome.INTERNAL_ERROR`) is not scored as the agent's: its
verdict is TOOL_FAILED, exit 4, naming the call, whatever the checks said; the same for a standing world's checks.
A refusal, an injected fault or an operation the fake does not implement is the agent's world, and is scored."""

from __future__ import annotations

from pathlib import Path

import pytest

from minutehand import session
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import StandingWorld
from minutehand.checks.runner import evaluate, exit_code
from minutehand.domain.run import RunRecord, StopReason, VerdictKind
from minutehand.domain.scenario import PersonAsked, Silent
from minutehand.domain.world import CallFailure, CallOutcome, Exchange
from tests.checks.world import START, Log, person, scenario, view

OWNER = person("owner")
SOFIA = person("sofia", Silent())
BROKE = "minutehand internal error while answering slack POST /api/chat.postMessage: KeyError: 'C1'"


def _call(answer: CallOutcome) -> Exchange:
    failed = answer in (CallOutcome.INTERNAL_ERROR, CallOutcome.NOT_IMPLEMENTED)
    return Exchange(
        method="POST",
        host="slack.com",
        path="/api/chat.postMessage",
        status={CallOutcome.INTERNAL_ERROR: 500, CallOutcome.NOT_IMPLEMENTED: 501}.get(answer, 200),
        outcome=answer,
        failure=CallFailure(kind=answer, message=BROKE, exception_type="builtins.KeyError", traceback="Traceback")
        if failed
        else None,
    )


def test_a_run_whose_fake_broke_is_not_scored_and_exits_4_naming_the_call() -> None:
    log = Log()
    log.message([SOFIA], 1)
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="owner")]), log)
    scored = evaluate(world, stop=StopReason.AGENT_DONE)
    assert scored.verdict.kind is VerdictKind.FAILED, "the expectation fails on its own"

    broken = world.model_copy(update={"broken_calls": [_call(CallOutcome.INTERNAL_ERROR)]})
    result = evaluate(broken, stop=StopReason.AGENT_DONE)

    assert result.verdict.kind is VerdictKind.TOOL_FAILED and result.exit_code == 4
    assert result.verdict.words == (
        "Not scored: Minutehand itself failed while answering 1 call "
        f"(first: POST slack.com/api/chat.postMessage: {BROKE}); the agent is not judged on this run."
    )
    assert exit_code([scored, result]) == 4


def _standing(tmp_path: Path) -> tuple[StandingWorld, SqliteStore]:
    registry = Registry.installed()
    played = scenario(OWNER, SOFIA)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "world", clock)
    world = StandingWorld(
        scenario=played,
        store=store,
        clock=clock,
        provider=lambda k: registry.provider(next(m for m in registry.manifests if m.key == k)),
        inbound=[],
        signing={},
        scripted=False,
    )
    world.open([])
    return world, store


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        (CallOutcome.INTERNAL_ERROR, VerdictKind.TOOL_FAILED),
        (CallOutcome.NOT_IMPLEMENTED, VerdictKind.PASSED),
        (CallOutcome.INJECTED_FAULT, VerdictKind.PASSED),
        (CallOutcome.REFUSED, VerdictKind.PASSED),
        (CallOutcome.ANSWERED, VerdictKind.PASSED),
    ],
)
async def test_a_standing_worlds_checks_say_the_tool_failed_only_for_minutehands_own_error(
    answer: CallOutcome, verdict: VerdictKind, tmp_path: Path
) -> None:
    world, store = _standing(tmp_path)
    try:
        head = store.head()
        store.attach(_call(answer), first_seq=head + 1, last_seq=head, provider="slack")
        result = await world.checks(stop=StopReason.CLOSED)
    finally:
        store.close()
    assert result.verdict.kind is verdict


async def test_a_played_runs_score_says_the_tool_failed_when_a_call_broke(tmp_path: Path) -> None:
    played = scenario(OWNER, SOFIA)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    try:
        store.attach(_call(CallOutcome.INTERNAL_ERROR), first_seq=1, last_seq=0, provider="slack")
        record = RunRecord(
            run_id="run",
            scenario=played.name,
            seed=played.seed,
            started_at=START,
            ended_at=START,
            wall_seconds=0,
            stop=StopReason.AGENT_DONE,
            wakes=[],
        )
        result = await session._Judge(played, None, judging=False).score(record, store)  # pyright: ignore[reportPrivateUsage]
    finally:
        store.close()
    assert result.verdict.kind is VerdictKind.TOOL_FAILED and result.exit_code == 4
