"""People whose replies a model writes, on real events from the real store, against a completions server on
this machine. The server's rules stand in for what a model would decide; what is tested is what the model is
told, what is done with its answer, and when the answer lands."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.model.openai_compatible import OpenAICompatible
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.refusals import RunRefused
from minutehand.application.replier_model import (
    HELPFULNESS,
    PERSON_PROMPT_VERSION,
    ModelReplier,
    PeopleReplier,
    WrittenReply,
)
from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.run_clock import RunClock
from minutehand.domain.conversation import Provenance
from minutehand.domain.scenario import (
    Absence,
    AbsenceTrigger,
    Answers,
    DelayRange,
    Helpfulness,
    Person,
    Scripted,
    ScriptedReply,
    Silent,
    WorkingHours,
)
from minutehand.domain.world import (
    Actor,
    Change,
    ControlKind,
    EntityKind,
    EntityRef,
    MessageAction,
    MessageSnapshot,
    Operation,
    WorldEvent,
)
from tests.model.fake_completions import FakeCompletions, Received, fake_completions
from tests.orchestrator.rig import T0, person, scenario

KEY = "sk-replier-test-key"
FACTS = ["The partner price is 40k a year.", "The contract renews in March."]
STALE = ["The partner price is 30k a year."]
DELAY = DelayRange(shortest=timedelta(hours=6), longest=timedelta(hours=66))
FRIDAY_1630 = datetime(2026, 8, 28, 16, 30, tzinfo=UTC)
QUESTION = "What is the partner price?"


def sofia(helpfulness: Helpfulness = Helpfulness.FULL, *, voice: str | None = None, model: str | None = None) -> Person:
    return Person(
        key="sofia",
        name="Sofia Romano",
        email="sofia@example.com",
        title="Head of Partnerships",
        facts=FACTS,
        stale_facts=STALE,
        reply=Answers(delay=DELAY, helpfulness=helpfulness, voice=voice, model=model, temperature=0.3),
    )


def answering(received: Received) -> WrittenReply:
    """The rule: a message ending in a question gets an answer; anything else needs none."""
    if received.last.rstrip().endswith("?"):
        return WrittenReply(replies=True, text="It is 40k a year.")
    return WrittenReply(replies=False, text=None)


def world(tmp_path: Path, at: datetime = T0) -> tuple[SqliteStore, RunClock]:
    clock = RunClock(at)
    tmp_path.mkdir(parents=True, exist_ok=True)
    return SqliteStore(tmp_path / "world.db", "r", clock), clock


def say(
    store: SqliteStore, actor: Actor, text: str, to: list[str], external_id: str, *, channel: str = "dm-sofia"
) -> WorldEvent:
    return store.apply(
        Change(
            entity=EntityRef(provider="testchat", kind=EntityKind.MESSAGE, external_id=external_id),
            operation=Operation.CREATE,
            actor=actor,
            body="{}",
            after=MessageSnapshot(text=text, channel=channel, recipient_emails=to),
        )
    )


def model(fake: FakeCompletions) -> OpenAICompatible:
    return OpenAICompatible(base_url=fake.base_url, api_key=KEY, model_id="people-1")


@pytest.mark.parametrize("helpfulness", list(Helpfulness))
async def test_each_helpfulness_reaches_the_model_and_the_reply_lands_when_a_scripted_one_would(
    tmp_path: Path, helpfulness: Helpfulness
) -> None:
    written = sofia(helpfulness, voice="terse")
    scripted = written.model_copy(
        update={"reply": Scripted(delay=DELAY, replies=[ScriptedReply(to_ask=1, text="scripted")])}
    )
    store, clock = world(tmp_path)
    asked = say(store, Actor.AGENT, QUESTION, [written.email], "m1")
    async with fake_completions(answering) as fake:
        reply = await ModelReplier(
            scenario(ticket_fates=[], people=[person("owner", Silent()), written]), model(fake)
        ).decide(written, asked, store.events(), clock)
    same = await ScriptedReplier(scenario(ticket_fates=[], people=[person("owner", Silent()), scripted])).decide(
        scripted, asked, store.events(), clock
    )

    assert reply is not None and same is not None
    assert reply.text == "It is 40k a year." and reply.in_reply_to == asked.entity
    assert reply.at == same.at and asked.sim_time + DELAY.shortest <= reply.at <= asked.sim_time + DELAY.longest
    assert reply.written_by == Provenance(model="people-1", prompt_version=PERSON_PROMPT_VERSION)
    [received] = fake.received
    system = received.system
    assert HELPFULNESS[helpfulness] in system
    assert all(other not in system for h, other in HELPFULNESS.items() if h is not helpfulness)
    assert "Sofia Romano, Head of Partnerships" in system and "terse" in system
    assert all(f in system for f in FACTS)
    assert (STALE[0] in system) is (helpfulness is Helpfulness.MISTAKEN)
    assert received.said[1:] == [("user", QUESTION)]
    assert received.temperature == 0.3 and received.schema_name == "WrittenReply"


async def test_a_message_that_needs_no_answer_gets_none(tmp_path: Path) -> None:
    who = sofia()
    store, clock = world(tmp_path)
    thanks = say(store, Actor.AGENT, "Thanks, that is all I needed.", [who.email], "m1")
    async with fake_completions(answering) as fake:
        reply = await ModelReplier(
            scenario(ticket_fates=[], people=[person("owner", Silent()), who]), model(fake)
        ).decide(who, thanks, store.events(), clock)
    assert reply is None and len(fake.received) == 1


async def test_a_person_asked_beyond_their_facts_is_told_to_say_they_do_not_know_and_given_nothing_else(
    tmp_path: Path,
) -> None:
    who = sofia()
    store, clock = world(tmp_path)
    asked = say(store, Actor.AGENT, "Who signs for legal, and what is the price?", [who.email], "m1")
    async with fake_completions(answering) as fake:
        await ModelReplier(scenario(ticket_fates=[], people=[person("owner", Silent()), who]), model(fake)).decide(
            who, asked, store.events(), clock
        )
    system = fake.received[0].system
    assert "You state nothing that is not in what you know. Asked something it does not cover" in system
    assert "you say you do not know" in system
    assert STALE[0] not in system and "what you believe" not in system


async def test_a_person_with_no_facts_is_told_they_know_nothing(tmp_path: Path) -> None:
    who = sofia().model_copy(update={"facts": []})
    store, clock = world(tmp_path)
    asked = say(store, Actor.AGENT, QUESTION, [who.email], "m1")
    async with fake_completions(answering) as fake:
        await ModelReplier(scenario(ticket_fates=[], people=[person("owner", Silent()), who]), model(fake)).decide(
            who, asked, store.events(), clock
        )
    assert "What you know:\n- nothing about this beyond what the messages themselves say" in fake.received[0].system


async def test_the_earlier_exchange_with_that_person_and_no_one_else_is_given_in_order(tmp_path: Path) -> None:
    who = sofia()
    store, clock = world(tmp_path)
    say(store, Actor.AGENT, "Hi Sofia, a question soon.", [who.email], "m1")
    say(store, Actor.AGENT, "Tom, unrelated.", ["tom@example.com"], "m2", channel="dm-tom")
    say(store, Actor.PERSON, "Sure, go ahead.", [], "m3")
    say(store, Actor.PERSON, "Tom here.", [], "m4", channel="dm-tom")
    say(store, Actor.PERSON, "Tom again, in a group with her.", [who.email], "m5")
    asked = say(store, Actor.AGENT, QUESTION, [who.email], "m6")
    async with fake_completions(answering) as fake:
        await ModelReplier(scenario(ticket_fates=[], people=[person("owner", Silent()), who]), model(fake)).decide(
            who, asked, store.events(), clock
        )
    # Hers: a PERSON message where the agent wrote to her, not addressed to her. Not hers: another
    # conversation, or a message addressed to her, which someone else wrote.
    assert fake.received[0].said[1:] == [
        ("user", "Hi Sofia, a question soon."),
        ("assistant", "Sure, go ahead."),
        ("user", QUESTION),
    ]


async def test_an_edited_message_is_put_to_the_model_as_it_reads_after_the_edit(tmp_path: Path) -> None:
    who = sofia()
    store, clock = world(tmp_path)
    say(store, Actor.AGENT, "Thinking...", [who.email], "m1")
    asked = store.apply(
        Change(
            entity=EntityRef(provider="testchat", kind=EntityKind.MESSAGE, external_id="m1"),
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            body="{}",
            after=MessageSnapshot(text=QUESTION, channel="dm-sofia", recipient_emails=[who.email]),
        )
    )
    async with fake_completions(answering) as fake:
        reply = await ModelReplier(
            scenario(ticket_fates=[], people=[person("owner", Silent()), who]), model(fake)
        ).decide(who, asked, store.events(), clock)
    assert fake.received[0].said[1:] == [("user", QUESTION)]
    assert reply is not None and reply.text == "It is 40k a year."


async def test_a_person_may_name_their_own_model(tmp_path: Path) -> None:
    who = sofia(model="people-large")
    store, clock = world(tmp_path)
    asked = say(store, Actor.AGENT, QUESTION, [who.email], "m1")
    async with fake_completions(answering) as fake:
        reply = await ModelReplier(
            scenario(ticket_fates=[], people=[person("owner", Silent()), who]), model(fake)
        ).decide(who, asked, store.events(), clock)
    assert fake.received[0].model == "people-large"
    assert reply is not None and reply.written_by is not None and reply.written_by.model == "people-large"


async def test_absence_and_working_hours_move_a_written_reply_as_they_move_a_scripted_one(tmp_path: Path) -> None:
    timing = {
        "working_hours": WorkingHours(timezone="Europe/Lisbon", opens=time(9), closes=time(17)),
        "absences": [
            Absence(trigger=AbsenceTrigger.ON_FIRST_ASK, starts_after=timedelta(hours=1), lasts=timedelta(days=3))
        ],
    }
    written = sofia().model_copy(update=timing)
    scripted = written.model_copy(
        update={"reply": Scripted(delay=DELAY, replies=[ScriptedReply(to_ask=1, text="scripted")])}
    )
    store, clock = world(tmp_path, FRIDAY_1630)
    asked = say(store, Actor.AGENT, QUESTION, [written.email], "m1")
    async with fake_completions(answering) as fake:
        reply = await ModelReplier(
            scenario(ticket_fates=[], people=[person("owner", Silent()), written]), model(fake)
        ).decide(written, asked, store.events(), clock)
    same = await ScriptedReplier(scenario(ticket_fates=[], people=[person("owner", Silent()), scripted])).decide(
        scripted, asked, store.events(), clock
    )
    assert reply is not None and same is not None
    assert reply.at == same.at >= FRIDAY_1630 + timedelta(days=3, hours=1)


async def test_an_answer_that_breaks_its_own_rule_is_sent_back_once(tmp_path: Path) -> None:
    who = sofia()
    store, clock = world(tmp_path)
    asked = say(store, Actor.AGENT, QUESTION, [who.email], "m1")

    def careless(received: Received) -> str | WrittenReply:
        if len(fake.received) == 1:
            return '{"replies": true, "text": null}'
        return WrittenReply(replies=True, text="40k.")

    async with fake_completions(careless) as fake:
        reply = await ModelReplier(
            scenario(ticket_fates=[], people=[person("owner", Silent()), who]), model(fake)
        ).decide(who, asked, store.events(), clock)
    assert reply is not None and reply.text == "40k."
    assert len(fake.received) == 2 and "replies is true, so text must be the reply" in fake.received[1].last


async def test_the_router_sends_scripted_people_to_the_script_and_written_ones_to_the_model(tmp_path: Path) -> None:
    written = sofia()
    tom = person("tom", Scripted(delay=DELAY, replies=[ScriptedReply(to_ask=1, text="From the script.")]))
    store, clock = world(tmp_path)
    to_tom = say(store, Actor.AGENT, QUESTION, [tom.email], "m1")
    to_sofia = say(store, Actor.AGENT, QUESTION, [written.email], "m2")
    async with fake_completions(answering) as fake:
        replier = PeopleReplier(
            scenario(ticket_fates=[], people=[person("owner", Silent()), written, tom]), model(fake)
        )
        from_tom = await replier.decide(tom, to_tom, store.events(), clock)
        assert fake.received == []
        from_sofia = await replier.decide(written, to_sofia, store.events(), clock)
    assert from_tom is not None and from_tom.text == "From the script." and from_tom.written_by is None
    assert from_sofia is not None and from_sofia.text == "It is 40k a year." and len(fake.received) == 1


def test_a_written_person_with_no_model_is_refused_by_name() -> None:
    with pytest.raises(RunRefused, match="sofia") as raised:
        PeopleReplier(scenario(ticket_fates=[], people=[person("owner", Silent()), sofia()]), None)
    assert "no model is configured" in str(raised.value)


def test_scripted_people_need_no_model() -> None:
    PeopleReplier(scenario(), None)


def card(store: SqliteStore, to: list[str]) -> WorldEvent:
    return store.apply(
        Change(
            entity=EntityRef(provider="testchat", kind=EntityKind.MESSAGE, external_id="card"),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            body="{}",
            after=MessageSnapshot(
                text="May I send the contract at 40k?",
                channel="dm-sofia",
                recipient_emails=to,
                actions=[
                    MessageAction(action_id="approve_op1", label="Accept", value="op1"),
                    MessageAction(action_id="reject_op1", label="Reject", value="op1"),
                    MessageAction(action_id="view_op1", label="Details", control=ControlKind.LINK),
                ],
            ),
        )
    )


async def test_the_person_is_shown_the_controls_and_a_press_with_a_reason_comes_back_as_one(tmp_path: Path) -> None:
    who = sofia()
    store, clock = world(tmp_path)
    asked = card(store, [who.email])

    def rejects(received: Received) -> WrittenReply:
        return WrittenReply(replies=True, text=None, press="Reject", form="The price is 45k now.")

    async with fake_completions(rejects) as fake:
        reply = await ModelReplier(
            scenario(ticket_fates=[], people=[person("owner", Silent()), who]), model(fake)
        ).decide(who, asked, store.events(), clock)

    assert reply is not None and reply.press is not None
    assert (reply.press.action_id, reply.press.value, [f.value for f in reply.press.form]) == (
        "reject_op1",
        "op1",
        ["The price is 45k now."],
    )
    assert reply.text == "The price is 45k now."
    [received] = fake.received
    assert received.last.endswith('[Controls: "Accept", "Reject"]'), "a link is not a control a person answers with"


async def test_a_press_the_message_does_not_carry_is_refused(tmp_path: Path) -> None:
    who = sofia()
    store, clock = world(tmp_path)
    asked = card(store, [who.email])

    def invents(received: Received) -> WrittenReply:
        return WrittenReply(replies=True, text=None, press="Approve all")

    async with fake_completions(invents) as fake:
        with pytest.raises(RunRefused, match="Approve all"):
            await ModelReplier(scenario(ticket_fates=[], people=[person("owner", Silent()), who]), model(fake)).decide(
                who, asked, store.events(), clock
            )
