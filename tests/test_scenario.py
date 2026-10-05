from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.experiment import Fork
from minutehand.domain.scenario import Scenario, WrittenScenario

BASE = {
    "name": "partner_pipeline",
    "goal": "Three signed agreements.",
    "owner": "owner",
    "starts_at": "2026-08-24T10:50:03Z",
    "people": [{"key": "owner", "name": "Owner", "email": "owner@example.com"}],
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
    agent = AgentUnderTest.model_validate(
        {"name": "a", "wakes": [{"kind": "booked"}, {"kind": "polled", "wake_url": "http://a/tick"}]}
    )
    assert [w.kind for w in agent.wakes] == ["booked", "polled"]


def test_an_agent_given_its_goal_by_message_needs_no_wake_source_but_an_inbound_target() -> None:
    agent = AgentUnderTest.model_validate(
        {
            "name": "a",
            "goal": {"kind": "by_message", "provider": "slack"},
            "inbound": [{"provider": "slack", "url": "http://a/slack/events"}],
        }
    )
    assert agent.wakes == []
    with pytest.raises(ValidationError, match="no inbound target there"):
        AgentUnderTest.model_validate({"name": "a", "goal": {"kind": "by_message", "provider": "slack"}})


def test_a_fork_carries_typed_overrides() -> None:
    fork = Fork.model_validate(
        {
            "parent_run": "r1",
            "at_seq": 2,
            "overrides": [
                {"kind": "prompt_patch", "text": " Confirm the name."},
                {"kind": "model_swap", "to": "another-model"},
            ],
        }
    )
    assert [o.kind for o in fork.overrides] == ["prompt_patch", "model_swap"]


RUN_STARTS = datetime(2026, 10, 4, 15, 30, 12, tzinfo=UTC)


def test_a_scenario_file_without_a_start_starts_when_the_run_does() -> None:
    written = WrittenScenario.model_validate(
        {k: v for k, v in BASE.items() if k != "starts_at"} | {"deadline_after": "P2D"}
    )

    played = written.starting(RUN_STARTS)

    assert written.starts_at is None
    assert played.starts_at == RUN_STARTS and played.deadline == RUN_STARTS + timedelta(days=2)


def test_a_scenario_file_with_a_start_keeps_it_whenever_the_run_starts() -> None:
    assert WrittenScenario.model_validate(BASE).starting(RUN_STARTS).starts_at == datetime(
        2026, 8, 24, 10, 50, 3, tzinfo=UTC
    )


def test_a_played_scenario_without_its_start_is_rejected() -> None:
    with pytest.raises(ValidationError, match="starts_at"):
        Scenario.model_validate({k: v for k, v in BASE.items() if k != "starts_at"})


LEGAL = {"key": "legal", "provider": "youtrack", "project": "Launch", "title": "Legal review"}


def test_a_happening_on_a_ticket_no_seed_names_is_rejected() -> None:
    happening = {"ticket": "venue", "by": "owner", "after": "P1D", "change": {"kind": "delete"}}
    with pytest.raises(ValidationError, match="no seeded ticket has the key: venue"):
        Scenario.model_validate({**BASE, "tickets": [LEGAL], "ticket_happenings": [happening]})


def test_a_happening_by_or_to_nobody_real_is_rejected() -> None:
    happening = {"ticket": "legal", "by": "owner", "after": "P1D", "change": {"kind": "assignee", "to": "dania"}}
    with pytest.raises(ValidationError, match="no such person: dania"):
        Scenario.model_validate({**BASE, "tickets": [LEGAL], "ticket_happenings": [happening]})


def test_two_tickets_with_one_key_and_two_seeds_for_one_provider_are_rejected() -> None:
    with pytest.raises(ValidationError, match="two seeded tickets share a key"):
        Scenario.model_validate({**BASE, "tickets": [LEGAL, LEGAL]})
    seed = {"provider": "youtrack", "text": "{}"}
    with pytest.raises(ValidationError, match="a provider has two seeds"):
        Scenario.model_validate({**BASE, "provider_seeds": [seed, seed]})


def test_a_tell_a_seeded_comment_or_a_commenting_person_holds_is_rejected() -> None:
    rosa = {
        "key": "rosa",
        "name": "Rosa",
        "email": "rosa@example.com",
        "reply": {"kind": "scripted", "replies": [{"to_ask": 1, "text": "Lakeside Hall it is."}]},
    }
    people = [{"key": "owner", "name": "Owner", "email": "owner@example.com"}, rosa]
    relayed = {"kind": "relayed", "said_by": "rosa", "to": "owner", "tell": "lakeside hall"}
    commented = {**LEGAL, "comments": [{"by": "owner", "text": "Maybe Lakeside Hall?"}]}
    with pytest.raises(ValidationError, match="a comment on 'Legal review'"):
        Scenario.model_validate({**BASE, "people": people, "tickets": [commented], "expect": [relayed]})
    happening = {
        "ticket": "legal",
        "by": "owner",
        "after": "P1D",
        "change": {"kind": "comment", "text": "Lakeside hall"},
    }
    with pytest.raises(ValidationError, match="a comment owner makes on legal"):
        Scenario.model_validate(
            {**BASE, "people": people, "tickets": [LEGAL], "ticket_happenings": [happening], "expect": [relayed]}
        )
