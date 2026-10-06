"""A question asked by editing a message: the person answers the message as it reads, not its placeholder."""

from __future__ import annotations

from datetime import timedelta

from minutehand import session
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.domain.people import PersonReply
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Person, Scenario
from minutehand.domain.world import Actor, MessageSnapshot, Operation, WorldEvent
from minutehand.ports.clock import Clock
from tests.orchestrator.rig import T0, Rig, scenario

PLACEHOLDER = "Thinking..."
QUESTION = "Can you confirm the pricing?"


class Overheard:
    """`ports.people.Replier`: the scripted replier, keeping the text of every message put to someone."""

    def __init__(self, scn: Scenario) -> None:
        self._scripted = ScriptedReplier(scn)
        self.asked: list[tuple[str, str]] = []

    async def decide(
        self, person: Person, asked: WorldEvent, history: list[WorldEvent], clock: Clock
    ) -> PersonReply | None:
        assert isinstance(asked.after, MessageSnapshot)
        self.asked.append((person.key, asked.after.text))
        return await self._scripted.decide(person, asked, history, clock)


def _person_messages(events: list[WorldEvent]) -> list[WorldEvent]:
    return [
        e
        for e in events
        if e.actor is Actor.PERSON and e.operation is Operation.CREATE and isinstance(e.after, MessageSnapshot)
    ]


async def test_a_placeholder_edited_into_a_question_in_the_same_wake_is_answered_once_about_the_question(
    rig: Rig,
) -> None:
    scn = scenario(ticket_fates=[])
    heard = Overheard(scn)

    record, store, _ = await rig.run(scn, rig.agent("placeholder"), replier=heard)

    assert heard.asked == [("sofia", QUESTION)]
    assert record.stop is StopReason.AGENT_DONE
    [reply] = _person_messages(store.events())
    assert reply.sim_time == T0 + timedelta(hours=36)
    assert len(rig.chat.pushed) == 1


async def test_a_placeholder_edited_in_a_later_wake_is_asked_again_and_its_first_answer_never_arrives(
    rig: Rig,
) -> None:
    scn = scenario(ticket_fates=[])
    heard = Overheard(scn)

    record, store, _ = await rig.run(scn, rig.agent("placeholder_later"), replier=heard)

    # The placeholder was all sofia could see when the first wake ended; the edit an hour later replaced it
    # before her answer was due, and the answer that arrives is the one to the question. The edit to the
    # same text, and the edit after she answered, are put to nobody.
    assert heard.asked == [("sofia", PLACEHOLDER), ("sofia", QUESTION)]
    assert record.stop is StopReason.AGENT_DONE
    [reply] = _person_messages(store.events())
    assert reply.sim_time == T0 + timedelta(hours=1) + timedelta(hours=36)
    assert len(rig.chat.pushed) == 1
    assert [w.sim_time for w in record.wakes] == [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=37)]


class AnswersOnlyThePlaceholder(Overheard):
    """Sofia answers the placeholder and has nothing to say to the question it was edited into."""

    async def decide(
        self, person: Person, asked: WorldEvent, history: list[WorldEvent], clock: Clock
    ) -> PersonReply | None:
        reply = await super().decide(person, asked, history, clock)
        assert isinstance(asked.after, MessageSnapshot)
        return None if asked.after.text == QUESTION else reply


async def test_a_reply_withdrawn_by_an_edit_that_gets_no_answer_settles_no_wait(rig: Rig) -> None:
    scn = scenario(ticket_fates=[])
    heard = AnswersOnlyThePlaceholder(scn)

    record, store, _ = await rig.run(scn, rig.agent("placeholder_later"), replier=heard)
    result = await session._Judge(scn, None, judging=False).score(record, store)  # pyright: ignore[reportPrivateUsage]

    # Her answer to the placeholder was decided, stored, and withdrawn when the placeholder became the question;
    # nothing ever reached the agent, so the wait on her is still open at the end.
    assert heard.asked == [("sofia", PLACEHOLDER), ("sofia", QUESTION)]
    [withdrawn] = store.replies()
    assert withdrawn.at == T0 + timedelta(hours=36) and _person_messages(store.events()) == []
    assert record.ended_at > withdrawn.at
    assert result.effectiveness.waits_opened == 1 and result.effectiveness.waits_open_at_end == 1
