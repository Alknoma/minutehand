"""Checks on how the agent treated people: chasing someone away, chasing what was already done."""

from __future__ import annotations

from datetime import timedelta

from minutehand.checks.chased_absent_person import ChasedAbsentPerson
from minutehand.checks.kept_chasing_after_done import KeptChasingAfterDone
from minutehand.domain.checks import FindingKind
from minutehand.domain.scenario import Absence, AbsenceTrigger
from minutehand.domain.world import Actor, Operation, TicketState

from tests.checks.world import Log, person, reply, scenario, view

OWNER, MARCUS = person("owner"), person("marcus")


def _away(delegate: str | None) -> list[Absence]:
    return [Absence(trigger=AbsenceTrigger.ON_FIRST_ASK, starts_after=timedelta(hours=1),
                    lasts=timedelta(hours=48), delegate=delegate)]


def test_a_message_to_someone_away_with_a_delegate_is_found() -> None:
    sofia = person("sofia", absences=_away("marcus"))
    log = Log()
    log.message([sofia], 0)
    chased = log.message([sofia], 10, text="Any news?")
    [finding] = ChasedAbsentPerson().run(view(scenario(OWNER, sofia, MARCUS), log)).findings
    assert finding.evidence == [chased.seq] and finding.kind is FindingKind.FAIL
    assert finding.message == "messaged sofia while they were away; marcus was covering"


def test_messages_before_or_after_the_absence_are_not_found() -> None:
    sofia = person("sofia", absences=_away("marcus"))
    log = Log()
    log.message([sofia], 0)
    log.message([sofia], 50, text="Welcome back")
    assert ChasedAbsentPerson().run(view(scenario(OWNER, sofia, MARCUS), log)).findings == []


def test_an_absence_with_nobody_covering_is_not_found_and_says_so() -> None:
    sofia = person("sofia", absences=_away(None))
    log = Log()
    log.message([sofia], 0)
    log.message([sofia], 10)
    report = ChasedAbsentPerson().run(view(scenario(OWNER, sofia), log))
    assert report.findings == [] and report.notes == ["nobody in the scenario is away with a delegate"]


SOFIA = person("sofia")


def test_a_message_in_the_thread_of_an_ask_already_answered_is_flagged_for_review() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    again = log.message([SOFIA], 6, text="Just checking on this", thread_of=ask.entity.external_id)
    [finding] = KeptChasingAfterDone().run(view(scenario(SOFIA), log, [reply(SOFIA, ask, 5)])).findings
    assert finding.kind is FindingKind.REVIEW and finding.evidence == [ask.seq, again.seq]


def test_a_chase_in_the_thread_before_the_answer_is_not_flagged() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 3, text="Just checking on this", thread_of=ask.entity.external_id)
    log.message([OWNER], 9, text="status")
    assert KeptChasingAfterDone().run(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 5)])).findings == []


def test_a_message_naming_a_ticket_its_holder_already_finished_is_flagged() -> None:
    log = Log()
    log.ticket("Review the contract", SOFIA, 0, external_id="T1")
    log.ticket("Review the contract", SOFIA, 10, actor=Actor.PERSON, operation=Operation.UPDATE,
               state=TicketState.DONE, external_id="T1")
    chase = log.message([SOFIA], 12, text="Is 'review the contract' done yet?")
    log.message([SOFIA], 13, text="Could you also look at the invoice?")
    [finding] = KeptChasingAfterDone().run(view(scenario(SOFIA), log)).findings
    assert finding.evidence == [1, chase.seq] and finding.message.endswith("already finished")
