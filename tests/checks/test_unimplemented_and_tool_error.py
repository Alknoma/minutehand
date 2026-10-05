"""A call the fake does not implement is a finding naming the operation once, with its count; a call Minutehand
failed to answer makes the verdict a tool error, never the agent's failure, and the run exits 4."""

from __future__ import annotations

from minutehand.checks.runner import evaluate_run, exit_code
from minutehand.domain.checks import FindingKind
from minutehand.domain.errors import AnswerKind, Failure, FailureKind
from minutehand.domain.run import ExitCode, StopReason, VerdictKind
from minutehand.domain.world import Exchange
from tests.checks.world import person, scenario

OWNER = person("owner")


def _call(path: str, answer: AnswerKind, status: int, message: str = "") -> Exchange:
    return Exchange(
        method="GET",
        host="slack.com",
        path=path,
        status=status,
        answer=answer,
        failure=Failure(kind=FailureKind.INTERNAL, code="minutehand_internal_error", message=message, where=path)
        if answer is AnswerKind.INTERNAL_ERROR
        else None,
    )


def test_each_unimplemented_operation_is_named_once_with_its_count() -> None:
    result = evaluate_run(
        scenario(OWNER),
        [],
        [],
        [],
        unmatched_calls=[],
        failed_calls=[
            _call("/api/views.push?x=1", AnswerKind.NOT_IMPLEMENTED, 501),
            _call("/api/views.push?x=2", AnswerKind.NOT_IMPLEMENTED, 501),
            _call("/api/bookmarks.add", AnswerKind.NOT_IMPLEMENTED, 501),
        ],
        stop=StopReason.AGENT_DONE,
    )
    found = [f for f in result.findings if f.check == "unimplemented_operation"]
    assert [f.message.split(" is not implemented")[0] for f in found] == [
        "GET slack.com/api/views.push",
        "GET slack.com/api/bookmarks.add",
    ]
    assert "answered 501 2 times" in found[0].message and "answered 501 once" in found[1].message
    assert {f.kind for f in found} == {FindingKind.REVIEW}
    assert result.verdict.kind is VerdictKind.PASSED


def test_an_internal_error_makes_the_verdict_a_tool_error_naming_the_call() -> None:
    result = evaluate_run(
        scenario(OWNER),
        [],
        [],
        [],
        unmatched_calls=[],
        failed_calls=[_call("/api/chat.postMessage", AnswerKind.INTERNAL_ERROR, 500, "minutehand internal error x")],
        stop=StopReason.AGENT_DONE,
    )
    assert result.verdict.kind is VerdictKind.TOOL_ERROR
    assert "GET slack.com/api/chat.postMessage (minutehand internal error x)" in result.verdict.words
    assert "not scored against the agent" in result.verdict.words
    assert result.exit_code == ExitCode.TOOL_ERROR == 4
    failing = evaluate_run(scenario(OWNER), [], [], [], unmatched_calls=[], failed_calls=[], stop=None)
    assert exit_code([failing, result]) == ExitCode.TOOL_ERROR


def test_without_a_record_of_failed_calls_the_check_is_blocked() -> None:
    result = evaluate_run(scenario(OWNER), [], [], [], unmatched_calls=[], stop=StopReason.AGENT_DONE)
    assert any(b.startswith("unimplemented_operation:") for b in result.blocked)
