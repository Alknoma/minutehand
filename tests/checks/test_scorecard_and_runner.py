"""The scorecard's facts, the runner that finds every check, and stability over samples."""

from __future__ import annotations

from datetime import timedelta

import pytest

from minutehand.checks.effectiveness import measure
from minutehand.checks.runner import RunResult, discover, evaluate, evaluate_run, stability
from minutehand.domain.checks import FindingKind, PersonBurden
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import PersonAsked, Silent, TicketCreated
from tests.checks.world import ONE_WAKE, Log, at, person, reply, rules, scenario, view
from tests.test_checks_on_reference_run import TIMELINE, WORLD

OWNER, SOFIA = person("owner"), person("sofia")

CHECKS = {
    "agent_contract_changed",
    "around_proxy",
    "assessments",
    "expectations",
    "items",
    "near_miss_name",
    "unmatched_call",
}
"""Minutehand's own: the scenario's words (expectations, protected names), the run's integrity, the reader of the
team's rules, and the assessment of the agent's effects against the declared world (`items`). None of them holds an
opinion of how an agent should behave that the scenario does not declare."""


def test_the_slowest_reaction_is_a_stretch_of_time_with_no_threshold() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 16.5, text="Thanks.")
    card = measure(view(scenario(SOFIA), log, [reply(SOFIA, ask, 1.5)]), findings=[], met=0, ended_at=at(20))
    assert (card.waits_settled, card.slowest_reaction) == (1, timedelta(hours=15))


def test_an_answer_followed_by_no_write_counts_to_the_end_of_the_run() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.read(ask.entity, 3)  # the agent looked, and wrote nothing: a read tells nobody anything
    log.memory("asks/sofia", "answered", 3)  # nor does its own memory
    card = measure(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 1.5)]), findings=[], met=0, ended_at=at(6))
    assert card.slowest_reaction == timedelta(hours=4.5)


def test_a_write_elsewhere_right_after_an_answer_is_the_reaction() -> None:
    """The trial's run: Sam's answer landed, the agent at once filed the approval (another person, another place) and
    never wrote to Sam again. The scorecard said 4.8 days, to the run's end; the agent reacted at once."""
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 1.5, text="Filed the approval with the cost centre Sofia gave.")
    card = measure(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 1.5)]), findings=[], met=0, ended_at=at(120))
    assert card.slowest_reaction == timedelta(0)


def test_burden_counts_messages_per_person_and_the_ones_that_chased_an_open_ask() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 1, text="Any news?")
    log.message([SOFIA, OWNER], 2, text="Thanks, all done.")
    card = measure(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 1.5)]), findings=[], met=2, ended_at=at(6))
    assert card.burden == [
        PersonBurden(person="owner", messages=1, follow_ups=0),
        PersonBurden(person="sofia", messages=3, follow_ups=1),
    ]
    assert (card.messages_to_people, card.messages_per_outcome, card.follow_ups_made) == (4, 2.0, 1)


def test_a_message_chasing_two_waits_is_one_follow_up() -> None:
    sofia, dania = person("sofia", Silent()), person("dania", Silent())
    log = Log()
    log.message([sofia], 0, channel="a")
    log.message([dania], 0, channel="b")
    log.message([sofia, dania], 5, text="Any news, both of you?", channel="a")
    card = measure(view(scenario(sofia, dania), log), findings=[], met=0, ended_at=at(7))
    assert card.follow_ups_made == 1


def test_every_check_module_is_discovered_without_a_list() -> None:
    assert {c.id for c in discover()} == CHECKS


def test_the_reference_capture_fails_on_its_own_scenarios_words_and_names_what_did_not_run() -> None:
    result = evaluate(WORLD, stop=None)
    assert result.exit_code == 1
    assert sorted({f.check for f in result.findings}) == ["expectations", "near_miss_name"]
    assert result.effectiveness.expectations_met == 3 and result.effectiveness.failed_checks == 5
    assert {b.split(":")[0] for b in result.blocked} == {"unmatched_call"}
    assert result.assessed_by == ["items", "expectations", "near_miss_name"]


def test_a_clean_run_exits_zero_and_the_ledger_is_built_from_the_world() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 2, text="Thanks, filing it.")
    log.ticket("File the contract", SOFIA, 2.1)
    result = evaluate_run(
        scenario(SOFIA, expect=[TicketCreated(assignee="sofia")]),
        log.events,
        [ONE_WAKE],
        [reply(SOFIA, ask, 1.5)],
        unmatched_calls=[],
        stop=StopReason.AGENT_DONE,
    )
    [met] = result.findings
    assert met.kind is FindingKind.INFORMATIONAL and met.message.startswith(
        "ticket created for sofia: met by the ticket"
    )
    assert result.exit_code == 0
    assert result.effectiveness.waits_opened == 2 and result.effectiveness.expectations_met == 1


def test_evaluate_run_reads_the_scenarios_own_rules_without_those_it_switches_off() -> None:
    written = rules(
        """
        - id: no_follow_ups
          each: ask
          count: {follow_ups: {}}
          at_most: 0
        - id: no_messages
          count: {messages: {}}
          at_most: 0
        """
    )
    dania = person("dania", Silent())
    log = Log()
    log.message([dania], 0)
    log.message([dania], 1, text="Any news?")
    world = scenario(dania).model_copy(update={"assess": written, "assess_off": ["no_messages"]})
    result = evaluate_run(world, log.events, [ONE_WAKE], [], unmatched_calls=[], stop=StopReason.AGENT_DONE)
    assert [f.check for f in result.findings] == ["no_follow_ups"] and result.assessed_by == ["items", "no_follow_ups"]


def _result(failing: bool) -> RunResult:
    return (
        evaluate(WORLD, stop=None)
        if failing
        else evaluate_run(
            scenario(OWNER, expect=[PersonAsked(person="owner", at_least=0)]),
            [],
            [ONE_WAKE],
            [],
            unmatched_calls=[],
            stop=None,
        )
    )


def test_stability_counts_the_samples_that_passed() -> None:
    assert stability([_result(True), _result(False), _result(False)]).passed == 2


def test_stability_of_no_samples_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one sample"):
        stability([])


def test_the_scenario_deadline_is_not_counted_as_a_wait() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 2, text="Thanks.")
    world = view(scenario(SOFIA, deadline_after=timedelta(days=14)), log, [reply(SOFIA, ask, 1.5)])
    assert len(world.obligations) == 2  # the ask and the deadline, which the ledger keeps for the rules' `deadline`
    card = evaluate(world, stop=None).effectiveness
    assert (card.waits_opened, card.waits_open_at_end) == (1, 0)


def test_the_reference_timeline_has_no_reaction_to_time() -> None:
    card = evaluate(TIMELINE, stop=None).effectiveness
    assert (card.follow_ups_made, card.waits_settled, card.idle_wakes) == (3, 0, 3)
