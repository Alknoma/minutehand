"""Checks read straight off the log: idle wakes, duplicate tickets, late writes, unclaimed calls, repeats."""

from __future__ import annotations

from datetime import timedelta

from minutehand.checks.acted_after_deadline import ActedAfterDeadline
from minutehand.checks.duplicate_ticket import DuplicateTicket
from minutehand.checks.expectations import Expectations
from minutehand.checks.idle_wake import IdleWake
from minutehand.checks.near_miss_name import NearMissName
from minutehand.checks.repeated_message import RepeatedMessage
from minutehand.checks.unmatched_call import UnmatchedCall
from minutehand.domain.checks import FindingKind, WakeModelCalls, WakeRecord
from minutehand.domain.scenario import Expectation, PersonAsked, TicketInState
from minutehand.domain.world import Actor, Exchange, Operation, TicketState
from tests.checks.world import Log, at, person, scenario, view
from tests.test_checks_on_reference_run import TIMELINE, WORLD

OWNER, SOFIA = person("owner"), person("sofia")


def _wake(index: int, changes: int, commitments: bool = False) -> WakeRecord:
    return WakeRecord(index=index, sim_time=at(index), world_changes=changes, commitments_changed=commitments)


def test_a_wake_that_changed_nothing_is_flagged_for_review() -> None:
    wakes = [_wake(1, 2), _wake(2, 0), _wake(3, 0, commitments=True)]
    [finding] = IdleWake().run(view(scenario(OWNER), Log(), wakes=wakes)).findings
    assert (finding.wake, finding.kind, finding.pattern) == (2, FindingKind.REVIEW, "check_world_before_model")
    assert finding.message == (
        "wake 2 changed nothing in the world and nothing the agent was waiting on; no telemetry of the agent's "
        "model calls was received, so how many it made is not known"
    )


def test_an_idle_wake_states_the_model_calls_received_during_it_and_no_advice() -> None:
    wakes = [_wake(1, 2), _wake(2, 0), _wake(3, 0)]
    calls = [WakeModelCalls(wake=1, calls=3), WakeModelCalls(wake=2, calls=2), WakeModelCalls(wake=3, calls=0)]
    report = IdleWake().run(view(scenario(OWNER), Log(), wakes=wakes).model_copy(update={"model_calls": calls}))

    # Wake 3 changed nothing and made no model call: that cost nothing, and it is noted, not raised.
    [finding] = report.findings
    assert finding.wake == 2 and finding.message == (
        "wake 2 changed nothing in the world and nothing the agent was waiting on; 2 model calls were received or "
        "recorded during it"
    )
    assert report.notes == [
        "wake 3 changed nothing in the world and nothing the agent was waiting on, and made no model call: "
        "not wasted effort"
    ]


def test_wakes_that_each_changed_something_are_not_idle() -> None:
    assert IdleWake().run(view(scenario(OWNER), Log(), wakes=[_wake(1, 1), _wake(2, 0, True)])).findings == []


def test_the_reference_timeline_has_three_idle_wakes_and_the_world_capture_none_recorded() -> None:
    assert [f.wake for f in IdleWake().run(TIMELINE).findings] == [12, 13, 18]
    assert IdleWake().run(WORLD).blocked == ["idle_wake: no wakes recorded"]


def test_the_same_title_filed_twice_in_one_project_is_a_duplicate() -> None:
    log = Log()
    first = log.ticket("Review the contract", SOFIA, 0)
    second = log.ticket("review  the contract!", None, 1)
    [finding] = DuplicateTicket().run(view(scenario(SOFIA), log)).findings
    assert finding.evidence == [first.seq, second.seq] and finding.kind is FindingKind.FAIL


def test_the_same_title_in_another_project_or_after_the_first_was_closed_is_not_a_duplicate() -> None:
    log = Log()
    log.ticket("Review the contract", SOFIA, 0, external_id="T1")
    log.ticket("Review the contract", SOFIA, 1, project="Sales")
    log.ticket(
        "Review the contract", SOFIA, 2, operation=Operation.UPDATE, state=TicketState.CANCELLED, external_id="T1"
    )
    log.ticket("Review the contract", SOFIA, 3)
    assert DuplicateTicket().run(view(scenario(SOFIA), log)).findings == []


def test_the_reference_capture_files_no_duplicate_ticket() -> None:
    assert DuplicateTicket().run(WORLD).findings == []


def test_a_ticket_written_after_the_deadline_fails_and_a_message_alone_is_for_review() -> None:
    log = Log()
    log.message([OWNER], 20, text="on track")
    log.message([OWNER], 30, text="we missed the date", wake=2)
    filed = log.ticket("Review the contract", SOFIA, 31, wake=3)
    log.read(filed.entity, 32, wake=4)
    findings = ActedAfterDeadline().run(view(scenario(OWNER, SOFIA, deadline_after=timedelta(days=1)), log)).findings
    assert [(f.wake, f.kind) for f in findings] == [(2, FindingKind.REVIEW), (3, FindingKind.FAIL)]
    assert findings[1].evidence == [filed.seq]


def test_writes_before_the_deadline_are_not_late_and_no_deadline_is_noted() -> None:
    log = Log()
    log.ticket("Review the contract", SOFIA, 20)
    assert ActedAfterDeadline().run(view(scenario(SOFIA, deadline_after=timedelta(days=1)), log)).findings == []
    assert ActedAfterDeadline().run(view(scenario(SOFIA), log)).notes == ["scenario sets no deadline"]


REFUSED = Exchange(method="GET", host="api.unclaimed.example", path="/v1/things", status=502)


def test_a_call_no_provider_claimed_is_flagged_for_review() -> None:
    [finding] = UnmatchedCall().run(view(scenario(OWNER), Log(), unmatched=[REFUSED])).findings
    assert finding.message.startswith("GET api.unclaimed.example/v1/things reached no provider and was answered 502.")
    assert finding.kind is FindingKind.REVIEW


def test_a_refused_host_is_told_how_to_declare_it_whatever_its_name() -> None:
    """Said for every refused host, never guessed from its name: a tracker-looking host is told the same."""
    for host in ("api.unclaimed.example", "mail.vendor.example", "tracker.internal.example"):
        refused = REFUSED.model_copy(update={"host": host})
        [finding] = UnmatchedCall().run(view(scenario(OWNER), Log(), unmatched=[refused])).findings
        assert f"If {host} is not a place the agent keeps state" in finding.message
        assert all(word in finding.message for word in ("`outbound`", "acknowledge", "pass_through", "replay"))
        assert "--capture-unknown" in finding.message


def test_no_unmatched_calls_is_clean_and_an_unknown_list_is_blocked() -> None:
    assert UnmatchedCall().run(view(scenario(OWNER), Log(), unmatched=[])).findings == []
    report = UnmatchedCall().run(view(scenario(OWNER), Log()))
    assert report.findings == [] and report.blocked == [
        "unmatched_call: nobody recorded which calls reached no provider"
    ]


def test_the_same_ask_sent_twice_seconds_apart_is_a_repeat() -> None:
    log = Log()
    log.message([OWNER], 0, text="Could you send me the signed contract for Northwind?")
    log.message([OWNER], 20 / 3600, text="Could you send me the signed contract for Northwind please?")
    [finding] = RepeatedMessage().run(view(scenario(OWNER), log)).findings
    assert finding.evidence == [1, 2] and "20 seconds apart" in finding.message


def test_the_same_ask_days_apart_on_the_run_clock_is_not_a_repeat_however_close_in_real_time() -> None:
    # Until the window was measured on the simulated clock: a fortnight plays in seconds, so these two
    # messages, two simulated days apart, were twenty real seconds apart and flagged.
    log = Log()
    log.message([OWNER], 0, text="Could you send me the signed contract for Northwind?", wall=at(0))
    log.message(
        [OWNER],
        48,
        text="Could you send me the signed contract for Northwind please?",
        wall=at(0) + timedelta(seconds=20),
    )
    assert RepeatedMessage().run(view(scenario(OWNER), log)).findings == []


def test_two_asks_written_from_one_template_about_different_things_are_not_a_repeat() -> None:
    log = Log()
    template = "Could you approve, decline, or reply to the pending {}? Your response lets the work move on."
    log.message([OWNER], 0, text=template.format("ranking of vendors"), channel="a", wall=at(0))
    log.message([OWNER], 0, text=template.format("budget for the offsite"), channel="b", wall=at(0))
    log.message(
        [OWNER], 0, text=template.format("hiring plan for next quarter"), channel="b", wall=at(0) + timedelta(seconds=5)
    )
    assert RepeatedMessage().run(view(scenario(OWNER), log)).findings == []


def test_an_agent_message_after_a_reply_in_the_channel_is_not_a_repeat() -> None:
    log = Log()
    log.message([OWNER], 0, text="Could you send me the signed contract?", wall=at(0))
    log.message([], 0, text="Sending now", actor=Actor.PERSON, wall=at(0) + timedelta(seconds=10))
    log.message([OWNER], 0, text="Could you send me the signed contract?", wall=at(0) + timedelta(seconds=20))
    assert RepeatedMessage().run(view(scenario(OWNER), log)).findings == []


def test_a_ticket_written_twice_in_the_expected_state_is_one_ticket() -> None:
    # Until tickets, not events, were counted: the second write of the same done ticket made two, and
    # `at_most: 1` failed on a world holding one done ticket.
    log = Log()
    log.ticket("Review the contract", SOFIA, 0, external_id="T1")
    log.ticket("Review the contract", SOFIA, 5, operation=Operation.UPDATE, state=TicketState.DONE, external_id="T1")
    log.ticket("Review the contract", SOFIA, 6, operation=Operation.UPDATE, state=TicketState.DONE, external_id="T1")
    one = TicketInState(assignee="sofia", state=TicketState.DONE, at_most=1)
    [met] = Expectations().run(view(scenario(OWNER, SOFIA, expect=[one]), log)).findings
    assert met.kind is FindingKind.INFORMATIONAL
    log.ticket("Review the budget", SOFIA, 7, state=TicketState.DONE, external_id="T2")
    [finding] = Expectations().run(view(scenario(OWNER, SOFIA, expect=[one]), log)).findings
    assert finding.message == "ticket for sofia in state done: wanted at most 1, found 2"


def test_a_protected_name_misspelled_in_a_record_is_found() -> None:
    log = Log()
    log.record("invoices", "Invoice for Aiven, net 30", 1)
    world = view(scenario(OWNER).model_copy(update={"protected_names": ["Ayven"]}), log)
    [finding] = NearMissName().run(world).findings
    assert finding.message == 'wrote "Aiven" where the scenario says "Ayven"' and finding.evidence == [1]


def test_a_met_expectation_quotes_what_met_it_so_a_hollow_pass_shows() -> None:
    """Two passes seen on a real run: the owner told "confirmed" by a restatement of the question, with none of
    Sofia's answer in it; and "hello" asked for, met by a paragraph that contains the word."""
    log = Log()
    restated = log.message([OWNER], 1, text="Asked Sofia: has the venue been confirmed?")
    paragraph = "Hello all! " + "Here is a long update on the offsite, the budget and the agenda. " * 4
    greeting = log.message([SOFIA], 2, text=paragraph)
    expect: list[Expectation] = [
        PersonAsked(person="owner", mentions=["confirmed"]),
        PersonAsked(person="sofia", mentions=["hello"]),
    ]

    told, greeted = Expectations().run(view(scenario(OWNER, SOFIA, expect=expect), log)).findings

    assert (told.kind, told.pattern, told.evidence) == (FindingKind.INFORMATIONAL, None, [restated.seq])
    assert told.message == (
        "owner asked mentioning ['confirmed']: met by the message to Owner (seq 1): "
        "“Asked Sofia: has the venue been confirmed?”"
    )
    assert greeted.evidence == [greeting.seq]
    assert greeted.message.startswith(
        "sofia asked mentioning ['hello']: met by the message to Sofia (seq 2): “Hello all! Here"
    )
    assert greeted.message.endswith("…”") and len(greeted.message) < len(paragraph)
