"""`Relayed`: a message from the agent carries what a person said, defined from the log through the tell the
scenario's author declares, and refused when the scenario lets the agent write the tell without hearing it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minutehand.checks.expectations import Expectations
from minutehand.domain.checks import FindingKind
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Direction, Relayed, Scenario, Scripted, ScriptedReply, Silent
from minutehand.domain.world import Actor
from tests.checks.world import Log, at, person, scenario, view

ANSWER = "The lakeside hall, booked for the 14th."
OWEN = person("owen", Scripted(replies=[]))
ROSA = person("rosa", Scripted(replies=[ScriptedReply(to_ask=1, text=ANSWER)]))
TOLD = Relayed(said_by="rosa", to="owen", tell="lakeside hall")


def _world(*, relay: str, before: str | None = None) -> tuple[Scenario, Log, list[PersonReply]]:
    """The agent asks Rosa; she answers at hour 30; the agent then writes `relay` to Owen. With `before`, the
    agent writes that to Owen first, before Rosa has said anything."""
    log = Log()
    if before is not None:
        log.message([OWEN], 0.5, text=before)
    ask = log.message([ROSA], 1, text="Which venue is booked for the offsite?")
    log.message([], 30, text=ANSWER, actor=Actor.PERSON)
    log.message([OWEN], 31, text=relay)
    answer = PersonReply(person="rosa", in_reply_to=ask.entity, text=ANSWER, at=at(30))
    return scenario(OWEN, ROSA, expect=[TOLD]), log, [answer]


def test_a_relay_carrying_the_persons_tell_after_they_said_it_is_met() -> None:
    world, log, replies = _world(relay="Rosa says: the lakeside hall, booked for the 14th.")
    [met] = Expectations().run(view(world, log, replies)).findings
    assert met.kind is FindingKind.INFORMATIONAL and met.evidence == [3]
    assert met.message.startswith("owen told what rosa said ('lakeside hall'): met by the message to Owen (seq 3)")


def test_restating_the_question_to_the_owner_relays_nothing() -> None:
    world, log, replies = _world(relay="I asked Rosa which venue is booked for the offsite; it is confirmed.")
    [failed] = Expectations().run(view(world, log, replies)).findings
    assert failed.kind is FindingKind.FAIL
    assert failed.message == "owen told what rosa said ('lakeside hall'): wanted at least 1, found 0"


def test_a_tell_the_agent_wrote_before_the_person_said_it_relays_nothing() -> None:
    world, log, replies = _world(relay="The lakeside hall it is.", before="I expect the Lakeside Hall.")
    [failed] = Expectations().run(view(world, log, replies)).findings
    assert failed.kind is FindingKind.FAIL
    assert failed.message == (
        "owen told what rosa said ('lakeside hall'): wanted at least 1, found 0: the agent wrote it (seq 1) "
        "before rosa said it, so nothing relayed it"
    )


def test_a_tell_nobody_said_relays_nothing() -> None:
    world, log, _ = _world(relay="Rosa says: the lakeside hall.")
    [failed] = Expectations().run(view(world, log, [])).findings
    assert failed.message.endswith(": someone else said it (seq 2) before rosa said it, so nothing relayed it")


def _refusal(**changes: object) -> str:
    fields = scenario(OWEN, ROSA, expect=[TOLD]).model_dump()
    fields.update(changes)
    with pytest.raises(ValidationError) as refused:
        Scenario.model_validate(fields)
    return str(refused.value)


def test_a_scenario_whose_goal_holds_the_tell_is_refused() -> None:
    assert "the tell 'lakeside hall' appears in the goal" in _refusal(goal="Book the Lakeside Hall with Rosa.")


def test_a_tell_in_a_direction_or_another_persons_reply_is_refused() -> None:
    told = Direction(text="Prefer the lakeside hall.", after=at(1) - at(0))
    assert "appears in direction 1" in _refusal(directions=[told.model_dump()])
    tom = person("tom", Scripted(replies=[ScriptedReply(to_ask=1, text="Try the lakeside hall.")]))
    assert "appears in what tom says or knows" in _refusal(people=[p.model_dump() for p in (OWEN, ROSA, tom)])


def test_a_tell_its_speaker_can_never_say_is_refused() -> None:
    silent = person("rosa", Silent())
    assert "rosa is silent" in _refusal(people=[p.model_dump() for p in (OWEN, silent)])
    elsewhere = person("rosa", Scripted(replies=[ScriptedReply(to_ask=1, text="The town hall.")]))
    assert "no scripted reply of rosa holds the tell" in _refusal(people=[p.model_dump() for p in (OWEN, elsewhere)])


def test_a_relay_to_the_speaker_themself_is_refused() -> None:
    assert "rosa is both" in _refusal(expect=[Relayed(said_by="rosa", to="rosa", tell="lakeside hall").model_dump()])
