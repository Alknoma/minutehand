"""An agent that asks a person, "follows up" seconds later in the same wake, tells its owner it reached out, and
reports done, as a real model did against the follow-up recipe. The run once passed (exit 0) with a wait still open:
the instant follow-up read as chasing the ask, so DONE was taken at its word. Whether that is wrong is the team's to
say: two rules say it, one failing a done reported over an open ask, one reviewing a follow-up within the hour of the
ask. Without them nothing judges it."""

from __future__ import annotations

from pathlib import Path

from minutehand.checks.runner import evaluate_run
from minutehand.domain.checks import FindingKind
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.scenario import PersonAsked, Silent
from tests.orchestrator.rig import person, rigged, scenario
from tests.support.rules import rules

DONE_WHILE_WAITING = """
- id: not_done_while_waiting
  when: {stopped: [agent_done]}
  count: {asks: {open_at: end}}
  at_most: 0
  message: "reported done with {rule.count} unanswered asks"
- id: waits_before_following_up
  each: ask
  where: {person_not: [owner]}
  count: {follow_ups: {}, until: ask+PT1H}
  at_most: 0
  severity: review
  message: "{person.key} was followed up within the hour of the ask, before they could have answered"
"""


async def test_done_after_an_instant_follow_up_with_the_ask_unanswered_fails_and_is_reviewed_by_the_teams_rules(
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

    judged = evaluate_run(
        played, store.events(), record.wakes, store.replies(), stop=record.stop, rules=rules(DONE_WHILE_WAITING)
    )
    said = {(f.check, f.kind, f.message) for f in judged.findings if f.check != "expectations"}
    assert said == {
        ("not_done_while_waiting", FindingKind.FAIL, "reported done with 2 unanswered asks"),
        (
            "waits_before_following_up",
            FindingKind.REVIEW,
            "dania was followed up within the hour of the ask, before they could have answered",
        ),
    }
    assert judged.verdict.kind is VerdictKind.FAILED and judged.effectiveness.follow_ups_made == 1

    unjudged = evaluate_run(played, store.events(), record.wakes, store.replies(), stop=record.stop, rules=[])
    assert unjudged.verdict.kind is VerdictKind.PASSED, "with only the expectation declared, nothing else is asked"
