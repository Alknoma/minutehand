"""The checks that stay Minutehand's, read straight off the log: calls nobody claimed, the scenario's expectations, its
protected names. How the agent should behave is the team's rules' to judge (`tests/checks/test_assessments.py`)."""

from __future__ import annotations

from minutehand.checks.expectations import Expectations
from minutehand.checks.near_miss_name import NearMissName
from minutehand.checks.unmatched_call import UnmatchedCall
from minutehand.domain.checks import FindingKind
from minutehand.domain.scenario import Expectation, PersonAsked, TicketInState
from minutehand.domain.world import Exchange, Operation, TicketState
from tests.checks.world import Log, person, scenario, view

OWNER, SOFIA = person("owner"), person("sofia")


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
