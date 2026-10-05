"""The obligations ledger, derived from the world and the replies, never from the agent."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.checks.ledger import build
from minutehand.checks.runner import evaluate_run
from minutehand.domain.checks import ObligationKind
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Absence, DelayRange, Silent, TicketFate, TicketState
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation
from tests.checks.world import START, Log, at, person, reply, scenario

OWNER, SOFIA, MARCUS = person("owner"), person("sofia"), person("marcus")
DANIA = person("dania", reply=Silent())


def test_an_answered_ask_opens_and_settles_when_the_reply_lands() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    log.message([OWNER], 10, text="status")
    [o] = [o for o in build(scenario(OWNER, SOFIA), log.events, [reply(SOFIA, ask, 5)]) if o.person == "sofia"]
    assert o.kind is ObligationKind.ANSWER_FROM_PERSON and o.entity == ask.entity and o.opened_by == ask.seq
    assert o.expected_by == at(2) and o.settled_at == at(5)


def test_a_reply_the_run_never_reached_does_not_settle() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    [o] = build(scenario(SOFIA), log.events, [reply(SOFIA, ask, 5)])
    assert o.settled_at is None


def test_an_ask_nobody_will_answer_opens_nothing_unless_the_person_is_silent() -> None:
    away = person("marcus", absences=[Absence(lasts=timedelta(days=2))])
    log = Log()
    log.message([SOFIA, DANIA, away], 1)
    opened = build(scenario(SOFIA, DANIA, away), log.events, [])
    assert [o.person for o in opened] == ["dania"]
    assert opened[0].expected_by == at(1) + DelayRange().longest and opened[0].settled_at is None
    assert opened[0].patience == DelayRange().longest


def test_a_thank_you_to_someone_away_who_already_answered_opens_no_wait() -> None:
    # Until the replier's decision was the only source: a message to someone away opened a wait even when
    # nobody would answer it, so the thank-you below was a second, never-settled wait on sofia.
    away_after = person("sofia", absences=[Absence(starts_after=timedelta(hours=1.2), lasts=timedelta(days=9))])
    log = Log()
    ask = log.message([away_after], 0)
    log.message([away_after], 1.5, text="Thank you!")
    [o] = build(scenario(away_after), log.events, [reply(away_after, ask, 1)])
    assert o.opened_by == ask.seq and o.settled_at == at(1)


def test_a_follow_up_in_the_same_conversation_is_a_touch_on_the_open_wait_not_a_wait_of_its_own() -> None:
    log = Log()
    ask = log.message([DANIA], 0)
    chased = log.message([DANIA], 48, text="Following up: could you review it?")
    [o] = build(scenario(DANIA), log.events, [])
    assert o.opened_by == ask.seq and o.agent_touches == [chased.seq]
    assert o.expected_by == at(66)


def test_a_message_after_the_wait_settled_or_in_another_conversation_opens_its_own() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    elsewhere = log.message([DANIA], 0.5, channel="general")
    other_channel = log.message([DANIA], 1, text="Any news?", channel="dm-dania")
    later = log.message([SOFIA], 10, text="One more question: who signs?")
    log.message([OWNER], 20, text="status")
    opened = build(scenario(OWNER, SOFIA, DANIA), log.events, [reply(SOFIA, ask, 5), reply(SOFIA, later, 12)])
    assert [(o.person, o.opened_by) for o in opened] == [
        ("sofia", ask.seq),
        ("dania", elsewhere.seq),
        ("dania", other_channel.seq),
        ("sofia", later.seq),
    ]


def test_an_answer_to_the_follow_up_settles_the_wait_it_chased() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    chased = log.message([SOFIA], 3, text="Any news?")
    log.message([OWNER], 60, text="status")
    [o] = [o for o in build(scenario(OWNER, SOFIA), log.events, [reply(SOFIA, ask, 50), reply(SOFIA, chased, 4)])]
    assert o.opened_by == ask.seq and o.settled_at == at(4)


def test_a_handed_off_ticket_waits_for_its_fate_and_settles_only_when_the_person_finishes_it() -> None:
    log = Log()
    filed = log.ticket("Review the contract", SOFIA, 0, external_id="T1")
    log.ticket(
        "Review the contract",
        SOFIA,
        10,
        actor=Actor.SCENARIO,
        operation=Operation.UPDATE,
        state=TicketState.DONE,
        external_id="T1",
    )
    log.ticket(
        "Review the contract",
        SOFIA,
        20,
        actor=Actor.PERSON,
        operation=Operation.UPDATE,
        state=TicketState.DONE,
        external_id="T1",
    )
    fate = TicketFate(assignee="sofia", becomes=TicketState.DONE, after=timedelta(days=3))
    [o] = build(scenario(SOFIA, fates=[fate]), log.events, [])
    assert o.kind is ObligationKind.WORK_WITH_PERSON and o.opened_by == filed.seq
    assert o.expected_by == at(72) and o.settled_at == at(20)


def test_a_reassignment_opens_work_with_the_new_holder_and_an_unchanged_update_opens_nothing() -> None:
    log = Log()
    log.ticket("Review the contract", SOFIA, 0, external_id="T1")
    log.ticket("Review the contract", SOFIA, 1, operation=Operation.UPDATE, external_id="T1")
    log.ticket("Review the contract", MARCUS, 2, operation=Operation.UPDATE, external_id="T1")
    assert [o.person for o in build(scenario(SOFIA, MARCUS), log.events, [])] == ["sofia", "marcus"]


def test_the_deadline_is_one_date_obligation_that_settles_when_the_clock_passes_it() -> None:
    log = Log()
    log.message([OWNER], 30, text="update")
    world = scenario(OWNER, deadline_after=timedelta(days=1))
    [date] = [o for o in build(world, log.events, []) if o.kind is ObligationKind.DATE]
    assert date.expected_by == at(24) and date.settled_at == at(24) and date.opened_at == START


def test_touches_count_the_person_their_delegate_and_the_entity_and_nothing_else() -> None:
    away = person("sofia", reply=Silent(), absences=[Absence(lasts=timedelta(days=9), delegate="marcus")])
    log = Log()
    ask = log.message([away], 0)
    to_delegate = log.message([MARCUS], 1)
    read_ask = log.read(ask.entity, 2)
    log.read(EntityRef(provider="chat", kind=EntityKind.MESSAGE, external_id="other"), 3)
    log.message([OWNER], 4)
    chased = log.message([away], 5)
    [o] = [o for o in build(scenario(OWNER, away, MARCUS), log.events, []) if o.opened_by == ask.seq]
    assert o.agent_touches == [to_delegate.seq, read_ask.seq, chased.seq]


def test_the_first_touch_after_an_answer_is_recorded_and_not_counted_as_open() -> None:
    log = Log()
    ask = log.message([SOFIA], 0)
    before = log.message([SOFIA], 1, text="any news?")
    after = log.message([SOFIA], 6, text="thanks, filing it")
    log.message([SOFIA], 7, text="one more thing")
    [first, *_] = build(scenario(SOFIA), log.events, [reply(SOFIA, ask, 5)])
    assert first.agent_touches == [before.seq] and first.first_touch_after_settled == after.seq


def test_the_ledger_reads_a_real_store_and_its_stored_replies(tmp_path: Path) -> None:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    clock.begin_wake()
    message = EntityRef(provider="chat", kind=EntityKind.MESSAGE, external_id="m1")
    ask = store.apply(
        Change(
            entity=message,
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body="{}",
            after=MessageSnapshot(text="Could you sign?", channel="dm", recipients=[SOFIA.key]),
        )
    )
    store.remember(PersonReply(person="sofia", in_reply_to=message, text="Signed.", at=at(1)))
    clock.jump(at(9))
    clock.begin_wake()
    store.apply(Change(entity=message, operation=Operation.READ, actor=Actor.AGENT))
    result = evaluate_run(scenario(SOFIA), store.events(), [], store.replies(), stop=None)
    assert result.effectiveness.waits_opened == 1 and result.effectiveness.reactions_slow == 1
    assert [f.check for f in result.findings] == ["slow_to_react"] and result.findings[0].evidence[0] == ask.seq
