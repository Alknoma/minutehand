"""People's answers to the agent's messages as transitions (`docs/design-transitions.md`, phase 3): each message the
agent sent a person is an ask they answer, by writing back or using a control, through the provider's own port."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.conversations import PushedConversations
from minutehand.application.refusals import RunRefused
from minutehand.application.run_clock import RunClock
from minutehand.checks.ledger import build
from minutehand.domain.checks import ObligationKind
from minutehand.domain.common import Window
from minutehand.domain.people import InboundTarget, PersonMessage, PersonReply
from minutehand.domain.scenario import Absence, MessagingHappening, Reminded, Take
from minutehand.domain.transitions import AWAITING, REPLIED, REPLY, conversations, message_offers
from minutehand.domain.world import (
    Actor,
    Change,
    ControlKind,
    EntityKind,
    EntityRef,
    MessageAction,
    MessageSnapshot,
    Operation,
    PendingStatus,
    TransitionSnapshot,
)
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store
from tests.orchestrator.rig import T0, scenario
from tests.support.people import people_engine

CHAT = "chat"
SOFIA = "sofia@example.com"
TARGET = InboundTarget(provider=CHAT, url="http://agent.test/pushed")
CONTROLS = [
    MessageAction(action_id="approve_btn", label="Approve", value="yes"),
    MessageAction(action_id="pick_owner", label="Pick an owner", control=ControlKind.USER_SELECT),
    MessageAction(action_id="open_doc", label="Open the doc", control=ControlKind.LINK),
]


class Pushes:
    """`PushesEvents` and `PushesInteractions` for a chat service: what it pushed to the agent, in order."""

    def __init__(self) -> None:
        self.delivered: list[PersonReply] = []
        self.pressed: list[PersonReply] = []

    async def deliver(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        self.delivered.append(reply)

    async def press(
        self, reply: PersonReply, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        self.pressed.append(reply)

    async def say(
        self, message: PersonMessage, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        del message

    async def happen(
        self, happening: MessagingHappening, target: InboundTarget, world: Store, clock: Clock, *, secret: str
    ) -> None:
        del happening


def _store(tmp_path: Path) -> tuple[SqliteStore, RunClock]:
    clock = RunClock(T0)
    return SqliteStore(tmp_path / "w.db", "w", clock), clock


def _sent(
    store: Store,
    key: str,
    text: str,
    *,
    to: str = SOFIA,
    actor: Actor = Actor.AGENT,
    answerable: bool = True,
    actions: list[MessageAction] | None = None,
    operation: Operation = Operation.CREATE,
    channel: str = "dm",
) -> EntityRef:
    ref = EntityRef(provider=CHAT, kind=EntityKind.MESSAGE, external_id=key)
    snapshot = MessageSnapshot(
        text=text, channel=channel, recipient_emails=[to], answerable=answerable, actions=actions or []
    )
    store.apply(
        Change(
            entity=ref,
            operation=operation,
            actor=actor,
            body=snapshot.model_dump_json(),
            after=None if operation is Operation.DELETE else snapshot,
        )
    )
    return ref


def test_each_message_the_agent_sent_a_person_they_can_answer_is_an_ask_and_nothing_else_is(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    asked = _sent(store, "m1", "Can you confirm the pricing?")
    _sent(store, "m2", "A note nobody can answer", answerable=False)
    _sent(store, "m3", "Thanks, Sofia", actor=Actor.PERSON)
    _sent(store, "m4", "For Tom only", to="tom@example.com")
    gone = _sent(store, "m5", "Sent by mistake")
    _sent(store, "m5", "Sent by mistake", operation=Operation.DELETE)
    del gone

    found = conversations(SOFIA, CHAT, store.events())

    # Mutations: dropping `answerable` adds m2; dropping the recipient check adds m4; dropping the actor check adds
    # m3; dropping the delete check adds m5.
    assert [(w.item, w.state, w.conversation) for w in found] == [(asked, AWAITING, True)]


def test_a_message_offers_a_reply_and_each_control_by_its_id_and_never_a_link() -> None:
    offers = message_offers(MessageSnapshot(text="Approve the booking?", channel="dm", actions=CONTROLS))

    # Mutations: naming a control by its label, or offering a link, changes the names.
    assert [(o.name, o.to_state) for o in offers] == [
        (REPLY, REPLIED),
        ("approve_btn", "yes"),
        ("pick_owner", "pick_owner"),
    ]
    assert [f.name for f in offers[2].fields if f.required] == ["picks"], "a picker needs the person picked"


async def test_a_control_used_is_pushed_as_the_interaction_with_the_person_picked_and_the_form(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    asked = _sent(store, "m1", "Who owns the booking?", actions=CONTROLS)
    pushes = Pushes()
    port = PushedConversations(CHAT, pushes, TARGET, "secret")
    sofia = scenario().people[1]
    form = json.dumps([{"input_id": "note", "value": "Tom has the budget"}])

    moved = await port.apply(
        asked, "pick_owner", Actor.PERSON, sofia, json.dumps({"picks": "tom", "form": form}), store, clock
    )

    # Mutation: delivering a control as words pushes a message and no interaction.
    assert pushes.delivered == []
    [pressed] = pushes.pressed
    assert pressed.press is not None
    assert (pressed.press.action_id, pressed.press.label, pressed.press.picks) == ("pick_owner", "Pick an owner", "tom")
    assert [f.value for f in pressed.press.form] == ["Tom has the budget"] and pressed.text == "Tom has the budget"
    [kept] = [e.after for e in store.events() if isinstance(e.after, TransitionSnapshot)]
    assert (moved.name, moved.from_state, moved.to_state, moved.who) == ("pick_owner", AWAITING, REPLIED, "sofia")
    assert (kept.name, kept.who) == ("pick_owner", "sofia")


async def test_an_answer_to_an_agent_with_no_inbound_target_on_the_service_is_refused(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    asked = _sent(store, "m1", "Can you confirm the pricing?")
    port = PushedConversations(CHAT, Pushes(), None, None)

    # Mutation: pushing anyway reaches no agent and records an answer nobody heard.
    with pytest.raises(RunRefused, match="the agent declares no inbound target there"):
        await port.apply(asked, REPLY, Actor.PERSON, scenario().people[1], '{"text": "Yes"}', store, clock)
    assert not port.heard_of(asked, None, store, clock)


async def test_an_answer_the_message_does_not_offer_is_refused(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    asked = _sent(store, "m1", "Approve the booking?", actions=CONTROLS)
    port = PushedConversations(CHAT, Pushes(), TARGET, "secret")

    # Mutation: matching a control by its label lets the link's id through.
    with pytest.raises(ValueError, match="offers no 'open_doc'"):
        await port.apply(asked, "open_doc", Actor.PERSON, scenario().people[1], "{}", store, clock)
    with pytest.raises(ValueError, match="takes no"):
        await port.apply(asked, REPLY, Actor.PERSON, scenario().people[1], '{"txt": "Yes"}', store, clock)


async def test_a_scripted_answer_is_worded_when_planned_and_lands_at_its_moment_as_said(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    asked = _sent(store, "m1", "Can you confirm the pricing?")
    pushes = Pushes()
    scn = scenario(tom_finishes=False)
    engine = people_engine(scn, {CHAT: PushedConversations(CHAT, pushes, TARGET, "secret")}, None)

    [booked] = (await engine.look(store, clock)).booked
    held = engine.pending(booked.pending, store)

    # Planned and worded as the ask is first seen; nothing said yet. Mutation: an `_ask` that writes no words
    # leaves `answer` empty until the moment.
    assert booked.conversation and booked.at == T0 + timedelta(hours=36)
    assert held.answer is not None and PersonReply.model_validate_json(held.answer).text == "Yes, 40k."
    assert pushes.delivered == [] and store.replies() == []
    assert (await engine.look(store, clock)).booked == [], "an ask held is not booked twice"

    clock.jump(T0 + timedelta(hours=36))
    acted = await engine.act(booked.pending, store, clock)

    assert acted.transition is not None and acted.transition.name == REPLY
    assert [r.text for r in pushes.delivered] == ["Yes, 40k."]
    [said] = store.replies()
    assert (said.text, said.at, said.in_reply_to) == ("Yes, 40k.", T0 + timedelta(hours=36), asked)
    assert engine.pending(booked.pending, store).status is PendingStatus.ACTED


async def test_an_ask_edited_before_its_answer_is_worded_again_from_its_new_text(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    asked = _sent(store, "m1", "Thinking...")
    scn = scenario(tom_finishes=False)
    engine = people_engine(scn, {CHAT: PushedConversations(CHAT, Pushes(), TARGET, "secret")}, None)
    [booked] = (await engine.look(store, clock)).booked
    first = engine.pending(booked.pending, store)

    clock.jump(T0 + timedelta(hours=1))
    _sent(store, "m1", "Can you confirm the pricing?", operation=Operation.UPDATE)
    looked = await engine.look(store, clock)

    # Mutation: an engine blind to edits keeps the plan made from the placeholder, measured from T0.
    [again] = looked.moved
    assert again.pending == booked.pending and again.at == T0 + timedelta(hours=37)
    now = engine.pending(booked.pending, store)
    assert now.asked is not None and first.asked is not None and now.asked > first.asked
    assert asked == now.item and looked.booked == []


async def test_a_follow_up_on_an_answer_owed_is_no_new_ask_and_a_reminded_person_answers_sooner(
    tmp_path: Path,
) -> None:
    store, clock = _store(tmp_path)
    asked = _sent(store, "m1", "Can you confirm the pricing?")
    reminded = Reminded(sooner_within=Window(min=timedelta(hours=1), max=timedelta(hours=1)))
    base = scenario(tom_finishes=False)
    people = [p.model_copy(update={"reminded": reminded}) if p.key == "sofia" else p for p in base.people]
    engine = people_engine(
        base.model_copy(update={"people": people}), {CHAT: PushedConversations(CHAT, Pushes(), TARGET, "secret")}, None
    )
    [booked] = (await engine.look(store, clock)).booked
    assert booked.at == T0 + timedelta(hours=36)

    clock.jump(T0 + timedelta(hours=2))
    _sent(store, "m2", "Just checking in on the pricing")
    looked = await engine.look(store, clock)

    # Mutations: a follow-up planned as an ask of its own is booked; one that never moves the answer leaves it at
    # T0+36h.
    assert looked.booked == []
    [sooner] = looked.moved
    assert (sooner.pending, sooner.at) == (booked.pending, T0 + timedelta(hours=3))
    held = engine.pending(booked.pending, store)
    assert (held.item, held.follow_ups) == (asked, ["m2"])


async def test_a_person_away_while_someone_covers_sends_their_automatic_reply_at_once_and_still_owes_the_answer(
    tmp_path: Path,
) -> None:
    store, clock = _store(tmp_path)
    _sent(store, "m1", "Can you confirm the pricing?")
    away = [Absence(lasts=timedelta(days=3), delegate="tom", reason="on leave")]
    base = scenario(tom_finishes=False)
    people = [p.model_copy(update={"absences": away}) if p.key == "sofia" else p for p in base.people]
    pushes = Pushes()
    engine = people_engine(
        base.model_copy(update={"people": people}), {CHAT: PushedConversations(CHAT, pushes, TARGET, "secret")}, None
    )

    automatic, answer = sorted((await engine.look(store, clock)).booked, key=lambda b: b.at or T0)

    # Mutation: an automatic reply sent as the ask is looked at, outside the run's table, is pushed before any wake.
    assert automatic.at == T0 and pushes.delivered == []
    assert answer.at is not None and answer.at >= T0 + timedelta(days=3), "her answer waits until she is back"
    await engine.act(automatic.pending, store, clock)
    [said] = pushes.delivered
    assert said.text.startswith("Automatic reply: Sofia is away (on leave)") and not said.answers
    assert engine.pending(answer.pending, store).status is PendingStatus.PENDING, "the ask is still hers to answer"


async def test_a_take_pinned_on_an_ask_still_counts_it_first_beside_an_automatic_reply(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    asked = _sent(store, "m1", "Can you confirm the pricing?")
    away = [Absence(lasts=timedelta(days=3), delegate="tom", reason="on leave")]
    take = Take(provider=CHAT, take=REPLY, nth=1, after=timedelta(days=4), verbatim="Back now: yes, 40k.")
    base = scenario(tom_finishes=False)
    people = [p.model_copy(update={"absences": away, "takes": [take]}) if p.key == "sofia" else p for p in base.people]
    pushes = Pushes()
    engine = people_engine(
        base.model_copy(update={"people": people}), {CHAT: PushedConversations(CHAT, pushes, TARGET, "secret")}, None
    )

    booked = (await engine.look(store, clock)).booked

    # Mutation: counting the automatic reply's record as an ask makes this the second, and the take pins nothing.
    assert [(b.item, b.at) for b in booked] == [(asked, T0), (asked, T0 + timedelta(days=4))]
    assert engine.pending(booked[1].pending, store).take == REPLY


def test_an_ask_the_script_plans_no_answer_to_opens_no_wait_and_one_it_does_opens_one(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    first = _sent(store, "m1", "Can you confirm the pricing?")
    _sent(store, "m2", "Thanks for the help last week", channel="team")  # a conversation of its own
    scn = scenario(tom_finishes=False)
    engine = people_engine(scn, {CHAT: PushedConversations(CHAT, Pushes(), TARGET, "secret")}, None)
    asyncio.run(engine.look(store, clock))

    waits = [o for o in build(scn, store.events(), store.replies()) if o.kind is ObligationKind.ANSWER_FROM_PERSON]

    # Her script answers one ask, then goes silent: the second asks her nothing. Mutation: reading every record the
    # engine holds as an ask opens a wait on the thank-you too.
    assert [o.entity for o in waits] == [first]
