"""The scorecard's new measures, the runner that finds every check, and stability over samples."""

from __future__ import annotations

from datetime import timedelta

import pytest

from minutehand.checks.effectiveness import measure
from minutehand.checks.runner import RunResult, discover, evaluate, evaluate_run, stability
from minutehand.domain.checks import FindingKind, PersonBurden
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.scenario import Silent, TicketCreated
from tests.checks.world import ONE_WAKE, Log, at, person, reply, scenario, view
from tests.test_checks_on_reference_run import TIMELINE, WORLD

OWNER, SOFIA = person("owner"), person("sofia")

CHECKS = {
    "acted_after_deadline",
    "acted_on_repeated_wake",
    "acted_without_approval",
    "agent_contract_changed",
    "chased_absent_person",
    "duplicate_ticket",
    "expectations",
    "idle_wake",
    "kept_chasing_after_done",
    "late_follow_up",
    "nagged",
    "near_miss_name",
    "no_follow_up",
    "planned_past_due",
    "repeated_message",
    "slow_to_react",
    "unmatched_call",
}


def test_a_slow_reaction_is_time_the_agent_lost() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 16.5, text="Thanks.")
    card = measure(view(scenario(SOFIA), log, [reply(SOFIA, ask, 1.5)]), findings=[], met=0, ended_at=at(20))
    assert (card.reactions_due, card.reactions_slow) == (1, 1)
    assert card.time_lost == card.slowest_reaction == timedelta(hours=15)


def test_a_prompt_reaction_costs_nothing() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 2, text="Thanks.")
    card = measure(view(scenario(SOFIA), log, [reply(SOFIA, ask, 1.5)]), findings=[], met=0, ended_at=at(6))
    assert (card.reactions_due, card.reactions_slow, card.time_lost) == (1, 0, timedelta(0))


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
    assert (card.messages_to_people, card.messages_per_outcome) == (4, 2.0)


def test_the_reference_scorecard_keeps_its_numbers_and_adds_no_untimeable_reaction() -> None:
    card = evaluate(TIMELINE, stop=None).effectiveness
    assert (card.follow_ups_due, card.follow_ups_made, card.follow_ups_late) == (3, 3, 1)
    assert timedelta(hours=32) < card.time_lost < timedelta(hours=33)
    assert (card.reactions_due, card.idle_wakes) == (0, 3)


def test_every_check_module_is_discovered_without_a_list() -> None:
    assert {c.id for c in discover()} == CHECKS


def test_the_reference_capture_fails_with_the_known_findings_and_names_what_did_not_run() -> None:
    result = evaluate(WORLD, stop=None)
    assert result.exit_code == 1
    assert sorted({f.check for f in result.findings}) == ["expectations", "near_miss_name", "repeated_message"]
    assert result.effectiveness.expectations_met == 3 and result.effectiveness.failed_checks == 5
    assert {b.split(":")[0] for b in result.blocked} == {
        "acted_on_repeated_wake",
        "idle_wake",
        "kept_chasing_after_done",
        "late_follow_up",
        "nagged",
        "no_follow_up",
        "planned_past_due",
        "slow_to_react",
        "unmatched_call",
    }


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


def _result(failing: bool) -> RunResult:
    return (
        evaluate(WORLD, stop=None)
        if failing
        else evaluate_run(scenario(OWNER), [], [ONE_WAKE], [], unmatched_calls=[], stop=None)
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
    assert len(world.obligations) == 2  # the ask and the deadline, which the ledger keeps for its own checks
    card = evaluate(world, stop=None).effectiveness
    assert (card.waits_opened, card.waits_open_at_end) == (1, 0)


def test_follow_ups_before_each_due_moment_are_counted_and_past_what_the_person_takes_are_nagging() -> None:
    """Sofia's longest delay is QUICK's; the agent pings her every hour, always before her answer is due, so no
    wait ever falls due and nothing is late. Before, that scored exactly as an agent that waited its turn."""
    sofia = person("sofia", Silent())
    log = Log()
    log.message([sofia], 0)
    for hour in range(1, 6):
        log.message([sofia], hour, text=f"Any news? ({hour})")
    world = view(scenario(sofia), log)

    result = evaluate(world, stop=None)

    assert result.effectiveness.follow_ups_early == 5 and result.effectiveness.follow_ups_late == 0
    nagged = [f for f in result.findings if f.check == "nagged"]
    assert len(nagged) == 1 and nagged[0].kind is FindingKind.FAIL
    assert (
        nagged[0].message == "sofia was followed up 5 times on one ask, each before their answer was due; they take 2"
    )
    patient = view(scenario(sofia.model_copy(update={"early_follow_ups": 5})), log)
    assert [f for f in evaluate(patient, stop=None).findings if f.check == "nagged"] == []


def test_a_run_whose_scorecard_counts_a_missed_follow_up_is_never_passed_without_a_finding() -> None:
    """Sofia's wait falls due two hours in; nothing follows, and her answer lands at thirty. The scorecard counts the
    late follow-up and the time lost; before, no check said so and the verdict, with the agent reporting done, was
    Passed beside it."""
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 30.5, text="Thanks.")
    result = evaluate(view(scenario(SOFIA), log, [reply(SOFIA, ask, 30)]), stop=StopReason.AGENT_DONE)

    assert result.effectiveness.follow_ups_late == 1
    assert result.verdict.kind is not VerdictKind.PASSED
    assert [f.check for f in result.findings if f.kind is FindingKind.FAIL] == ["no_follow_up"]


def test_a_reply_decided_under_a_slower_delay_is_measured_by_that_delay() -> None:
    """A fork made Sofia quick after her answer had been decided under a slower delay: the wait is measured by the
    delay it was decided under, and costs the agent nothing."""
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 30.5, text="Thanks.")
    slow = reply(SOFIA, ask, 30).model_copy(update={"patience": timedelta(hours=48)})

    result = evaluate(view(scenario(SOFIA), log, [slow]), stop=StopReason.AGENT_DONE)

    assert result.effectiveness.follow_ups_late == 0 and result.effectiveness.time_lost == timedelta(0)
    assert result.verdict.kind is VerdictKind.PASSED
