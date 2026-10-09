"""Whose each model call Minutehand made was, as `model_calls.side` says it: a person's, a declared service's, a
judged check's, or the reviewer's of the agent's effects. The run is the read model's own fixture, a run written by
hand, with the calls a judge and the reviewer made added to its world."""

from __future__ import annotations

from pathlib import Path

import pytest

from minutehand.adapters.query.reader import open_model, query
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.conversation import SIDE, PersonCall, Side, Wrote
from tests.query.fixture import T0, build_fixture


@pytest.mark.parametrize("wrote", list(Wrote))
def test_every_kind_of_call_has_a_side(wrote: Wrote) -> None:
    assert wrote in SIDE


def test_a_judges_and_the_reviewers_calls_are_listed_as_theirs(tmp_path: Path) -> None:
    run = build_fixture(tmp_path / "state")
    world = SqliteStore(run.state / "runs" / run.run_id / "world.db", run.run_id, RunClock(T0))
    for n, wrote in enumerate((Wrote.JUDGEMENT, Wrote.REVIEW, Wrote.FACT_CHECK)):
        world.record_person_call(
            PersonCall(
                key=f"{n}" * 64,
                person="sofia" if wrote is Wrote.FACT_CHECK else None,
                wrote=wrote,
                model="judge-model",
                prompt_version=f"{wrote.value}/1",
                input_tokens=10,
                output_tokens=2,
                answer="{}",
                sim_time=T0,
                wake=2,
            )
        )
    world.close()
    db = open_model(run.state, run.run_id)
    try:
        listed = query(db, "SELECT wrote, side FROM model_calls WHERE side != 'agent' ORDER BY person_call_id").rows
    finally:
        db.close()
    assert [list(r) for r in listed] == [
        ["reply", Side.PERSON.value],
        ["judgement", Side.JUDGE.value],
        ["review", Side.ASSESSOR.value],
        ["fact_check", Side.JUDGE.value],
    ]
