"""The verdict: whether the checks held, kept apart from whether the agent finished."""

from __future__ import annotations

from datetime import timedelta

import pytest

from minutehand.checks.runner import evaluate, exit_code
from minutehand.domain.agent import Commitment, CommitmentStatus, WaitingOn
from minutehand.domain.checks import FindingKind
from minutehand.domain.people import PersonReply
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.scenario import Expectation, PersonAsked, Silent
from tests.checks.world import Log, at, person, scenario, view

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


def test_done_with_a_question_it_never_followed_up_is_not_finished() -> None:
    """The agent asked Sofia, never came back to it, and reported done: it stopped waiting, it did not finish.
    Before, DONE passed whatever was left open."""
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]), _asked_and_waiting())

    result = evaluate(world, stop=StopReason.AGENT_DONE)

    assert result.verdict.kind is VerdictKind.UNFINISHED and result.exit_code == 3
    assert result.verdict.words == (
        "Not finished: no check failed, but the agent reported it was done with 1 ask it made still unanswered "
        "and never followed up (sofia)."
    )


def test_done_after_following_the_question_up_passes() -> None:
    log = _asked_and_waiting()
    log.message([SOFIA], 30, text="Following up on the contract.")
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="sofia")]), log)

    result = evaluate(world, stop=StopReason.AGENT_DONE, ended=at(31))

    assert result.verdict.kind is VerdictKind.PASSED and result.exit_code == 0
    assert result.verdict.words == "Passed: no check failed, and the agent reported it was done."


def test_a_silent_owner_told_the_result_once_every_expectation_is_met_leaves_nothing_open() -> None:
    """The agent asks Sofia, she answers, every expectation is met, and it tells its owner, who never answers, and
    stops without DONE. Before, that one message kept the run 'Not finished'."""
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

    assert told.verdict.kind is VerdictKind.PASSED and told.exit_code == 0, told.verdict.words
    assert "1 message telling the owner, who never answers, is not counted as open" in told.verdict.words
    assert evaluate(unmet, stop=StopReason.NOTHING_PENDING, ended=at(6)).verdict.kind is not VerdictKind.PASSED


def test_a_limit_stop_with_nothing_open_passes_and_says_how_it_stopped() -> None:
    log = Log()
    log.message([OWNER], 1, text="Nothing to do here.")
    world = view(scenario(OWNER, SOFIA), log)

    result = evaluate(world, stop=StopReason.WAKE_LIMIT)

    assert result.verdict.kind is VerdictKind.PASSED and result.exit_code == 0
    assert result.verdict.words == (
        "Passed: no check failed, and the run stopped at the scenario's wake limit, with nothing left open."
    )


def test_an_open_commitment_alone_keeps_a_limit_stop_unfinished() -> None:
    world = view(scenario(OWNER, SOFIA), Log()).model_copy(
        update={"commitments": [_commitment(CommitmentStatus.OPEN), _commitment(CommitmentStatus.MET)]}
    )

    result = evaluate(world, stop=StopReason.DEADLINE_PASSED)

    assert result.verdict.kind is VerdictKind.UNFINISHED and result.verdict.open_commitments == 1
    assert "at the scenario's deadline, with 1 commitment still open" in result.verdict.words


def test_a_failed_check_is_a_failure_whatever_was_open() -> None:
    world = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="owner")]), _asked_and_waiting())

    result = evaluate(world, stop=StopReason.WAKE_LIMIT)

    assert result.verdict.kind is VerdictKind.FAILED and result.exit_code == 1
    assert result.verdict.words == "Failed: 1 check failed; the run stopped at the scenario's wake limit."


def test_samples_exit_1_on_any_failure_else_3_on_any_unfinished() -> None:
    waiting = view(scenario(OWNER, SOFIA), _asked_and_waiting())
    failing = view(scenario(OWNER, SOFIA, expect=[PersonAsked(person="owner")]), _asked_and_waiting())
    done = evaluate(view(scenario(OWNER, SOFIA), Log()), stop=StopReason.AGENT_DONE)
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
    world = view(scenario(OWNER, SOFIA, deadline_after=timedelta(days=3)), log)

    result = evaluate(world, stop=StopReason.DEADLINE_PASSED, ended=at(72))

    assert result.verdict.kind is VerdictKind.UNFINISHED and result.exit_code == 3
