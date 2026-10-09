"""A question asked by editing a message: the person answers the message as it reads, not its placeholder."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from minutehand import session
from minutehand.application.moments import Owed
from minutehand.application.replier import PeopleReplier
from minutehand.domain.people import PersonReply, Plan
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Person, Scenario
from minutehand.domain.world import Actor, MessageSnapshot, Operation, WorldEvent
from minutehand.ports.clock import Clock
from minutehand.ports.model import ModelFailed
from minutehand.ports.store import Store
from tests.orchestrator.rig import T0, Rig, scenario

PLACEHOLDER = "Thinking..."
QUESTION = "Can you confirm the pricing?"


class Overheard:
    """`ports.people.Replier`: the scripted replier, keeping the text of every message put to someone."""

    def __init__(self, scn: Scenario) -> None:
        self._scripted = PeopleReplier(scn, None)
        self.asked: list[tuple[str, str]] = []

    def plan(
        self, person: Person, asked: WorldEvent, history: Sequence[WorldEvent], owed: Sequence[Owed]
    ) -> Plan | None:
        assert isinstance(asked.after, MessageSnapshot)
        self.asked.append((person.key, asked.after.text))
        return self._scripted.plan(person, asked, history, owed)

    async def write(
        self,
        person: Person,
        asked: WorldEvent,
        plan: Plan,
        history: Sequence[WorldEvent],
        world: Store,
        clock: Clock,
    ) -> PersonReply | None:
        return await self._scripted.write(person, asked, plan, history, world, clock)


def _person_messages(events: list[WorldEvent]) -> list[WorldEvent]:
    return [
        e
        for e in events
        if e.actor is Actor.PERSON and e.operation is Operation.CREATE and isinstance(e.after, MessageSnapshot)
    ]


async def test_a_placeholder_edited_into_a_question_in_the_same_wake_is_answered_once_about_the_question(
    rig: Rig,
) -> None:
    scn = scenario(tom_finishes=False)
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
    scn = scenario(tom_finishes=False)
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

    async def write(
        self,
        person: Person,
        asked: WorldEvent,
        plan: Plan,
        history: Sequence[WorldEvent],
        world: Store,
        clock: Clock,
    ) -> PersonReply | None:
        reply = await super().write(person, asked, plan, history, world, clock)
        assert isinstance(asked.after, MessageSnapshot)
        return None if asked.after.text == QUESTION else reply


async def test_an_ask_edited_into_one_that_needs_no_answer_opens_no_wait(rig: Rig) -> None:
    scn = scenario(tom_finishes=False)
    heard = AnswersOnlyThePlaceholder(scn)

    record, store, _ = await rig.run(scn, rig.agent("placeholder_later"), replier=heard)
    result = await session._Judge(scn, None, judging=False).score(record, store)  # pyright: ignore[reportPrivateUsage]

    # Her answer to the placeholder was written, kept with the engine's record, and written again from the question
    # when the placeholder became it before her moment: the question she has nothing to say to. Nothing reaches the
    # agent, nothing is kept as said, and no wait was opened on her.
    # Mutation: keeping the words written for the placeholder lands a reply and opens a wait.
    assert heard.asked == [("sofia", PLACEHOLDER), ("sofia", QUESTION)]
    assert store.replies() == [] and _person_messages(store.events()) == []
    assert rig.chat.pushed == []
    assert result.effectiveness.waits_opened == 0


class FailsThenFindsNothingToSay(Overheard):
    """Sofia's model fails as her answer is planned and again on the next look; written again at her moment, it finds
    nothing to say."""

    def __init__(self, scn: Scenario) -> None:
        super().__init__(scn)
        self.writes = 0

    async def write(
        self,
        person: Person,
        asked: WorldEvent,
        plan: Plan,
        history: Sequence[WorldEvent],
        world: Store,
        clock: Clock,
    ) -> PersonReply | None:
        self.writes += 1
        if self.writes <= 2:
            raise ModelFailed("503 from the model service")
        return None


async def test_an_answer_that_needs_none_when_written_at_its_moment_wakes_nobody(rig: Rig) -> None:
    scn = scenario(tom_finishes=False)
    heard = FailsThenFindsNothingToSay(scn)

    record, store, _ = await rig.run(scn, rig.agent("placeholder"), replier=heard)

    # Written again at her moment, it needs no answer: passed before anyone hears of it. Mutation: keeping it among
    # what is due wakes the agent for an answer that never comes.
    assert heard.writes == 3
    assert record.stop is StopReason.NOTHING_PENDING and [w.sim_time for w in record.wakes] == [T0]
    assert rig.chat.pushed == [] and store.replies() == []
