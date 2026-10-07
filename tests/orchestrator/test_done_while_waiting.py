"""An agent that asks a person, "follows up" seconds later in the same wake, tells its owner it reached out, and
reports done, as a real model did against the follow-up recipe. The run once passed (exit 0) with "follow-ups made
1, early 1" and a wait still open: the instant follow-up read as chasing the ask, so DONE was taken at its word."""

from __future__ import annotations

from pathlib import Path

from minutehand.checks.runner import evaluate_run
from minutehand.domain.checks import FindingKind
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.scenario import PersonAsked, Silent
from tests.orchestrator.rig import person, rigged, scenario


async def test_done_after_an_instant_follow_up_with_the_ask_unanswered_is_not_finished_and_reviewed(
    tmp_path: Path,
) -> None:
    played = scenario(
        people=[person("owner", Silent()), person("dania", Silent())],
        ticket_fates=[],
        expect=[PersonAsked(person="dania")],
    )
    async with rigged(tmp_path) as rig:
        record, store, _ = await rig.run(played, rig.agent("ask_chase_and_close"))

    assert record.stop is StopReason.AGENT_DONE
    result = evaluate_run(played, store.events(), record.wakes, store.replies(), stop=record.stop)

    assert (result.effectiveness.follow_ups_made, result.effectiveness.follow_ups_early) == (1, 1)
    assert result.verdict.kind is VerdictKind.UNFINISHED and result.exit_code == 3
    assert result.verdict.words == (
        "Not finished: no check failed, but the agent reported it was done with 1 ask it made still unanswered "
        "and never followed up after the wake it asked in (dania)."
    )
    [instant] = [f for f in result.findings if f.check == "nagged"]
    assert instant.kind is FindingKind.REVIEW
    assert instant.message == (
        "dania was followed up in the same wake as the ask, 0 seconds after it, before they could have answered"
    )
