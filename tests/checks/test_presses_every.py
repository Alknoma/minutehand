"""`Scripted.presses_every`: a person presses the control with that label on every message the agent sends them
that carries one, however many there are, and nothing on a message without it; a scripted reply to an ask wins."""

from __future__ import annotations

from minutehand.application.replier_scripted import ScriptedReplier
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import DelayRange, Scripted, ScriptedPress, ScriptedReply
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    MessageAction,
    MessageSnapshot,
    Operation,
    WorldEvent,
)
from tests.checks.world import START, Log, at, person, scenario

QUICK = DelayRange(shortest=at(1) - START, longest=at(1) - START)


def _card(log: Log, owner_email: str, hours: float, *, approvable: bool) -> WorldEvent:
    ref = EntityRef(provider="chat", kind=EntityKind.MESSAGE, external_id=f"m{len(log.events) + 1}")
    actions = [MessageAction(action_id="approve", label="Approve", value="op-1")] if approvable else []
    snapshot = MessageSnapshot(text="Approve sending the summary?", channel="dm", recipient_emails=[owner_email],
                               actions=actions)  # fmt: skip
    return log._add(hours, Actor.AGENT, Operation.CREATE, ref, snapshot, wake=1)


async def test_every_approval_card_is_pressed_and_a_message_without_one_is_not() -> None:
    owner = person("owner", Scripted(delay=QUICK, replies=[], presses_every=ScriptedPress(label="approve")))
    replier = ScriptedReplier(scenario(owner))
    log = Log()
    cards = [_card(log, owner.email, h, approvable=True) for h in (1, 2, 3)]
    plain = _card(log, owner.email, 4, approvable=False)
    for card in cards:
        reply = await replier.decide(owner, card, log.events, RunClock(START))
        assert reply is not None and reply.press is not None and reply.press.action_id == "approve"
        assert reply.press.value == "op-1" and reply.at == card.sim_time + QUICK.shortest
    assert await replier.decide(owner, plain, log.events, RunClock(START)) is None


async def test_a_scripted_reply_to_an_ask_is_used_before_pressing_every_card() -> None:
    script = Scripted(delay=QUICK, replies=[ScriptedReply(to_ask=2, text="Hold on, not this one.")],
                      presses_every=ScriptedPress(label="Approve"))  # fmt: skip
    owner = person("owner", script)
    log = Log()
    first, second = (_card(log, owner.email, h, approvable=True) for h in (1, 2))
    replier = ScriptedReplier(scenario(owner))
    pressed = await replier.decide(owner, first, log.events, RunClock(START))
    written = await replier.decide(owner, second, log.events, RunClock(START))
    assert pressed is not None and pressed.press is not None
    assert written is not None and written.press is None and written.text == "Hold on, not this one."
