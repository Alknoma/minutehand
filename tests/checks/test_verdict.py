"""The verdict: whether the checks held, kept apart from whether the agent finished."""

from __future__ import annotations

from datetime import timedelta

import pytest

from minutehand.checks.runner import evaluate, exit_code
from minutehand.domain.agent import Commitment, CommitmentStatus, WaitingOn
from minutehand.domain.assessments import IntegrityCheck, StoppedBy
from minutehand.domain.checks import AroundProxy, FindingKind
from minutehand.domain.people import PersonReply
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.scenario import Expectation, PersonAsked, Silent
from minutehand.domain.world import Exchange
from tests.checks.world import Log, at, person, rules, scenario, view

OWNER = person("owner")
SOFIA = person("sofia", Silent())
SOFIA_ANSWERS = person("sofia")


def _asked_and_waiting() -> Log:
    """The agent asked Sofia, who never answers, an hour in; the wait is not due when the run stops."""
    log = Log()
    log.message([SOFIA], 1)
    return log


def _commitment(status: CommitmentStatus) -> Commitment:
    return Commitment(
        key="pricing", description="Pricing confirmed", waiting_on=WaitingOn.PERSON, opened_at=at(1), status=status
    )


@pytest.mark.parametrize(
    "stop", [StopReason.WAKE_LIMIT, StopReason.DEADLINE_PASSED, StopReason.NOTHING_PENDING, StopReason.CLOSED]
)
def test_a_stop_without_done_while_a_wait_is_open_is_not_finished_and_exits_3(stop: StopReason) -> None:
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]), _asked_and_waiting())

    result = evaluate(world, stop=stop)

    assert [f for f in result.findings if f.kind is FindingKind.FAIL] == []
    assert result.verdict.kind is VerdictKind.UNFINISHED and result.exit_code == 3
    assert result.verdict.words.startswith("Not finished: no check failed, but the agent never reported it was done")
    assert "with 1 wait still open" in result.verdict.words
    assert result.verdict.stop is stop and result.verdict.open_waits == 1


DONE_WHILE_WAITING = rules(
    """
    - id: done_with_nothing_open
      when: {stopped: [agent_done]}
      count: {asks: {open_at: end}}
      at_most: 0
      message: "the agent reported done with {rule.count} ask(s) still unanswered"
    """
)


def test_done_is_taken_at_its_word_unless_the_team_rules_otherwise() -> None:
    """The agent asked Sofia, never came back to it, and reported done. Whether that is finished is the team's to
    say: by itself the run passes; under a rule that done means nothing left open, it fails, naming the ask."""
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]), _asked_and_waiting())

    assert evaluate(world, stop=StopReason.AGENT_DONE).verdict.kind is VerdictKind.PASSED
    ruled = evaluate(
        world.model_copy(update={"rules": DONE_WHILE_WAITING, "stopped": StoppedBy.AGENT_DONE}),
        stop=StopReason.AGENT_DONE,
    )
    assert ruled.verdict.kind is VerdictKind.FAILED and ruled.exit_code == 1
    [finding] = [f for f in ruled.findings if f.check == "done_with_nothing_open"]
    assert (finding.check, finding.message, finding.evidence) == (
        "done_with_nothing_open",
        "the agent reported done with 1 ask(s) still unanswered",
        [1],
    )
    stopped_otherwise = world.model_copy(update={"rules": DONE_WHILE_WAITING, "stopped": StoppedBy.WAKE_LIMIT})
    assert evaluate(stopped_otherwise, stop=StopReason.WAKE_LIMIT).verdict.kind is VerdictKind.UNFINISHED


def test_done_after_following_the_question_up_passes() -> None:
    log = _asked_and_waiting()
    log.message([SOFIA], 30, text="Following up on the contract.")
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]), log)

    result = evaluate(world, stop=StopReason.AGENT_DONE, ended=at(31))

    assert result.verdict.kind is VerdictKind.PASSED and result.exit_code == 0
    assert result.verdict.words == "Passed: no check failed, and the agent reported it was done."


def test_a_message_to_a_silent_owner_is_an_open_wait_whatever_it_said() -> None:
    """The agent asks Sofia, she answers, every expectation is met, and it tells its owner, who never answers, and
    stops without DONE. Nothing reads the message as 'the result being reported': the wait is open, and the run is
    not finished unless the agent says it is done."""
    owner = person("owner", Silent())
    log = Log()
    asked = log.message([SOFIA_ANSWERS], 1)
    log.message([SOFIA_ANSWERS], 3.5, text="Thank you!")
    log.message([owner], 4, text="Sofia confirmed the contract.")
    answered = [PersonReply(person="sofia", in_reply_to=asked.entity, text="Signed.", at=at(3))]
    expected: list[Expectation] = [PersonAsked(person="owner", mentions=["confirmed"])]
    world = view(scenario(owner, SOFIA_ANSWERS, expect=expected), log, answered)
    unmet = view(scenario(owner, SOFIA_ANSWERS, expect=[PersonAsked(person="owner", mentions=["x"])]), log, answered)

    told = evaluate(world, stop=StopReason.NOTHING_PENDING, ended=at(6))

    assert told.verdict.kind is VerdictKind.UNFINISHED and told.verdict.open_waits == 1, told.verdict.words
    assert evaluate(world, stop=StopReason.AGENT_DONE, ended=at(6)).verdict.kind is VerdictKind.PASSED
    assert evaluate(unmet, stop=StopReason.NOTHING_PENDING, ended=at(6)).verdict.kind is not VerdictKind.PASSED


def test_a_limit_stop_with_nothing_open_passes_and_says_how_it_stopped() -> None:
    log = Log()
    log.message([OWNER], 1, text="Nothing to do here.")
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="owner")]), log)

    result = evaluate(world, stop=StopReason.WAKE_LIMIT)

    assert result.verdict.kind is VerdictKind.PASSED and result.exit_code == 0
    assert result.verdict.words == (
        "Passed: no check failed, and the run stopped at its wake limit, with nothing left open."
    )


def test_an_open_commitment_alone_keeps_a_limit_stop_unfinished() -> None:
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="owner", at_least=0)]), Log()).model_copy(
        update={"commitments": [_commitment(CommitmentStatus.OPEN), _commitment(CommitmentStatus.MET)]}
    )

    result = evaluate(world, stop=StopReason.DEADLINE_PASSED)

    assert result.verdict.kind is VerdictKind.UNFINISHED and result.verdict.open_commitments == 1
    assert "at the scenario's deadline, with 1 commitment still open" in result.verdict.words


def test_a_failed_check_is_a_failure_whatever_was_open() -> None:
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="owner")]), _asked_and_waiting())

    result = evaluate(world, stop=StopReason.WAKE_LIMIT)

    assert result.verdict.kind is VerdictKind.FAILED and result.exit_code == 1
    assert result.verdict.words == "Failed: 1 check failed; the run stopped at its wake limit."


def test_samples_exit_1_on_any_failure_else_3_on_any_unfinished() -> None:
    asked: list[Expectation] = [PersonAsked(person="sofia")]
    waiting = view(scenario(OWNER, SOFIA, expect=asked), _asked_and_waiting())
    failing = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="owner")]), _asked_and_waiting())
    done = evaluate(waiting, stop=StopReason.AGENT_DONE)
    cut_off = evaluate(waiting, stop=StopReason.WAKE_LIMIT)
    failed = evaluate(failing, stop=StopReason.AGENT_DONE)

    assert exit_code([done, done]) == 0
    assert exit_code([done, cut_off]) == 3
    assert exit_code([cut_off, failed, done]) == 1


def test_the_deadline_after_a_silent_person_is_not_finished_rather_than_failed() -> None:
    """A scenario whose point is that nobody answers ends at its deadline: the run is not a failure, and not an
    unqualified pass either, since the agent never said it was done."""
    log = _asked_and_waiting()
    log.message([SOFIA], 40, text="Following up on the contract.")
    world = view(scenario(OWNER, SOFIA, deadline_after=timedelta(days=3), expect=[PersonAsked(person="sofia")]), log)

    result = evaluate(world, stop=StopReason.DEADLINE_PASSED, ended=at(72))

    assert result.verdict.kind is VerdictKind.UNFINISHED and result.exit_code == 3


INSTANT = rules(
    """
    - id: waits_before_following_up
      each: ask
      count: {follow_ups: {}, until: ask+PT1H}
      at_most: 0
      severity: review
    """
)


def test_a_follow_up_in_the_hour_of_the_ask_is_reviewed_when_the_team_rules_so() -> None:
    """A "follow-up" seconds after the ask chased nothing; a team that minds says so, and it is a review."""
    log = _asked_and_waiting()
    log.message([SOFIA], 1.01, text="Just following up on the contract.")
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]), log, assess=INSTANT)

    result = evaluate(world, stop=StopReason.AGENT_DONE)

    [finding] = [f for f in result.findings if f.check == "waits_before_following_up"]
    assert finding.kind is FindingKind.REVIEW and finding.evidence == [1, 2]
    assert result.verdict.kind is VerdictKind.PASSED


def test_a_follow_up_hours_after_the_ask_is_not_reviewed() -> None:
    log = _asked_and_waiting()
    log.message([SOFIA], 3, text="Just following up on the contract.", wake=2)
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]), log, assess=INSTANT)

    assert [f for f in evaluate(world, stop=StopReason.AGENT_DONE).findings if f.check != "expectations"] == []


def test_a_run_whose_files_declare_no_assessment_is_still_assessed_against_the_declared_world() -> None:
    """Write the scenario, get the assessment: the agent's effects are measured against what the scenario declares
    (`items`) whether or not anyone wrote a rule, so a run is never left unjudged for want of one."""
    world = view(scenario(OWNER, SOFIA), _asked_and_waiting())

    result = evaluate(world, stop=StopReason.AGENT_DONE)

    assert result.assessed_by == ["items"] and result.findings == []
    assert result.verdict.kind is VerdictKind.PASSED and result.exit_code == 0
    assert result.effectiveness.waits_opened == 1


def test_what_judged_a_run_is_recorded() -> None:
    world = view(
        scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]).model_copy(update={"protected_names": ["Ayven"]}),
        _asked_and_waiting(),
        assess=INSTANT,
    )
    assert evaluate(world, stop=None).assessed_by == [
        "items",
        "waits_before_following_up",
        "expectations",
        "near_miss_name",
    ]


_AROUND = AroundProxy(
    host="slack.com", provider="slack", by_agent=1, through_proxy=0, around=1, example="POST https://slack.com/api"
)
_UNMATCHED = Exchange(method="POST", host="api.mail.example", path="/v3/send", status=501)


@pytest.mark.parametrize("check", [IntegrityCheck.AROUND_PROXY, IntegrityCheck.UNMATCHED_CALL])
def test_an_integrity_fact_changes_the_verdict_only_when_the_users_files_name_it(check: IntegrityCheck) -> None:
    """Minutehand states whether a run can be trusted; it fails the run on it only when the user says so."""
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]), _asked_and_waiting()).model_copy(
        update={"around_proxy": [_AROUND], "unmatched_calls": [_UNMATCHED]}
    )
    stated = evaluate(world, stop=StopReason.AGENT_DONE)
    assert stated.verdict.kind is VerdictKind.PASSED, stated.verdict.words
    assert {f.check for f in stated.findings if f.kind is FindingKind.REVIEW} >= {check.value}

    named = evaluate(world.model_copy(update={"fail_on_integrity": [check]}), stop=StopReason.AGENT_DONE)
    assert named.verdict.kind is VerdictKind.FAILED and named.verdict.failed_checks == 1
    assert [f.check for f in named.findings if f.kind is FindingKind.FAIL] == [check.value]
    assert check.value in named.assessed_by


def test_a_run_judged_by_an_integrity_fact_alone_is_assessed() -> None:
    world = view(scenario(OWNER, SOFIA), Log()).model_copy(
        update={"around_proxy": [], "fail_on_integrity": [IntegrityCheck.AROUND_PROXY]}
    )
    result = evaluate(world, stop=StopReason.AGENT_DONE)
    assert result.assessed_by == ["items", "around_proxy"] and result.verdict.kind is VerdictKind.PASSED


def test_a_world_with_a_wait_left_open_at_the_end_of_its_window_passes_and_counts_the_wait() -> None:
    world = scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]).model_copy(
        update={"goal": None, "owner": None, "deadline_after": None, "runs_for": timedelta(days=30)}
    )

    result = evaluate(view(world, _asked_and_waiting()), stop=StopReason.WINDOW_ENDED)

    assert result.verdict.kind is VerdictKind.PASSED and result.exit_code == 0, "the agent's work never finishes"
    assert (
        result.verdict.words
        == "Passed: no check failed in the window; the run watched the agent to the end of its window."
    )
    assert result.verdict.open_waits == 1, "what it left open is still counted"


def test_a_world_whose_agent_failed_fails_though_no_check_did() -> None:
    world = scenario(OWNER, SOFIA).model_copy(
        update={"goal": None, "owner": None, "deadline_after": None, "runs_for": timedelta(days=30)}
    )

    result = evaluate(view(world, _asked_and_waiting()), stop=StopReason.AGENT_FAILED)

    assert result.verdict.kind is VerdictKind.FAILED and result.exit_code == 1
    assert result.verdict.words.startswith("Failed: the run stopped because the agent could not be reached")
