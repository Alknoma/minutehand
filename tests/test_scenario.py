import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.experiment import Fork
from minutehand.domain.scenario import Scenario, Seed, WrittenScenario

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


def test_a_scenario_file_without_its_goal_and_expectations_loads_as_a_seed_owned_by_its_first_person() -> None:
    seed = Seed.model_validate(
        {
            "people": [
                {"key": "sofia", "name": "Sofia", "email": "sofia@example.com"},
                {"key": "dania", "name": "Dania", "email": "dania@example.com"},
            ],
            "tickets": [{"provider": "asana", "project": "Launch", "title": "Pricing", "assignee": "dania"}],
        }
    )
    played = seed.starting(datetime(2026, 9, 1, tzinfo=UTC))
    assert (played.owner, played.goal, played.name) == ("sofia", "", "world")
    assert played.starts_at == datetime(2026, 9, 1, tzinfo=UTC) and played.tickets[0].assignee == "dania"


def test_a_whole_scenario_file_loads_as_a_seed_with_its_expectations() -> None:
    seed = Seed.model_validate({**BASE, "expect": [{"kind": "person_asked", "person": "owner"}]})
    assert seed.owner == "owner" and seed.goal == "Three signed agreements." and len(seed.expect) == 1


def test_a_seed_with_nobody_in_it_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        Seed.model_validate({"people": []})


def test_a_provider_seed_written_as_structure_is_kept_as_its_json_text() -> None:
    scenario = Scenario.model_validate(
        {**BASE, "provider_seeds": [{"provider": "asana", "body": {"tags": ["urgent"]}}]}
    )
    seed = scenario.provider_seed("asana")
    assert seed is not None and json.loads(seed.body) == {"tags": ["urgent"]}
    assert scenario.provider_seed("youtrack") is None


def test_two_seeds_for_one_provider_are_rejected() -> None:
    twice = [{"provider": "asana", "body": {}}, {"provider": "asana", "body": {}}]
    with pytest.raises(ValidationError, match="more than one provider seed for asana"):
        Scenario.model_validate({**BASE, "provider_seeds": twice})


def test_a_happening_on_a_ticket_nobody_seeded_is_rejected() -> None:
    happening = {"kind": "ticket", "person": "owner", "ticket": "Ghost", "after": "P1D", "action": {"kind": "deletes"}}
    with pytest.raises(ValidationError, match="'Ghost', and 0 seeded tickets have that title"):
        Scenario.model_validate({**BASE, "happenings": [happening]})


def test_a_happening_that_reassigns_to_nobody_real_is_rejected() -> None:
    ticket = {"provider": "asana", "project": "P", "title": "Book the hall"}
    happening = {
        "kind": "ticket",
        "person": "owner",
        "ticket": "Book the hall",
        "after": "P1D",
        "action": {"kind": "reassigns", "to": "mira"},
    }
    with pytest.raises(ValidationError, match="no such person: mira"):
        Scenario.model_validate({**BASE, "tickets": [ticket], "happenings": [happening]})


LEGAL = {"key": "legal", "provider": "youtrack", "project": "Launch", "title": "Legal review"}


def test_two_tickets_with_one_key_are_rejected() -> None:
    with pytest.raises(ValidationError, match="two seeded tickets share a key"):
        Scenario.model_validate({**BASE, "tickets": [LEGAL, LEGAL]})


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
        "kind": "ticket",
        "ticket": "Legal review",
        "person": "owner",
        "after": "P1D",
        "action": {"kind": "comments", "text": "Lakeside hall"},
    }
    with pytest.raises(ValidationError, match="owner's comment on 'Legal review'"):
        Scenario.model_validate(
            {**BASE, "people": people, "tickets": [LEGAL], "happenings": [happening], "expect": [relayed]}
        )


def test_a_scripted_reply_that_neither_writes_nor_presses_is_rejected() -> None:
    from minutehand.domain.scenario import ScriptedReply

    with pytest.raises(ValidationError, match="writes text or presses"):
        ScriptedReply(to_ask=1)
    with pytest.raises(ValidationError, match="not both"):
        ScriptedReply.model_validate({"to_ask": 1, "text": "ok", "press": {"label": "Accept"}})


def test_a_reason_typed_into_a_form_is_what_the_person_says_for_a_relayed_tell() -> None:
    nadia = {
        "key": "nadia",
        "name": "Nadia",
        "email": "nadia@example.com",
        "reply": {
            "kind": "scripted",
            "replies": [{"to_ask": 1, "press": {"label": "Reject", "form": [{"value": "budget is frozen"}]}}],
        },
    }
    scenario = Scenario.model_validate(
        {
            **BASE,
            "people": [*BASE["people"], nadia],
            "expect": [{"kind": "relayed", "said_by": "nadia", "to": "owner", "tell": "budget is frozen"}],
        }
    )
    assert scenario.expect[0].kind == "relayed"


def test_a_picker_that_picks_nobody_real_is_rejected() -> None:
    picker = {
        "key": "nadia",
        "name": "Nadia",
        "email": "nadia@example.com",
        "reply": {"kind": "scripted", "replies": [{"to_ask": 1, "press": {"label": "Assign to", "picks": "ghost"}}]},
    }
    with pytest.raises(ValidationError, match="no such person: ghost"):
        Scenario.model_validate({**BASE, "people": [*BASE["people"], picker]})


def test_a_thread_reply_older_than_its_post_is_rejected() -> None:
    from minutehand.domain.scenario import SeededPost

    with pytest.raises(ValidationError, match="older than the post"):
        SeededPost(
            by="owner",
            text="root",
            ago=timedelta(hours=1),
            replies=[SeededPost(by="owner", text="r", ago=timedelta(hours=2))],
        )


DOC = {"provider": "google_workspace", "title": "Plan"}


def test_a_document_change_or_share_naming_nobody_real_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no such person: rosa"):
        Scenario.model_validate({**BASE, "documents": [{**DOC, "shared_with": [{"person": "rosa"}]}]})
    with pytest.raises(ValidationError, match="no such person: rosa"):
        Scenario.model_validate(
            {
                **BASE,
                "documents": [DOC],
                "happenings": [
                    {
                        "kind": "document",
                        "document": "Plan",
                        "person": "rosa",
                        "after": "PT1H",
                        "action": {"kind": "trashed"},
                    }
                ],
            }
        )


def test_a_change_to_a_document_that_is_not_seeded_is_rejected() -> None:
    with pytest.raises(ValidationError, match="'Budget', and 0 seeded documents have that title"):
        Scenario.model_validate(
            {
                **BASE,
                "documents": [DOC],
                "happenings": [
                    {
                        "kind": "document",
                        "document": "Budget",
                        "person": "owner",
                        "after": "PT1H",
                        "action": {"kind": "renamed", "to": "Old"},
                    }
                ],
            }
        )


def test_a_document_in_a_space_that_is_not_seeded_is_rejected() -> None:
    with pytest.raises(ValidationError, match="space 'Team', which is not seeded"):
        Scenario.model_validate({**BASE, "documents": [{**DOC, "space": "Team"}]})


def test_rows_on_anything_but_a_spreadsheet_are_rejected() -> None:
    with pytest.raises(ValidationError, match="has rows but is a document"):
        Scenario.model_validate({**BASE, "documents": [{**DOC, "rows": [["a"]]}]})


def test_a_tell_a_person_edits_into_a_document_is_refused() -> None:
    with pytest.raises(ValidationError, match="appears in the change to 'Plan'"):
        Scenario.model_validate(
            {
                **BASE,
                "people": [
                    *BASE["people"],
                    {
                        "key": "rosa",
                        "name": "Rosa",
                        "email": "rosa@example.com",
                        "reply": {"kind": "scripted", "replies": [{"to_ask": 1, "text": "Lakeside hall."}]},
                    },
                ],
                "documents": [DOC],
                "happenings": [
                    {
                        "kind": "document",
                        "document": "Plan",
                        "person": "owner",
                        "after": "PT1H",
                        "action": {"kind": "edited", "append": "Venue: lakeside hall"},
                    }
                ],
                "expect": [{"kind": "relayed", "said_by": "rosa", "to": "owner", "tell": "lakeside hall"}],
            }
        )


@pytest.mark.parametrize("old", ["state", "databases"])
def test_an_agent_file_with_state_hooks_or_fronted_databases_is_refused_naming_the_store(old: str) -> None:
    with pytest.raises(ValidationError, match=r"minutehand_agent\.store"):
        AgentUnderTest.model_validate({"name": "a", "wakes": [{"kind": "command", "argv": ["x"]}], old: {}})


def test_a_scenario_seeds_the_agents_memory_with_json_values_and_refuses_a_key_twice() -> None:
    seeded = Scenario.model_validate(
        {**BASE, "memory": [{"key": "asks/sam", "value": {"status": "asked"}}, {"key": "n", "value": 3}]}
    )
    assert [(m.collection, m.key, m.value) for m in seeded.memory] == [
        ("default", "asks/sam", {"status": "asked"}),
        ("default", "n", 3),
    ]
    with pytest.raises(ValidationError, match="seeds a key twice: default/n"):
        Scenario.model_validate({**BASE, "memory": [{"key": "n", "value": 1}, {"key": "n", "value": 2}]})


def test_an_own_database_is_handed_out_in_one_variable_once() -> None:
    with pytest.raises(ValidationError, match="two own databases"):
        AgentUnderTest.model_validate(
            {
                "name": "a",
                "wakes": [{"kind": "command", "argv": ["x"]}],
                "own_databases": [{"env": "DB"}, {"env": "DB"}],
            }
        )


def test_a_moment_counted_from_the_start_is_written_into_the_text_when_the_run_starts() -> None:
    from minutehand.domain.scenario import WrittenScenario

    written = WrittenScenario.model_validate(
        {
            "name": "dated",
            "goal": "Signed by {{start+P2D}} ({{start+P2D:iso}}); review {{start+P1DT5H:time}}; filed {{start-P1D}}.",
            "owner": "owen",
            "people": [{"key": "owen", "name": "Owen", "email": "owen@example.com"}],
            "directions": [{"text": "Now it is due {{start+P3D:iso}}.", "after": "PT1H"}],
        }
    )
    played = written.starting(datetime(2026, 9, 1, 9, tzinfo=UTC))
    assert played.goal == (
        "Signed by Thursday 3 September 2026 (2026-09-03); review Wednesday 2 September 2026, 14:00 UTC; "
        "filed Monday 31 August 2026."
    )
    assert played.directions[0].text == "Now it is due 2026-09-04."
    assert played.starting(datetime(2030, 1, 1, tzinfo=UTC)).goal == played.goal


def test_a_moment_that_names_no_duration_is_rejected_at_load() -> None:
    from minutehand.domain.scenario import WrittenScenario

    with pytest.raises(ValidationError):
        WrittenScenario.model_validate(
            {
                "name": "x",
                "goal": "By {{start+Pnever}}",
                "owner": "o",
                "people": [{"key": "o", "name": "O", "email": "o@x"}],
            }
        )


def _with_dispatch(*rules: dict[str, object]) -> dict[str, object]:
    return {
        "name": "dispatched",
        "goal": "g",
        "owner": "owen",
        "starts_at": "2026-08-24T09:00:00Z",
        "people": [{"key": "owen", "name": "Owen", "email": "owen@example.com", "reply": {"kind": "silent"}}],
        "dispatch": list(rules),
    }


def test_dispatch_rules_load_with_their_durations() -> None:
    scn = Scenario.model_validate(
        _with_dispatch(
            {"wakes": "reported", "fault": "late", "by": "PT2H"},
            {"wakes": "polled", "nth": 3, "fault": "dropped"},
            {"wakes": "booked", "nth": 1, "fault": "late", "by": "PT10M"},
        )
    )
    assert [r.by for r in scn.dispatch] == [timedelta(hours=2), None, timedelta(minutes=10)]


@pytest.mark.parametrize(
    ("rule", "says"),
    [
        ({"wakes": "reported", "fault": "late"}, "a late wake needs `by`"),
        ({"wakes": "reported", "fault": "twice", "by": "PT0S"}, "a twice wake needs `by`"),
        ({"wakes": "polled", "fault": "dropped", "by": "PT1H"}, "a dropped wake is never delivered"),
    ],
)
def test_a_dispatch_rule_that_cannot_be_delivered_is_refused(rule: dict[str, object], says: str) -> None:
    with pytest.raises(ValidationError, match=says):
        Scenario.model_validate(_with_dispatch(rule))


def test_two_dispatch_rules_for_the_same_wake_are_refused() -> None:
    rule = {"wakes": "reported", "nth": 2, "fault": "dropped"}
    with pytest.raises(ValidationError, match="two dispatch rules name the same wake"):
        Scenario.model_validate(_with_dispatch(rule, rule))


def test_a_seed_with_dispatch_rules_is_refused() -> None:
    written = _with_dispatch({"wakes": "reported", "fault": "dropped"})
    with pytest.raises(ValidationError, match="dispatch rules need the run loop's clock"):
        Seed.model_validate(written)
