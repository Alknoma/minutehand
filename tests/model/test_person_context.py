"""What a person a model writes for is shown: what they can see in the world and nothing else, their own earlier words
to stay consistent with, what they know as of the moment (changed only where the scenario says), the oldest beyond
their budget as a summary written once, and a fresh answer only when what they were shown differs."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.replier import PeopleReplier, owed_in
from minutehand.application.run_clock import RunClock
from minutehand.domain.conversation import Wrote
from minutehand.domain.scenario import Answers, DelayRange, FactChange, Person, Scenario, Silent
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, MessageSnapshot, Operation, WorldEvent
from tests.orchestrator.rig import T0, person, scenario
from tests.support.people import people_model, people_requests

ANA_DM = "dm-ana"


HOUR = DelayRange(shortest=timedelta(hours=1), longest=timedelta(hours=1))


def _person(key: str, facts: list[str], **more: object) -> Person:
    return Person.model_validate(
        {
            "key": key,
            "name": key.title(),
            "email": f"{key}@example.com",
            "facts": facts,
            "reply": Answers(delay=HOUR),
            **more,
        }
    )


def _say(
    store: SqliteStore, clock: RunClock, actor: Actor, text: str, to: list[str], ref: str, channel: str, at: datetime
) -> WorldEvent:
    clock.jump(at)
    return store.apply(
        Change(
            entity=EntityRef(provider="testchat", kind=EntityKind.MESSAGE, external_id=ref),
            operation=Operation.CREATE,
            actor=actor,
            body="{}",
            after=MessageSnapshot(text=text, channel=channel, recipient_emails=to),
        )
    )


def _world(tmp_path: Path, name: str = "world.db", run: str = "r") -> tuple[SqliteStore, RunClock]:
    clock = RunClock(T0)
    return SqliteStore(tmp_path / name, run, clock), clock


def _shown() -> str:
    """What the person was last shown: the one message of the latest request for a person."""
    messages = people_requests()[-1]["messages"]
    assert isinstance(messages, list)
    return str(messages[-1]["content"])


def _system() -> str:
    messages = people_requests()[-1]["messages"]
    assert isinstance(messages, list)
    return str(messages[0]["content"])


def _scn(*people: Person) -> Scenario:
    return scenario(ticket_fates=[], people=[person("owner", Silent()), *people])


async def _answer(replier: PeopleReplier, who: Person, asked: WorldEvent, store: SqliteStore, clock: RunClock) -> str:
    reply = await replier.decide(who, asked, store.events(), clock, store)
    assert reply is not None
    store.remember(reply)
    return reply.text


async def test_a_private_message_to_one_person_never_reaches_anothers_context(tmp_path: Path) -> None:
    ana, ben = _person("ana", ["The budget is 40k."]), _person("ben", ["The venue is booked."])
    store, clock = _world(tmp_path)
    replier = PeopleReplier(_scn(ana, ben), people_model())
    _say(store, clock, Actor.AGENT, "Ana, between us: what is the budget?", [ana.email], "m1", ANA_DM, T0)
    _say(store, clock, Actor.AGENT, "Team, the offsite is on.", [ana.email, ben.email], "m2", "team", T0)
    asked = _say(store, clock, Actor.AGENT, "Ben, is the venue booked?", [ben.email], "m3", "dm-ben", T0)
    await _answer(replier, ben, asked, store, clock)
    shown = _shown()
    assert "Team, the offsite is on." in shown, "a channel he is in is his to see"
    assert "between us" not in shown and "budget" not in shown


async def test_a_persons_earlier_words_are_shown_and_a_declared_fact_change_holds_from_its_moment(
    tmp_path: Path,
) -> None:
    ana = _person(
        "ana",
        ["The budget is 40k."],
        fact_changes=[FactChange(after=timedelta(days=1), facts=["The budget is 45k since the review."])],
    )
    store, clock = _world(tmp_path)
    replier = PeopleReplier(_scn(ana), people_model())
    first = _say(store, clock, Actor.AGENT, "What is the budget?", [ana.email], "m1", ANA_DM, T0)
    said = await _answer(replier, ana, first, store, clock)
    assert said == "The budget is 40k." and "- The budget is 40k." in _system()
    _say(store, clock, Actor.PERSON, said, [], "r1", ANA_DM, T0 + timedelta(hours=1))
    second = _say(
        store, clock, Actor.AGENT, "Still the same budget?", [ana.email], "m2", ANA_DM, T0 + timedelta(days=2)
    )
    again = await _answer(replier, ana, second, store, clock)
    system, shown = _system(), _shown()
    assert "- The budget is 45k since the review." in system and "40k" not in system
    assert "Stay consistent with what you said before" in system
    assert shown.index("They: What is the budget?") < shown.index("You: The budget is 40k.")
    assert shown.rstrip().endswith("They: Still the same budget?")
    assert again == "The budget is 45k since the review."


async def test_beyond_the_budget_the_oldest_turns_are_one_summary_written_once(tmp_path: Path) -> None:
    ana = _person("ana", ["The budget is 40k."], reply=Answers(delay=HOUR, history_turns=2))
    store, clock = _world(tmp_path)
    replier = PeopleReplier(_scn(ana), people_model())
    for n in range(3):
        _say(store, clock, Actor.AGENT, f"Note {n}.", [ana.email], f"n{n}", ANA_DM, T0 + timedelta(minutes=n))
    asked = _say(store, clock, Actor.AGENT, "What is the budget?", [ana.email], "q", ANA_DM, T0 + timedelta(hours=1))
    await _answer(replier, ana, asked, store, clock)
    summary, written = store.person_calls()
    assert summary.wrote is Wrote.SUMMARY and written.wrote is Wrote.REPLY
    shown = _shown()
    assert "Earlier, in short, as you remember it:\n2 earlier messages." in shown
    assert "Note 0." not in shown and "Note 1." not in shown and "They: Note 2." in shown
    asked_before = len(people_requests())
    await replier.decide(ana, asked, store.events(), clock, store)
    assert len(people_requests()) == asked_before and all(c.replayed for c in store.person_calls()[2:])


async def test_a_fork_that_changed_what_was_said_before_gets_a_fresh_answer_and_one_that_did_not_replays(
    tmp_path: Path,
) -> None:
    ana = _person("ana", ["The budget is 40k."])
    replier = PeopleReplier(_scn(ana), people_model())
    parent, clock = _world(tmp_path)
    _say(parent, clock, Actor.AGENT, "Hello Ana.", [ana.email], "m1", ANA_DM, T0)
    shared = parent.head()
    asked = _say(parent, clock, Actor.AGENT, "What is the budget?", [ana.email], "m2", ANA_DM, T0 + timedelta(hours=1))
    await _answer(replier, ana, asked, parent, clock)
    before = len(people_requests())

    same_clock = RunClock(T0)
    same = parent.fork("same", at_seq=shared, clock=same_clock)
    again = _say(
        same, same_clock, Actor.AGENT, "What is the budget?", [ana.email], "m2", ANA_DM, T0 + timedelta(hours=1)
    )
    await _answer(replier, ana, again, same, same_clock)
    assert len(people_requests()) == before and same.person_calls()[0].replayed

    other_clock = RunClock(T0)
    other = parent.fork("other", at_seq=0, clock=other_clock)
    _say(other, other_clock, Actor.AGENT, "Hello Ana, about the offsite.", [ana.email], "m1", ANA_DM, T0)
    changed = _say(
        other, other_clock, Actor.AGENT, "What is the budget?", [ana.email], "m2", ANA_DM, T0 + timedelta(hours=1)
    )
    await _answer(replier, ana, changed, other, other_clock)
    assert len(people_requests()) == before + 1 and not other.person_calls()[0].replayed
    assert owed_in(other) and "Hello Ana, about the offsite." in _shown()
