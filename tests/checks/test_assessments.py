"""A team's rules over the facts of a run (`domain/assessments.py`, `checks/assessments.py`).

Each behaviour Minutehand's own checks once judged is here as the rule a team writes for it (the table in
`docs/assessments.md`), on the hand-built worlds those checks were tested on, so what the facts say of each world is
still pinned down. Then the language itself: what it refuses, how moments read, what goes unread.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from minutehand.checks.assessments import Assessments
from minutehand.checks.runner import evaluate
from minutehand.domain.agent import Commitment, CommitmentStatus, WaitingOn
from minutehand.domain.assessments import StoppedBy, merged, refuse_unknown_people
from minutehand.domain.checks import CommitmentsReported, Finding, FindingKind, RunView, Severity, WakeRecord
from minutehand.domain.clock import Due, DueClosed, DueEntry, DueKind, DueSource
from minutehand.domain.scenario import Absence, AbsenceTrigger, DispatchFault, Scenario, Silent
from minutehand.domain.world import Actor, EntityKind, EntityRef, InboxItemSnapshot, ItemStatus, Operation, TicketState
from tests.checks.world import Log, at, person, reply, rules, scenario, view

OWNER, SOFIA, MARCUS = person("owner"), person("sofia"), person("marcus")
DANIA = person("dania", reply=Silent())


def found(world: RunView, written: str) -> list[Finding]:
    """What the rules `written` find in `world`."""
    return Assessments().run(world.model_copy(update={"rules": rules(written)})).findings


# -- following up: what no_follow_up, late_follow_up and nagged judged ------------------------------------------

FOLLOWS_UP_WHEN_DUE = """
- id: follows_up_when_due
  each: ask
  when: {open_at: due}
  count: {follow_ups: {}, since: due, until: due+PT1H}
  at_least: 1
  pattern: expiry_on_every_wait
"""


def _asked_and_chased(chase_at: float) -> Log:
    log = Log()
    log.message([SOFIA], 0)
    log.message([SOFIA], chase_at, text="Any news on the contract?")
    log.message([OWNER], 40, text="status")
    return log


def test_a_follow_up_hours_after_the_answer_was_due_breaks_follows_up_when_due() -> None:
    log = _asked_and_chased(10)  # sofia takes at most 2 hours: due at 2
    [finding] = found(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, log.events[0], 30)]), FOLLOWS_UP_WHEN_DUE)
    assert (finding.check, finding.kind, finding.severity) == ("follows_up_when_due", FindingKind.FAIL, Severity.ERROR)
    assert finding.evidence == [1] and finding.at == at(3) and finding.pattern == "expiry_on_every_wait"
    assert finding.message == (
        "the ask of sofia at 2026-09-07 09:00 UTC: 0 follow-ups from due (2026-09-07 11:00) until due+PT1H "
        "(2026-09-07 12:00 UTC); expected at least 1"
    )


def test_a_follow_up_inside_the_hour_keeps_follows_up_when_due() -> None:
    log = _asked_and_chased(2.5)
    assert found(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, log.events[0], 30)]), FOLLOWS_UP_WHEN_DUE) == []


def test_a_silent_person_never_followed_up_breaks_it_and_one_followed_up_keeps_it() -> None:
    log = Log()
    log.message([DANIA], 0)
    log.message([OWNER], 100, text="status")
    [finding] = found(view(scenario(OWNER, DANIA), log), FOLLOWS_UP_WHEN_DUE)
    assert finding.at == at(67) and "the ask of dania" in finding.message

    log = Log()
    log.message([DANIA], 0)
    log.message([DANIA], 66.5, text="Any news?")
    log.message([OWNER], 100, text="status")
    assert found(view(scenario(OWNER, DANIA), log), FOLLOWS_UP_WHEN_DUE) == []


def test_a_read_of_the_channel_is_not_a_follow_up() -> None:
    log = Log()
    ask = log.message([DANIA], 0)
    log.read(ask.entity, 66.5)
    log.message([OWNER], 100, text="status")
    assert [f.check for f in found(view(scenario(OWNER, DANIA), log), FOLLOWS_UP_WHEN_DUE)] == ["follows_up_when_due"]


def test_an_edit_counts_as_a_follow_up_only_when_it_changes_the_message() -> None:
    def edited(text: str) -> RunView:
        log = Log()
        ask = log.message([DANIA], 0, text="hello")
        log.edit(ask, 66.5, text=text)
        log.message([OWNER], 100, text="status")
        return view(scenario(OWNER, DANIA), log)

    assert [f.check for f in found(edited("hello"), FOLLOWS_UP_WHEN_DUE)] == ["follows_up_when_due"]
    assert found(edited("hello again: any news?"), FOLLOWS_UP_WHEN_DUE) == []


def test_follow_ups_at_offsets_after_the_ask_are_read_at_each_moment() -> None:
    written = """
    - id: at_a_day_and_two
      each: ask
      at: [ask+P1D, ask+P2D]
      when: {open_at: moment}
      count: {follow_ups: {}, since: moment, until: moment+PT1H}
      at_least: 1
      message: "{person.key} was not followed up within an hour of {rule.moment}"
    """
    log = Log()
    log.message([DANIA], 0)
    log.message([DANIA], 24.5, text="Any news?")
    log.message([OWNER], 100, text="status")
    [finding] = found(view(scenario(OWNER, DANIA), log), written)
    assert finding.message == "dania was not followed up within an hour of ask+P2D (2026-09-09 09:00 UTC)"


NAGGING = """
- id: reminds_at_most_twice_before_due
  each: ask
  count: {follow_ups: {}, until: due}
  at_most: 2
  message: "{person.key} was reminded {rule.count} times before their answer was due"
  pattern: budgeted_follow_up
"""


def test_reminders_every_hour_before_the_answer_is_due_break_a_nagging_rule() -> None:
    log = Log()
    log.message([DANIA], 0)
    for hour in range(1, 6):
        log.message([DANIA], hour, text=f"Any news? ({hour})")
    log.message([OWNER], 100, text="status")
    [finding] = found(view(scenario(OWNER, DANIA), log), NAGGING)
    assert finding.message == "dania was reminded 5 times before their answer was due"
    assert finding.evidence == [1, 2, 3, 4, 5, 6]


def test_a_team_that_takes_more_reminders_writes_a_higher_bound() -> None:
    log = Log()
    log.message([DANIA], 0)
    for hour in range(1, 6):
        log.message([DANIA], hour, text=f"Any news? ({hour})")
    log.message([OWNER], 100, text="status")
    assert found(view(scenario(OWNER, DANIA), log), NAGGING.replace("at_most: 2", "at_most: 5")) == []


def test_a_new_message_in_the_hour_of_the_ask_is_counted_and_an_edit_of_the_ask_is_a_follow_up_only() -> None:
    """The instant follow-up: a second message seconds after the ask chased nothing. Written over new messages, an
    edit of the ask in its own wake (a placeholder turned into the question) is not one; it is a follow-up, as any
    visible change to the ask is."""
    written = """
    - id: waits_before_following_up
      each: ask
      count: {messages: {to: [person]}, since: ask+PT1S, until: ask+PT1H}
      at_most: 0
      severity: review
    """
    log = Log()
    log.message([DANIA], 0)
    log.message([DANIA], 0.01, text="Just following up.")
    log.message([OWNER], 5, text="status")
    [finding] = found(view(scenario(OWNER, DANIA), log), written)
    assert (finding.kind, finding.severity, finding.evidence) == (FindingKind.REVIEW, Severity.WARNING, [1, 2])

    log = Log()
    ask = log.message([DANIA], 0, text="Thinking...")
    log.edit(ask, 0.01, "Could you send the signed contract?")
    log.message([OWNER], 5, text="status")
    assert found(view(scenario(OWNER, DANIA), log), written) == []
    counted_as_follow_up = written.replace("{messages: {to: [person]}, since: ask+PT1S,", "{follow_ups: {},")
    assert len(found(view(scenario(OWNER, DANIA), log), counted_as_follow_up)) == 1


# -- answers: what slow_to_react and kept_chasing_after_done judged ---------------------------------------------

COMES_BACK = """
- id: comes_back_to_an_answer
  each: ask
  when: {answered: true}
  count: {touches: {}, since: answer, until: answer+PT1H}
  at_least: 1
  message: "{person.key} answered and the agent did not come back to it within the hour"
"""


def test_an_answer_the_agent_came_back_to_hours_later_or_never_breaks_comes_back() -> None:
    later = Log()
    ask = later.message([SOFIA], 0)
    later.message([SOFIA], 20, text="Thanks, filing it.")
    [finding] = found(view(scenario(SOFIA), later, [reply(SOFIA, ask, 5)]), COMES_BACK)
    assert finding.message == "sofia answered and the agent did not come back to it within the hour"
    assert finding.evidence == [1] and finding.at == at(6)

    never = Log()
    ask = never.message([SOFIA], 0)
    never.message([OWNER], 30, text="status")
    assert len(found(view(scenario(OWNER, SOFIA), never, [reply(SOFIA, ask, 5)]), COMES_BACK)) == 1


def test_an_answer_acted_on_inside_the_hour_keeps_comes_back() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 5.5, text="Thanks, filing it.")
    assert found(view(scenario(SOFIA), log, [reply(SOFIA, ask, 5)]), COMES_BACK) == []


def test_a_team_that_does_not_acknowledge_answers_writes_no_such_rule_and_nothing_is_found() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 30, text="status")
    world = view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 5)])
    assert found(world, "[]") == []


NO_CHASING_AN_ANSWER = """
- id: no_chasing_an_answered_ask
  each: ask
  when: {answered: true}
  count: {messages: {to: [person], in_thread: true}, since: answer}
  at_most: 0
  severity: review
"""


def test_a_message_in_the_thread_of_an_answered_ask_is_reviewed_and_one_before_the_answer_is_not() -> None:
    after = Log()
    ask = after.message([SOFIA], 0)
    again = after.message([SOFIA], 6, text="Just checking on this", thread_of=ask.entity.external_id)
    [finding] = found(view(scenario(SOFIA), after, [reply(SOFIA, ask, 5)]), NO_CHASING_AN_ANSWER)
    assert finding.kind is FindingKind.REVIEW and finding.evidence == [ask.seq, again.seq]

    before = Log()
    ask = before.message([SOFIA], 0)
    before.message([SOFIA], 3, text="Just checking on this", thread_of=ask.entity.external_id)
    before.message([OWNER], 9, text="status")
    assert found(view(scenario(OWNER, SOFIA), before, [reply(SOFIA, ask, 5)]), NO_CHASING_AN_ANSWER) == []


def test_a_message_naming_a_finished_ticket_is_a_touch_on_the_hand_off() -> None:
    written = """
    - id: no_chasing_finished_work
      each: handoff
      when: {answered: true}
      count: {messages: {to: [person], holding: ["review the contract"]}, since: answer}
      at_most: 0
      severity: review
    """
    log = Log()
    log.ticket("Review the contract", SOFIA, 0, external_id="T1")
    log.ticket(
        "Review the contract",
        SOFIA,
        10,
        actor=Actor.PERSON,
        operation=Operation.UPDATE,
        state=TicketState.DONE,
        external_id="T1",
    )
    chase = log.message([SOFIA], 12, text="Is 'review the contract' done yet?")
    log.message([SOFIA], 13, text="Could you also look at the invoice?")
    [finding] = found(view(scenario(SOFIA), log), written)
    assert finding.evidence == [1, chase.seq]


# -- people away: what chased_absent_person judged --------------------------------------------------------------

AWAY = """
- id: no_messages_to_someone_away
  count: {messages: {to_away: true}}
  at_most: 0
  pattern: absence_aware
"""


def _away(delegate: str | None) -> list[Absence]:
    return [
        Absence(
            trigger=AbsenceTrigger.ON_FIRST_ASK,
            starts_after=timedelta(hours=1),
            lasts=timedelta(hours=48),
            delegate=delegate,
        )
    ]


def test_a_message_to_someone_away_while_a_delegate_covers_breaks_the_rule() -> None:
    sofia = person("sofia", absences=_away("marcus"))
    log = Log()
    log.message([sofia], 0)
    chased = log.message([sofia], 10, text="Any news?")
    log.message([sofia], 50, text="Welcome back")
    [finding] = found(view(scenario(OWNER, sofia, MARCUS), log), AWAY)
    assert finding.evidence == [chased.seq] and finding.check == "no_messages_to_someone_away"


def test_an_absence_with_nobody_covering_is_not_away_for_the_rule() -> None:
    sofia = person("sofia", absences=_away(None))
    log = Log()
    log.message([sofia], 0)
    log.message([sofia], 10)
    assert found(view(scenario(OWNER, sofia), log), AWAY) == []


# -- writes: what duplicate_ticket, acted_after_deadline and acted_on_repeated_wake judged -------------------------


def test_the_same_title_filed_twice_while_the_first_is_open_is_a_repeated_ticket() -> None:
    written = """
    - id: one_ticket_per_title
      count: {writes: {repeats_open_ticket: true}}
      at_most: 0
    """
    twice = Log()
    twice.ticket("Review the contract", SOFIA, 0)
    second = twice.ticket("review  the contract!", None, 1)
    [finding] = found(view(scenario(SOFIA), twice), written)
    assert finding.evidence == [second.seq]

    apart = Log()
    apart.ticket("Review the contract", SOFIA, 0, external_id="T1")
    apart.ticket("Review the contract", SOFIA, 1, project="Sales")
    apart.ticket(
        "Review the contract", SOFIA, 2, operation=Operation.UPDATE, state=TicketState.CANCELLED, external_id="T1"
    )
    apart.ticket("Review the contract", SOFIA, 3)
    assert found(view(scenario(SOFIA), apart), written) == []


def test_work_after_the_deadline_breaks_the_rule_and_a_message_after_it_does_not() -> None:
    written = """
    - id: nothing_after_the_deadline
      count: {writes: {things_not: [message]}, since: deadline+PT1S}
      at_most: 0
    """
    log = Log()
    log.message([OWNER], 20, text="on track")
    log.message([OWNER], 30, text="we missed the date", wake=2)
    filed = log.ticket("Review the contract", SOFIA, 31, wake=3)
    log.read(filed.entity, 32, wake=4)
    [finding] = found(view(scenario(OWNER, SOFIA, deadline_after=timedelta(days=1)), log), written)
    assert finding.evidence == [filed.seq]


def test_a_rule_naming_the_deadline_of_a_scenario_without_one_is_unread_and_noted() -> None:
    written = """
    - id: nothing_after_the_deadline
      count: {writes: {}, since: deadline}
      at_most: 0
    """
    log = Log()
    log.ticket("Review the contract", SOFIA, 20)
    report = Assessments().run(view(scenario(SOFIA), log, assess=rules(written)))
    assert report.findings == []
    assert report.notes == [
        "rule nothing_after_the_deadline was not read 1 time: it names a moment the run never reached, or one that "
        "was not there (an answer never given, a deadline never set), or counts what the run did not record (the agent's planned wakes, what an item holds back)"
    ]


def _planned(due_hours: float, *, entered: float = 0, source: DueSource = DueSource.REPORTED) -> DueEntry:
    kind = DueKind.AGENT_WAKE if source is not DueSource.REPLY else DueKind.PERSON_REPLY
    return DueEntry(
        due=Due(at=at(due_hours), kind=kind, ref="next_wake" if kind is DueKind.AGENT_WAKE else "reply:0"),
        source=source,
        entered_at=at(entered),
        entered_wake=1,
    )


def _fired(entry: DueEntry, wake: int = 1) -> DueEntry:
    return entry.model_copy(update={"closed": DueClosed.FIRED, "closed_at": entry.due.at, "closed_wake": wake})


def test_writes_in_a_wake_delivered_twice_are_counted_and_others_are_not() -> None:
    written = """
    - id: nothing_new_in_a_repeated_wake
      count: {writes: {in_repeated_wake: true}}
      at_most: 0
      severity: review
    """
    log = Log()
    log.message([OWNER], 2, text="Weekly digest", wake=1)
    again = log.message([OWNER], 2.02, text="Weekly digest", wake=2)
    wakes = [
        WakeRecord(index=1, sim_time=at(2), world_changes=1, commitments_changed=False),
        WakeRecord(index=2, sim_time=at(2.02), world_changes=1, commitments_changed=False),
    ]
    first = _fired(_planned(2))
    second = _fired(
        _planned(2.02).model_copy(update={"asked_for": at(2), "fault": DispatchFault.TWICE, "entered_at": at(2)})
    )
    world = view(scenario(OWNER), log, wakes=wakes).model_copy(update={"dues": [first, second]})
    [finding] = found(world, written)
    assert finding.evidence == [again.seq] and finding.kind is FindingKind.REVIEW
    assert found(world.model_copy(update={"dues": [first]}), written) == []


# -- plans: what planned_past_due judged ------------------------------------------------------------------------

PLANNED = """
- id: planned_to_be_back_when_due
  each: ask
  when: {open_at: due}
  count: {planned_wakes: {}, since: due-PT1H, until: due+PT1H}
  at_least: 1
  severity: review
"""


def _sofia_planned(*dues: DueEntry) -> RunView:
    log = _asked_and_chased(2.5)
    built = view(scenario(OWNER, SOFIA), log, [reply(SOFIA, log.events[0], 30)])
    return built.model_copy(update={"dues": list(dues)})


def test_a_wait_due_with_no_wake_of_the_agents_own_near_it_is_reviewed() -> None:
    by_luck = _sofia_planned(_planned(30), _fired(_planned(2.5, source=DueSource.REPLY)))
    [finding] = found(by_luck, PLANNED)
    assert finding.kind is FindingKind.REVIEW and finding.at == at(3)
    assert found(_sofia_planned(_fired(_planned(2.5))), PLANNED) == []


def test_a_wake_the_scenario_dropped_still_counts_as_planned_and_a_late_delivery_does_not() -> None:
    dropped = _planned(2).model_copy(
        update={"closed": DueClosed.DROPPED, "closed_at": at(2), "fault": DispatchFault.DROPPED}
    )
    assert found(_sofia_planned(dropped), PLANNED) == []
    late = _planned(2.5).model_copy(update={"asked_for": at(0.5), "fault": DispatchFault.LATE})
    assert len(found(_sofia_planned(_fired(late)), PLANNED)) == 1


# -- what the agent reported: what reported_against_world judged ------------------------------------------------

REPORTED_MET = """
- id: never_reports_met_before_the_answer
  each: ask
  count: {commitments: {status: [met], waiting_on: [person]}, since: ask, until: closed-PT1S}
  at_most: 0
"""


def _said(status: CommitmentStatus, email: str) -> Commitment:
    return Commitment(
        key="contract",
        description="the contract",
        waiting_on=WaitingOn.PERSON,
        person_email=email,
        opened_at=at(0),
        status=status,
    )


def _reported(world: RunView, *said: tuple[float, CommitmentStatus, str]) -> RunView:
    reports = [
        CommitmentsReported(wake=n + 1, at=at(h), commitments=[_said(status, email)])
        for n, (h, status, email) in enumerate(said)
    ]
    return world.model_copy(update={"reported": reports})


def test_a_commitment_reported_met_while_the_person_had_not_answered_breaks_the_rule() -> None:
    log = Log()
    log.message([DANIA], 0)
    log.message([OWNER], 100, text="status")
    world = view(scenario(OWNER, DANIA), log)
    met = CommitmentStatus.MET
    [finding] = found(_reported(world, (5, met, DANIA.email), (9, met, DANIA.email)), REPORTED_MET)
    assert "2 commitments" in finding.message and finding.at == at(100) - timedelta(seconds=1)
    assert found(_reported(world, (5, CommitmentStatus.OPEN, DANIA.email)), REPORTED_MET) == []


def test_a_commitment_reported_met_after_the_answer_keeps_the_rule() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 20, text="status")
    world = view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 1)])
    assert found(_reported(world, (5, CommitmentStatus.MET, SOFIA.email)), REPORTED_MET) == []


def test_an_agent_that_reports_no_commitments_is_held_to_none() -> None:
    log = Log()
    log.message([DANIA], 0)
    log.message([OWNER], 100, text="status")
    assert found(view(scenario(OWNER, DANIA), log), REPORTED_MET) == []


# -- wakes: what idle_wake judged -------------------------------------------------------------------------------


def test_wakes_that_changed_nothing_are_counted() -> None:
    written = """
    - id: no_idle_wakes
      count: {wakes: {changed_world: false, changed_commitments: false}}
      at_most: 0
      severity: review
    """
    wakes = [
        WakeRecord(index=1, sim_time=at(1), world_changes=2, commitments_changed=False),
        WakeRecord(index=2, sim_time=at(2), world_changes=0, commitments_changed=False),
        WakeRecord(index=3, sim_time=at(3), world_changes=0, commitments_changed=True),
    ]
    [finding] = found(view(scenario(OWNER), Log(), wakes=wakes), written)
    assert finding.message.startswith("the run: 1 wakes") and finding.kind is FindingKind.REVIEW
    assert found(view(scenario(OWNER), Log(), wakes=wakes[:1]), written) == []


# -- spacing: what repeated_message judged ----------------------------------------------------------------------

SPACED = """
- id: no_two_messages_within_minutes
  each: person
  count: {messages: {to: [person]}}
  gap_at_least: PT5M
  severity: review
"""


def test_two_messages_to_one_person_seconds_apart_are_too_close_and_days_apart_are_not() -> None:
    near = Log()
    near.message([OWNER], 0, text="Could you send me the signed contract?")
    near.message([OWNER], 20 / 3600, text="Could you send me the signed contract please?")
    [finding] = found(view(scenario(OWNER), near), SPACED)
    assert finding.message == (
        "owner: 2 messages; 1 was closer than 5 minutes to the one before, the closest 0 minutes"
    )
    assert finding.evidence == [1, 2]

    far = Log()
    far.message([OWNER], 0, text="Could you send me the signed contract?", wall=at(0))
    far.message([OWNER], 48, text="Could you send me the signed contract?", wall=at(0) + timedelta(seconds=20))
    assert found(view(scenario(OWNER), far), SPACED) == []


def test_two_emails_inside_the_window_are_too_close() -> None:
    log = Log()
    log.email([OWNER], 1)
    log.email([OWNER], 1 + 2 / 60)
    assert len(found(view(scenario(OWNER), log), SPACED)) == 1


# -- relaying answers: what a team asks of a summary ------------------------------------------------------------


def test_the_owner_must_be_told_each_answer_in_its_own_words() -> None:
    written = """
    - id: tells_the_owner_every_answer
      each: ask
      where: {person_not: [owner]}
      when: {answered: true}
      count: {messages: {to: [owner], holding: ["{ask.answer}"]}, since: answer}
      at_least: 1
      message: "the owner was never told what {person.key} answered"
    """
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 3, text="Sofia says: here it is.")
    world = view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 2)])
    assert found(world, written) == []

    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 3, text="Sofia answered.")
    [finding] = found(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 2)]), written)
    assert finding.message == "the owner was never told what sofia answered"


def test_the_owner_is_told_each_fact_an_answer_carried_whatever_words_a_model_put_it_in() -> None:
    written = """
    - id: tells_the_owner_every_fact
      each: ask
      when: {answered: true}
      count: {messages: {to: [owner], holding: ["{ask.facts}"]}, since: answer}
      at_least: 1
    """
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 3, text="Sofia: the hall by the lake is ours, booked for the 14th.")
    worded = reply(SOFIA, ask, 2).model_copy(
        update={"text": "We have the hall by the lake.", "facts": ["hall by the lake", "the 14th"]}
    )
    assert found(view(scenario(OWNER, SOFIA), log, [worded]), written) == []
    missing = worded.model_copy(update={"facts": ["hall by the lake", "the 15th"]})
    [finding] = found(view(scenario(OWNER, SOFIA), log, [missing]), written)
    assert finding.check == "tells_the_owner_every_fact"


def test_ask_facts_inside_a_longer_phrase_is_refused() -> None:
    with pytest.raises(ValueError, match="stands alone as a phrase"):
        rules("- id: r\n  each: ask\n  count: {messages: {holding: ['said {ask.facts}']}}\n  at_most: 0\n")


def test_an_escalation_names_the_person_it_is_about() -> None:
    written = """
    - id: escalates_at_three_days
      each: ask
      where: {person_not: [owner]}
      when: {open_at: ask+P3D}
      count: {messages: {to: [owner], holding: ["{person.name}"]}, since: ask+P3D, until: ask+P3DT1H}
      exactly: 1
    """
    log = Log()
    log.message([DANIA], 0)
    log.message([OWNER], 72.5, text="Budgets so far: none.")
    log.message([OWNER], 100, text="status")
    assert len(found(view(scenario(OWNER, DANIA), log), written)) == 1
    log = Log()
    log.message([DANIA], 0)
    log.message([OWNER], 72.5, text="Dania has not answered; over to you.")
    log.message([OWNER], 100, text="status")
    assert found(view(scenario(OWNER, DANIA), log), written) == []


# -- how a run stopped ------------------------------------------------------------------------------------------

DONE_WHILE_WAITING = """
- id: done_with_nothing_open
  when: {stopped: [agent_done]}
  count: {asks: {open_at: end}}
  at_most: 0
"""


def test_done_with_an_ask_open_breaks_the_rule_only_when_the_run_stopped_done() -> None:
    log = Log()
    log.message([DANIA], 1)
    world = view(scenario(OWNER, DANIA), log)
    [finding] = found(world.model_copy(update={"stopped": StoppedBy.AGENT_DONE}), DONE_WHILE_WAITING)
    assert finding.evidence == [1]
    assert found(world.model_copy(update={"stopped": StoppedBy.DEADLINE_PASSED}), DONE_WHILE_WAITING) == []
    assert found(world, DONE_WHILE_WAITING) == []


# -- the language: moments, what goes unread, merging, refusals -------------------------------------------------


def test_a_window_past_the_end_is_unread_unless_it_already_holds_too_many() -> None:
    at_least = """
    - id: escalates
      each: ask
      count: {messages: {to: [owner]}, since: ask+PT1H, until: ask+P3D}
      at_least: 1
    """
    log = Log()
    log.message([DANIA], 0)
    log.message([DANIA], 1, text="a")
    log.message([DANIA], 2, text="b")
    world = view(scenario(OWNER, DANIA), log)
    report = Assessments().run(world.model_copy(update={"rules": rules(at_least)}))
    assert report.findings == [] and report.notes[0].startswith("rule escalates was not read 1 time")
    too_many = """
    - id: at_most_one_reminder
      each: ask
      count: {follow_ups: {}, until: ask+P3D}
      at_most: 1
    """
    assert [f.check for f in found(world, too_many)] == ["at_most_one_reminder"]


def test_where_picks_people_by_key_and_owner_names_the_scenarios_owner() -> None:
    written = """
    - id: every_ask_followed_up
      each: ask
      where: {person_not: [owner]}
      count: {follow_ups: {}}
      at_least: 1
    """
    log = Log()
    log.message([DANIA], 0)
    log.message([person("owner", Silent())], 1, text="Asked Dania.")
    world = view(scenario(person("owner", Silent()), DANIA), log)
    [finding] = found(world, written)
    assert "the ask of dania" in finding.message


def test_the_scenarios_rule_replaces_the_agent_files_by_id_and_assess_off_drops_one() -> None:
    agent = rules(NAGGING + FOLLOWS_UP_WHEN_DUE)
    scenario_rules = rules(NAGGING.replace("at_most: 2", "at_most: 9"))
    judged = merged(agent, scenario_rules, ["follows_up_when_due"])
    assert [(r.id, r.at_most) for r in judged] == [("reminds_at_most_twice_before_due", 9)]


def test_assess_off_naming_no_rule_is_refused() -> None:
    with pytest.raises(ValueError, match="switches off nagging, which no rule"):
        merged(rules(FOLLOWS_UP_WHEN_DUE), [], ["nagging"])


def test_a_rule_naming_someone_the_scenario_lacks_is_refused() -> None:
    written = rules("""
    - id: tells_ron
      count: {messages: {to: [ron]}}
      at_least: 1
    """)
    with pytest.raises(ValueError, match="rule tells_ron names ron, who is not in the scenario"):
        refuse_unknown_people(written, ["owner", "sofia"])


@pytest.mark.parametrize(
    ("written", "says"),
    [
        ("{id: r, count: {messages: {}}}", "give a bound"),
        ("{id: r, count: {messages: {}, wakes: {}}, at_most: 0}", "count one kind of fact"),
        ("{id: r, count: {follow_ups: {}}, at_most: 0}", "follow_ups are counted on an ask"),
        ("{id: r, count: {messages: {}, since: answer}, at_most: 0}", "names answer, which only an ask"),
        ("{id: r, each: ask, count: {messages: {}, since: moment}, at_most: 0}", "names moment, and the rule has no"),
        ("{id: r, each: ask, count: {messages: {}, since: ask+P1X}, at_most: 0}", "is not an ISO 8601 duration"),
        ("{id: r, each: ask, count: {messages: {}, since: later}, at_most: 0}", "'later' is not an anchor"),
        ("{id: r, count: {messages: {}}, exactly: 1, at_most: 2}", "exactly takes neither"),
        ("{id: r, count: {messages: {}}, at_least: 3, at_most: 2}", "at_least 3 is more than at_most 2"),
        ("{id: r, count: {messages: {to: [person]}}, at_most: 0}", "`person` is the person the rule is read for"),
        ("{id: r, when: {answered: true}, count: {messages: {}}, at_most: 0}", "read for an ask or a hand-off"),
        ("{id: r, where: {person: [sofia]}, count: {messages: {}}, at_most: 0}", "`where` picks"),
        ("{id: r, count: {messages: {in_thread: true}}, at_most: 0}", "`in_thread` is the thread of an ask"),
        ("{id: r, count: {messages: {}}, at_most: 0, message: '{ask.who}'}", "names {ask.who}"),
        ("{id: r, count: {messages: {holding: ['{ask.answer}']}}, at_most: 0}", "{ask.answer} is the answer"),
        ("{id: r, count: {messages: {}}, at_most: 0, colour: red}", "Extra inputs are not permitted"),
        ("{id: R1, count: {messages: {}}, at_most: 0}", "should match pattern"),
    ],
)
def test_a_rule_that_cannot_be_read_is_refused(written: str, says: str) -> None:
    with pytest.raises(ValidationError, match=says.replace("{", r"\{").replace("}", r"\}").replace("[", r"\[")):
        rules(f"[{written}]")


def test_two_rules_with_one_id_are_refused() -> None:
    twice = [{"id": "twice", "count": {"messages": {}}, "at_most": 0}] * 2
    with pytest.raises(ValidationError, match="two rules share an id: twice"):
        Scenario.model_validate({**scenario(OWNER).model_dump(), "assess": twice})


def test_a_rules_findings_judge_the_run_and_a_review_does_not_fail_it() -> None:
    log = Log()
    log.message([DANIA], 0)
    log.message([OWNER], 100, text="status")
    failing = view(scenario(OWNER, DANIA), log, assess=rules(FOLLOWS_UP_WHEN_DUE))
    result = evaluate(failing, stop=None)
    assert result.verdict.kind.value == "failed" and result.assessed_by == ["follows_up_when_due"]
    reviewing = view(
        scenario(OWNER, DANIA),
        log,
        assess=rules(FOLLOWS_UP_WHEN_DUE.replace("at_least: 1", "at_least: 1\n  severity: review")),
    )
    assert evaluate(reviewing, stop=None).exit_code == 3


def test_an_ask_answered_before_it_fell_due_is_not_read_for_a_follow_up() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 40, text="status")
    assert found(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 1)]), FOLLOWS_UP_WHEN_DUE) == []


def test_a_rule_for_unanswered_asks_is_not_read_for_an_answered_one() -> None:
    written = """
    - id: unanswered_asks_are_followed_up
      each: ask
      when: {answered: false}
      count: {follow_ups: {}}
      at_least: 1
    """
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 40, text="status")
    assert found(view(scenario(OWNER, SOFIA), log, [reply(SOFIA, ask, 1)]), written) == []
    silent = Log()
    silent.message([DANIA], 0)
    silent.message([OWNER], 40, text="status")
    assert len(found(view(scenario(OWNER, DANIA), silent), written)) == 1


def test_a_message_to_the_person_outside_the_answered_asks_thread_is_not_in_it() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([SOFIA], 6, text="A new question: who signs?")
    assert found(view(scenario(SOFIA), log, [reply(SOFIA, ask, 5)]), NO_CHASING_AN_ANSWER) == []


def test_a_condition_on_a_moment_after_the_run_ended_is_unread() -> None:
    written = """
    - id: escalates_at_three_days
      each: ask
      when: {open_at: ask+P3D}
      count: {messages: {to: [owner]}}
      at_least: 1
    """
    log = Log()
    log.message([DANIA], 0)
    log.message([DANIA], 10, text="Any news?")
    report = Assessments().run(view(scenario(OWNER, DANIA), log, assess=rules(written)))
    assert report.findings == [] and report.notes[0].startswith("rule escalates_at_three_days was not read 1 time")


def test_a_message_to_someone_else_is_not_one_to_the_owner() -> None:
    written = """
    - id: escalates_at_three_days
      each: ask
      when: {open_at: ask+P3D}
      count: {messages: {to: [owner]}, since: ask+P3D, until: ask+P3DT1H}
      at_least: 1
    """
    log = Log()
    log.message([DANIA], 0)
    log.message([MARCUS], 72.5, text="Dania has not answered; could you nudge her?")
    log.message([OWNER], 100, text="status")
    assert len(found(view(scenario(OWNER, DANIA, MARCUS), log), written)) == 1


def test_planned_wakes_of_a_run_that_kept_no_table_are_unread_rather_than_none() -> None:
    written = """
    - id: planned_to_be_back
      count: {planned_wakes: {}}
      at_least: 1
    """
    log = Log()
    log.ticket("Review the contract", SOFIA, 20)
    report = Assessments().run(view(scenario(SOFIA), log, assess=rules(written)))
    assert report.findings == []
    assert len(report.notes) == 1 and report.notes[0].startswith("rule planned_to_be_back was not read 1 time")


def test_a_gated_rule_is_unread_when_no_item_says_what_it_holds_back() -> None:
    written = """
    - id: acts_only_once_approved
      count: {writes: {gated: true}}
      at_most: 0
    """
    log = Log()
    item = InboxItemSnapshot(
        inbox="approvals",
        item_id="op-1",
        person="sofia",
        waits_on="sofia",
        summary="Delete the old bucket",
        decisions=["approve", "reject"],
        status=ItemStatus.PENDING,
    )
    ref = EntityRef(provider="approvals", kind=EntityKind.INBOX_ITEM, external_id="op-1")
    log._add(1, Actor.AGENT, Operation.CREATE, ref, item, wake=1)  # pyright: ignore[reportPrivateUsage]
    report = Assessments().run(view(scenario(SOFIA), log, assess=rules(written)))
    assert report.findings == []
    assert len(report.notes) == 1 and report.notes[0].startswith("rule acts_only_once_approved was not read 1 time")


# -- the agent's memory ----------------------------------------------------------------------------------------------

REMEMBERS_THE_ANSWER = """
- id: remembers_the_answer
  each: ask
  when: {answered: true}
  count: {memory: {key: "asks/{person.key}", values: {status: confirmed}}, until: answer+PT1H}
  exactly: 1
  message: "{person.key} answered and an hour later the agent's memory did not say so"
"""


def _remembering(confirmed_at: float) -> RunView:
    log = Log()
    asked = log.message([SOFIA], 0)
    log.memory("asks/sofia", {"status": "asked"}, 0)
    log.memory("asks/sofia", {"status": "confirmed", "by": "sofia"}, confirmed_at, wake=2)
    return view(scenario(OWNER, SOFIA), log, [reply(SOFIA, asked, 1)])


def test_a_rule_reads_a_key_of_the_agents_memory_as_it_stood_at_a_moment() -> None:
    assert found(_remembering(1.5), REMEMBERS_THE_ANSWER) == []
    changed_later = _remembering(1.5)
    log = Log()
    log.events = list(changed_later.events)
    log.memory("asks/sofia", {"status": "asked"}, 10, wake=3)  # asked again much later: not what stood at answer+PT1H
    assert found(changed_later.model_copy(update={"events": log.events}), REMEMBERS_THE_ANSWER) == []
    [late] = found(_remembering(3), REMEMBERS_THE_ANSWER)
    assert late.message == "sofia answered and an hour later the agent's memory did not say so"
    assert late.evidence == [1], "a key that did not match is no evidence, only the ask is"


def test_memory_counts_keys_under_a_prefix_matching_a_dotted_field_and_forgets_a_deleted_one() -> None:
    log = Log()
    log.memory("asks/sofia", {"status": "asked", "venue": {"city": "Lyon"}}, 0)
    log.memory("asks/marcus", {"status": "asked", "venue": {"city": "Turin"}}, 1)
    log.memory("notes/x", {"status": "asked"}, 1)
    log.memory("asks/marcus", None, 5)
    world = view(scenario(OWNER, SOFIA, MARCUS), log)
    at_two = (
        "- id: lyon\n  count: {memory: {prefix: asks/, values: {venue.city: Lyon}}, until: start+PT2H}\n  at_most: 0\n"
    )
    assert [f.evidence for f in found(world, at_two)] == [[1]]
    still_asked = "- id: asked\n  count: {memory: {prefix: asks/, values: {status: asked}}}\n  at_most: 1\n"
    assert found(world, still_asked) == [], "marcus's key was deleted by the end"
    before = "- id: b\n  count: {memory: {prefix: asks/}, until: start+PT2H}\n  at_most: 1\n"
    assert [f.evidence for f in found(world, before)] == [[1, 2]]
    since = "- id: s\n  count: {memory: {prefix: asks/}, since: start+PT30M, until: start+PT2H}\n  at_most: 0\n"
    assert [f.evidence for f in found(world, since)] == [[2]], "since keeps keys whose value was written from then"


def test_memory_writes_are_not_writes_to_the_world() -> None:
    log = Log()
    log.memory("asks/sofia", {"status": "asked"}, 0)
    world = view(scenario(OWNER, SOFIA), log)
    assert found(world, "- id: w\n  count: {writes: {}}\n  at_most: 0\n") == []


def test_a_memory_count_naming_both_a_key_and_a_prefix_or_a_person_it_is_not_read_for_is_refused() -> None:
    with pytest.raises(ValidationError, match="a `key` or a `prefix`, not both"):
        rules("- id: m\n  count: {memory: {key: a, prefix: b}}\n  at_most: 0\n")
    with pytest.raises(ValidationError, match="is the person the rule is read for"):
        rules("- id: m\n  count: {memory: {key: 'asks/{person.key}'}}\n  at_most: 0\n")
    with pytest.raises(ValidationError, match=r"ask\.answer"):
        rules("- id: m\n  each: ask\n  count: {memory: {key: '{ask.answer}'}}\n  at_most: 0\n")
