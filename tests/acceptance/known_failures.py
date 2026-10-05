"""Promises broken on trunk today, one entry per acceptance test that fails because of one.

A listed test is expected to fail, and FAILS THE RUN if it passes: whoever fixes the promise removes its entry in the
same change. An unlisted failure fails the run as any failure does. `docs/acceptance.md` carries this table, and
`test_known_failures.py` holds the two to each other and every entry to a test that exists.
"""

from __future__ import annotations

from typing import Final, NamedTuple


class KnownFailure(NamedTuple):
    observed: str
    """What the test saw, in one line."""
    promised: str
    """What the documentation promises instead, and where."""


KNOWN_FAILURES: Final[dict[str, KnownFailure]] = {
    "test_verdicts.py::test_a_run_with_nothing_expected_and_nothing_done_is_never_passed": KnownFailure(
        observed="no expectation, no wait, no message, one wake that changed nothing: verdict passed, exit 0",
        promised="design.md, The verdict: NOT_JUDGED when nothing was there to judge, never PASSED; a `run` always "
        "has a wake, so as written the rule never fires outside `serve`",
    ),
    "test_verdicts.py::test_a_late_follow_up_on_a_question_answered_only_when_chased_is_scored_and_failed": KnownFailure(
        observed="Rosa answers only her 2nd message; a follow-up 36 h after her answer fell due scores 0 made, "
        "0 due, 0 late and the run passes",
        promised="design.md, Pillar one: a follow-up is any agent write the person could see while the wait is "
        "open, and one more than GRACE after it fell due is late_follow_up, FAIL",
    ),
    "test_driven_parity.py::test_the_same_story_driven_through_serve_gets_the_verdict_scorecard_and_findings_run_gives": KnownFailure(
        observed="driven as serve.md's recipe says (advance, then a step around the wake), Rosa's answer lands on "
        "the advance outside every step: idle_wakes 1 and an idle_wake review, where `run` scores 0 and none",
        promised="serve.md, Scoring a run you drive yourself: a step is recorded as the run loop records a wake, "
        "and the checks and scorecard read it as one",
    ),
    "test_people.py::test_a_standing_world_lists_a_scripted_answer_still_to_land_as_owed": KnownFailure(
        observed="Rosa is asked and her scripted answer fires 5 h later on an advance, but WorldView.owed is empty "
        "until then, so a harness cannot learn where to move the clock",
        promised="serve.md, the control API: GET /v1/worlds/{id} answers what the world owes; WorldView.owed is "
        "what falls due as the clock moves",
    ),
}
"""Keyed by `<file>.py::<test function>`, the file relative to `tests/acceptance/`."""
