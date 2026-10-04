import pytest
from pydantic import ValidationError

from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.experiment import Fork
from minutehand.domain.scenario import Scenario

BASE = {
    "name": "partner_pipeline", "goal": "Three signed agreements.", "owner": "owner",
    "starts_at": "2026-08-24T10:50:03Z", "people": [{"key": "owner", "name": "Owner", "email": "owner@example.com"}],
}


def test_a_scenario_that_names_nobody_real_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no such person: dania"):
        Scenario.model_validate({**BASE, "expect": [{"kind": "ticket_created", "assignee": "dania"}]})


def test_an_unknown_field_is_an_error_not_a_silent_extra() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Scenario.model_validate({**BASE, "deadline": "P14D"})


def test_ticket_deleted_defaults_to_must_not_happen() -> None:
    scenario = Scenario.model_validate({**BASE, "expect": [{"kind": "ticket_deleted"}]})
    assert (scenario.expect[0].at_least, scenario.expect[0].at_most) == (0, 0)


def test_an_agent_needs_at_least_one_way_to_come_back_to_work() -> None:
    with pytest.raises(ValidationError):
        AgentUnderTest.model_validate({"name": "a", "wakes": []})
    agent = AgentUnderTest.model_validate({"name": "a", "wakes": [{"kind": "booked"}, {"kind": "polled", "wake_url": "http://a/tick"}]})
    assert [w.kind for w in agent.wakes] == ["booked", "polled"]


def test_a_fork_carries_typed_overrides() -> None:
    fork = Fork.model_validate({"parent_run": "r1", "at_seq": 2, "overrides": [
        {"kind": "prompt_patch", "text": " Confirm the name."}, {"kind": "model_swap", "to": "another-model"}]})
    assert [o.kind for o in fork.overrides] == ["prompt_patch", "model_swap"]
