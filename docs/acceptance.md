# Acceptance

`tests/acceptance/` tests Minutehand from the outside, the way a team adopting it would: through the `minutehand`
command, the control API and its Python client, and the viewer's JSON API, against the shipped examples or a
stand-in agent of a few dozen lines (`tests/acceptance/agents/`). Nothing there imports Minutehand's internals to
drive it; models are imported only to write and read the wire. Each test's name is the promise it holds. The suite
is marked `acceptance` and runs in the default run.

A promise broken on trunk is not bent to pass. Its test is listed in `tests/acceptance/known_failures.py`, below,
and is expected to fail: it fails the run the day it passes, so whoever fixes it removes its line. Any other failure
fails the run. `test_known_failures.py` holds this page's table to that file.

## The promises

### 1. A verdict never lies (`test_verdicts.py`)

- Rosa answers, the agent relays it and says DONE: passed, exit 0, and `findings` reads the same.
- A question nobody answers and the agent never chases: failed with `no_follow_up`, exit 1.
- DONE while the question is unanswered and unchased, every expectation met: not finished, exit 3, naming Rosa.
- A fake that raises while answering: not scored, exit 4, naming the call, though an expectation also failed.
- An external emulator that dies mid-run: environment failed, exit 2, though an expectation also failed.
- A standing world nobody stepped, with nothing expected: not judged, exit 5, never "Passed".
- A run with nothing expected and nothing done is never passed.
- A late follow-up on a question the person answers only when chased is scored late and fails the run.

## Known failures

| Test | Observed | Promised |
|---|---|---|
| `test_verdicts.py::test_a_run_with_nothing_expected_and_nothing_done_is_never_passed` | no expectation, no wait, no message, one wake that changed nothing: verdict passed, exit 0 | design.md, The verdict: NOT_JUDGED when nothing was there to judge, never PASSED; a `run` always has a wake, so as written the rule never fires outside `serve` |
| `test_verdicts.py::test_a_late_follow_up_on_a_question_answered_only_when_chased_is_scored_and_failed` | Rosa answers only her 2nd message; a follow-up 36 h after her answer fell due scores 0 made, 0 due, 0 late and the run passes | design.md, Pillar one: a follow-up is any agent write the person could see while the wait is open, and one more than GRACE after it fell due is late_follow_up, FAIL |
