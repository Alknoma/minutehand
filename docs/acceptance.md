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

### 2. A run you drive yourself is scored like a run Minutehand drives (`test_driven_parity.py`)

- One story (ask, two follow-ups, the answer, a relay) through `minutehand run` and through `serve` driven as
  `docs/serve.md` says: the same verdict, scorecard and findings. Documented differences: the stop (`agent_done`
  against `closed`) and so the verdict's sentence; seq numbers, which are positions in two different logs.
- A case across three worlds (Slack, Asana, GitHub) is one run in `minutehand runs`, `findings` and the viewer.

### 3. The record is complete and unchanged (`test_record.py`)

- Every call is kept once, in order, its text, binary, invalid-charset and over-512-byte bodies byte for byte.
- No credential in a header, cookie, query, form or JSON body reaches any stored file, decompressed or not.
- A forwarded call reaches its emulator unchanged but for `x-minutehand-world`, `-wake`, `-time` and `traceparent`.
- A passed-through call reaches its upstream unchanged, and is kept.
- A model call on a tunnel is kept as a connection with its byte counts, and nothing it said.
- Only a call to an undeclared host is unclaimed; model traffic to a host no world declared is not.

### 4. The page tells the story (`test_viewer_story.py`)

- For a driven case with a question, an answer, a rewrite and a relay, the viewer's `messages` give each message's
  recipient and text in order and the rewrite's old and new text; `model-traffic` gives each model call and the
  message it wrote; `runs` names the case.
- A run nobody judged reads "Passed" nowhere in any route the viewer serves for it.

### 5. People behave as declared (`test_people.py`)

- A scripted delay lands the answer exactly that long after the ask.
- Working hours, in the person's own timezone and weekdays only, hold an answer to their next working day.
- An absence holds an answer until it is over.
- A silent person never answers, however often asked.
- Nobody answers a question they have no declared answer to.
- A button press lands after the person's delay and reaches the agent; a form after a press carries what was typed.
- A standing world lists a scripted answer still to land as owed.

## Known failures

| Test | Observed | Promised |
|---|---|---|
| `test_verdicts.py::test_a_run_with_nothing_expected_and_nothing_done_is_never_passed` | no expectation, no wait, no message, one wake that changed nothing: verdict passed, exit 0 | design.md, The verdict: NOT_JUDGED when nothing was there to judge, never PASSED; a `run` always has a wake, so as written the rule never fires outside `serve` |
| `test_verdicts.py::test_a_late_follow_up_on_a_question_answered_only_when_chased_is_scored_and_failed` | Rosa answers only her 2nd message; a follow-up 36 h after her answer fell due scores 0 made, 0 due, 0 late and the run passes | design.md, Pillar one: a follow-up is any agent write the person could see while the wait is open, and one more than GRACE after it fell due is late_follow_up, FAIL |
| `test_driven_parity.py::test_the_same_story_driven_through_serve_gets_the_verdict_scorecard_and_findings_run_gives` | driven as serve.md's recipe says (advance, then a step around the wake), Rosa's answer lands on the advance outside every step: idle_wakes 1 and an idle_wake review, where `run` scores 0 and none | serve.md, Scoring a run you drive yourself: a step is recorded as the run loop records a wake, and the checks and scorecard read it as one |
| `test_people.py::test_a_standing_world_lists_a_scripted_answer_still_to_land_as_owed` | Rosa is asked and her scripted answer fires 5 h later on an advance, but WorldView.owed is empty until then, so a harness cannot learn where to move the clock | serve.md, the control API: GET /v1/worlds/{id} answers what the world owes; WorldView.owed is what falls due as the clock moves |
