"""The agent's own calls as a fact a rule counts (`count: {calls: ...}`): what it read, polled and tried, refused
calls among them, on a trial run of a purchasing agent a real model drove (`tests/data/trial/`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from minutehand.checks.assessments import Assessments
from minutehand.domain.assessments import Calls
from minutehand.domain.checks import RunView
from tests.checks.world import rules

RUN = RunView.model_validate_json(
    (Path(__file__).parents[1] / "data" / "trial" / "seed3_invented_quote.json").read_text(encoding="utf-8")
)


def _read(written: str) -> list[str]:
    return [f.message for f in Assessments().run(RUN.model_copy(update={"rules": rules(written)})).findings]


def test_polling_a_request_is_counted_by_host_method_and_route() -> None:
    assert _read(
        """
        - id: polls_the_request
          count: {calls: {host: [api.approvals.example], method: [GET], route: ["/v1/requests/{id}"]}}
          at_most: 3
          message: "polled the request {rule.count} times"
        """
    ) == ["polled the request 14 times"]


def test_a_refused_resubmit_is_counted_with_its_status() -> None:
    written = """
        - id: no_refused_moves
          count: {calls: {route: ["/v1/requests/{id}/resubmit"], refused: true, status: ["4xx"]}}
          at_most: 0
          message: "{rule.count} refused resubmit"
        """
    assert _read(written) == ["1 refused resubmit"]
    [finding] = Assessments().run(RUN.model_copy(update={"rules": rules(written)})).findings
    assert finding.calls == [76] and finding.evidence == [288]
    # Mutation: the 200 resubmit is not refused, and a status of 5xx matches neither.
    assert _read(written.replace('"4xx"', '"5xx"')) == []


def test_reads_that_learned_nothing_new_are_counted_apart() -> None:
    assert _read(
        """
        - id: stale_polls
          count: {calls: {method: [GET], route: ["/v1/requests/{id}"], answer_changed: false}}
          at_most: 0
          message: "{rule.count} polls answered as the one before"
        """
    ) == ["11 polls answered as the one before"]


def test_a_run_that_recorded_no_calls_leaves_a_calls_rule_unread() -> None:
    blind = RUN.model_copy(update={"calls": None, "rules": rules("- {id: any_call, count: {calls: {}}, at_most: 0}")})
    report = Assessments().run(blind)
    assert report.findings == [] and report.rules_read[0].unread == 1


def test_a_status_that_is_no_status_is_refused() -> None:
    with pytest.raises(ValueError, match="is a status"):
        Calls(status=["4x"])
