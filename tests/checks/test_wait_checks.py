"""The time checks: a follow-up that came late, one that never came, an answer left sitting."""

from __future__ import annotations

from datetime import timedelta

from minutehand.checks.late_follow_up import LateFollowUp
from minutehand.checks.no_follow_up import NoFollowUp
from minutehand.checks.runner import evaluate
from minutehand.checks.slow_to_react import SlowToReact
from minutehand.domain.checks import FindingKind
from minutehand.domain.scenario import Absence, Silent
from tests.checks.world import Log, at, person, reply, scenario, view
from tests.test_checks_on_reference_run import TIMELINE, WORLD

OWNER, SOFIA = person("owner"), person("sofia")
DANIA = person("dania", reply=Silent())


def _asked_and_chased(chase_at: float) -> Log:
    log = Log()
    log.message([SOFIA], 0)
    log.message([SOFIA], chase_at, text="Any news on the contract?")
    log.message([OWNER], 40, text="status")
    return log


def test_a_follow_up_hours_after_the_wait_expired_is_late() -> None:
    log = _asked_and_chased(10)
    findings = LateFollowUp().run(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, log.events[0], 30)])).findings
    assert [(f.kind, f.message, f.evidence) for f in findings] == [
        (FindingKind.FAIL, "followed up 8 hours after the wait expired", [1, 2])
    ]
    assert findings[0].at == at(10) and findings[0].pattern == "expiry_on_every_wait"


def test_a_follow_up_inside_the_grace_is_not_late() -> None:
    log = _asked_and_chased(2.5)
    assert LateFollowUp().run(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, log.events[0], 30)])).findings == []


def test_the_33_hour_late_reminder_on_the_reference_run_is_found() -> None:
    [finding] = LateFollowUp().run(TIMELINE).findings
    assert finding.message == "followed up 33 hours after the wait expired" and finding.evidence[-1] == 4


def test_a_time_check_without_obligations_is_blocked_not_clean() -> None:
    for check in (LateFollowUp(), NoFollowUp(), SlowToReact()):
        report = check.run(WORLD)
        assert report.findings == [] and report.blocked and report.blocked[0].startswith(check.id)


def test_a_silent_person_never_chased_after_the_wait_expired_is_no_follow_up() -> None:
    log = Log()
    log.message([DANIA], 0)
    log.message([OWNER], 100, text="status")
    [finding] = NoFollowUp().run(view(scenario(OWNER, DANIA), log)).findings
    assert finding.kind is FindingKind.FAIL and finding.evidence == [1] and finding.at == at(66)
    assert "dania" in finding.message


def test_a_silent_person_chased_after_expiry_is_not_no_follow_up() -> None:
    log = Log()
    log.message([DANIA], 0)
    log.message([DANIA], 70, text="Any news?")
    log.message([OWNER], 100, text="status")
    assert NoFollowUp().run(view(scenario(OWNER, DANIA), log)).findings == []


def test_no_follow_up_finds_nothing_on_the_reference_timeline() -> None:
    assert NoFollowUp().run(TIMELINE).findings == []


def test_an_answer_the_agent_came_back_to_hours_later_is_slow() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 20, text="Thanks, filing it.")
    [finding] = SlowToReact().run(view(scenario(SOFIA), log, [reply(SOFIA, ask, 5)])).findings
    assert finding.message == "answer from sofia landed and the agent came back to it 15 hours later"
    assert finding.evidence == [1, 2]


def test_an_answer_the_agent_never_came_back_to_is_slow() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 30, text="status")
    [finding] = SlowToReact().run(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 5)])).findings
    assert "never came back to it in the 25 hours left" in finding.message


def test_an_answer_acted_on_inside_the_grace_is_not_slow() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 5.5, text="Thanks, filing it.")
    assert SlowToReact().run(view(scenario(SOFIA), log, [reply(SOFIA, ask, 5)])).findings == []


def test_a_wait_naming_nobody_cannot_be_timed_and_says_so() -> None:
    report = SlowToReact().run(TIMELINE)
    assert report.findings == []
    assert report.notes == ["7 settled obligation(s) name no person or entity; no reaction can be timed"]


def _reminded_once(remind_at: float, ends_at: float) -> Log:
    """The diligent example on a silent person: ask, one reminder, then nothing until the run ends."""
    log = Log()
    log.message([DANIA], 0)
    log.message([DANIA], remind_at, text="Following up: could you review the contract?")
    log.message([OWNER], ends_at, text="status")
    return log


def test_an_early_reminder_counts_and_the_wait_left_after_it_says_what_happened() -> None:
    # Until a follow-up before the expected date counted: a reminder 48 hours into a 66-hour wait was
    # ignored, the scorecard said "made: 0" and the finding said the agent never came back.
    log = _reminded_once(48, 336)
    [finding] = NoFollowUp().run(view(scenario(OWNER, DANIA), log)).findings
    assert finding.message == (
        "wait on dania: the agent followed up once, the last 2 days after the ask, then nothing; "
        "due again 4 days 18 hours after the ask, it sat 9 days 6 hours until the run ended"
    )
    assert finding.at == at(114) and finding.evidence == [1, 2]
    card = evaluate(view(scenario(OWNER, DANIA), log)).effectiveness
    assert (card.waits_opened, card.waits_open_at_end) == (1, 1)
    assert (card.follow_ups_due, card.follow_ups_made, card.follow_ups_late) == (1, 1, 1)
    assert card.time_lost == timedelta(hours=336 - 114)


def test_a_follow_up_gives_the_person_their_whole_delay_again() -> None:
    log = Log()
    log.message([DANIA], 0)
    log.message([DANIA], 48, text="Following up: could you review the contract?")
    log.message([DANIA], 114.5, text="Following up again on the contract.")
    log.message([OWNER], 170, text="status")
    world = view(scenario(OWNER, DANIA), log)
    assert NoFollowUp().run(world).findings == [] and LateFollowUp().run(world).findings == []
    card = evaluate(world).effectiveness
    assert (card.follow_ups_due, card.follow_ups_made, card.follow_ups_late) == (1, 2, 0)


def test_a_read_of_the_channel_is_not_a_follow_up() -> None:
    log = Log()
    ask = log.message([DANIA], 0)
    log.read(ask.entity, 70)
    log.message([OWNER], 100, text="status")
    [finding] = NoFollowUp().run(view(scenario(OWNER, DANIA), log)).findings
    assert "never came back to it" in finding.message


def test_an_edit_that_leaves_the_text_as_it_was_is_not_a_follow_up() -> None:
    log = Log()
    ask = log.message([DANIA], 0, text="hello")
    log.edit(ask, 70, text="hello")
    log.message([OWNER], 100, text="status")
    [finding] = NoFollowUp().run(view(scenario(OWNER, DANIA), log)).findings
    assert "never came back to it" in finding.message


def test_an_edit_that_changes_the_text_is_a_follow_up() -> None:
    log = Log()
    ask = log.message([DANIA], 0, text="hello")
    log.edit(ask, 70, text="hello again: any news?")
    log.message([OWNER], 100, text="status")
    assert NoFollowUp().run(view(scenario(OWNER, DANIA), log)).findings == []


def test_a_thank_you_to_someone_who_answered_is_not_an_open_wait_or_a_slow_reaction() -> None:
    away_after = person("sofia", absences=[Absence(starts_after=timedelta(hours=1.2), lasts=timedelta(days=9))])
    log = Log()
    ask = log.message([away_after], 0)
    log.message([away_after], 1.5, text="Thank you!")
    log.message([OWNER], 30, text="The contract is signed.")
    world = view(scenario(OWNER, away_after), log, [reply(away_after, ask, 1)])
    result = evaluate(world)
    assert [f.check for f in result.findings] == []
    assert (result.effectiveness.waits_opened, result.effectiveness.waits_open_at_end) == (1, 0)
    assert (result.effectiveness.reactions_due, result.effectiveness.reactions_slow) == (1, 0)
