"""Work left undone (`checks/judged/undone.py`): an agent that does nothing does nothing wrong, and passed every other
check; the window read whole names the work its own instructions give it that it left undone."""

from __future__ import annotations

from pathlib import Path

from minutehand.checks.judged.undone import UNDONE_PROMPT_VERSION, WorkUndone, shown_whole
from minutehand.domain.checks import FindingKind, RunView
from minutehand.domain.conversation import SIDE, Side, Wrote
from minutehand.domain.items import AssessedKind
from tests.support.people import people_model

TRIAL = Path(__file__).parents[2] / "data" / "trial"


def _trial(name: str) -> RunView:
    return RunView.model_validate_json((TRIAL / f"{name}.json").read_text(encoding="utf-8"))


def _idle(run: RunView) -> RunView:
    """The same world and instructions, with the agent having done nothing in it."""
    return run.model_copy(update={"typed": [], "replies": []})


async def test_an_agent_that_did_nothing_with_work_its_instructions_give_it_left_it_undone() -> None:
    report = await WorkUndone().judge(_idle(_trial("seed1_nagging_and_polling")), people_model(), failed=frozenset())

    [undone] = report.findings
    assert undone.check == "work_left_undone" and undone.kind is FindingKind.REVIEW
    assert undone.assessed is not None and undone.assessed.kind is AssessedKind.WRONG_ACTION
    assert undone.judged is not None and undone.judged.prompt_version == UNDONE_PROMPT_VERSION
    assert report.notes == [f"the window read whole for work left undone by people-fake ({UNDONE_PROMPT_VERSION})"]


async def test_an_agent_that_did_its_work_has_nothing_left_undone() -> None:
    report = await WorkUndone().judge(_trial("seed1_nagging_and_polling"), people_model(), failed=frozenset())

    assert report.findings == []


def test_it_reads_the_agents_instructions_and_does_not_run_without_them() -> None:
    run = _trial("seed1_nagging_and_polling")
    shown = shown_whole(run)

    assert shown.startswith("The agent's own instructions, as it gave them to its model:\nYou are Owen Hart's")
    assert "What the agent did, oldest first:\n-" in shown and "How each item stood at the end:" in shown
    assert "What the agent did, oldest first:\n(nothing)" in shown_whole(_idle(run))
    assert WorkUndone().applies(run) and not WorkUndone().applies(run.model_copy(update={"agent_instructions": []}))
    assert SIDE[Wrote.UNDONE] is Side.ASSESSOR, "its calls are the assessor's, in model_calls"
