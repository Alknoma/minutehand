"""A bot's whole life in Teams, through the proxy: installed, messaged, answering with an Adaptive Card through the
connector, its button pressed, the card updated. Every activity reaches the bot's endpoint signed so that its own
validation accepts it, and the world log names who did each step."""

from __future__ import annotations

import json
from typing import Any

from minutehand.adapters.providers.microsoft import cards
from minutehand.domain.people import PersonMessage, PersonReply, Press
from minutehand.domain.scenario import FormInput, PersonAddsAgent
from minutehand.domain.world import (
    Actor,
    EntityKind,
    EntityRef,
    InteractionSnapshot,
    MessageSnapshot,
    Operation,
)
from tests.providers.microsoft.tenant import CONNECTOR, Bot, Intercepted, Tenant, bearer, token

CARD: dict[str, Any] = {
    "type": "AdaptiveCard",
    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
    "version": "1.5",
    "body": [
        {"type": "TextBlock", "text": "Approve the vendor shortlist?"},
        {"type": "Input.Text", "id": "rejection_reason", "label": "Why not?"},
    ],
    "actions": [
        {"type": "Action.Execute", "title": "Approve", "verb": "approve_operation", "data": {"operation": "op-7"}},
        {"type": "Action.Execute", "title": "Reject", "verb": "reject_operation", "data": {"operation": "op-7"}},
    ],
}
UPDATED: dict[str, Any] = {
    "type": "AdaptiveCard",
    "version": "1.5",
    "body": [{"type": "TextBlock", "text": "Approved by Sofia."}],
}


async def test_install_message_card_press_and_update_round_trip(
    tenant: Tenant, microsoft: Intercepted, bot: Bot
) -> None:
    sofia = tenant.world.person("sofia")
    assert sofia is not None
    chat = tenant.world.personal_with(sofia.user.id, tenant.directory.tenant_id)
    assert chat is not None
    # Start from a chat the bot is not in yet, so the install is what gives it the conversation.
    tenant.world.write_conversation(
        chat.model_copy(update={"bot_installed": False}), operation=Operation.UPDATE, actor=Actor.SCENARIO
    )
    await tenant.provider.happen(
        PersonAddsAgent(provider="microsoft", person="sofia"), bot.target(), tenant.store, tenant.clock, secret="unused"
    )
    installed = bot.accepted()
    assert [a["type"] for a in installed] == ["installationUpdate", "conversationUpdate"]
    reference = installed[1]
    assert reference["conversation"]["id"] == chat.id
    assert reference["serviceUrl"] == CONNECTOR
    assert reference["membersAdded"][0]["id"] == f"28:{tenant.directory.bot_app_id}"

    await tenant.provider.say(
        PersonMessage(person="sofia", text="Is the shortlist ready?", at=tenant.clock.now()),
        bot.target(),
        tenant.store,
        tenant.clock,
        secret="unused",
    )
    message = bot.accepted()[-1]
    assert (message["type"], message["text"]) == ("message", "Is the shortlist ready?")
    assert message["from"]["aadObjectId"] == sofia.user.id
    assert message["channelData"]["tenant"]["id"] == tenant.directory.tenant_id
    assert all(r.accepted for r in bot.received), [r.reason for r in bot.received]

    async with microsoft.http() as http:
        connector = await token(http, tenant, "https://api.botframework.com/.default")
        sent = await http.post(
            f"{message['serviceUrl']}v3/conversations/{message['conversation']['id']}/activities",
            json={"type": "message", "attachments": [{"contentType": cards.wire.ADAPTIVE_CARD, "content": CARD}]},
            headers=bearer(connector),
        )
        assert sent.status_code == 201, sent.text
        card_id = sent.json()["id"]

        card_event = [e for e in tenant.store.events() if e.entity.external_id == card_id][-1]
        assert isinstance(card_event.after, MessageSnapshot)
        assert [a.label for a in card_event.after.actions] == ["Approve", "Reject"]
        assert card_event.after.recipient_emails == ["sofia@example.com"]

        def answer(activity: dict[str, Any]) -> dict[str, Any]:
            return {"statusCode": 200, "type": cards.wire.ADAPTIVE_CARD, "value": UPDATED}

        bot.answer = answer
        await tenant.provider.press(
            PersonReply(
                person="sofia",
                in_reply_to=EntityRef(provider="microsoft", kind=EntityKind.MESSAGE, external_id=card_id),
                text="Reject",
                at=tenant.clock.now(),
                press=Press(action_id="reject_operation", label="Reject", form=[FormInput(value="Too expensive")]),
            ),
            bot.target(),
            tenant.store,
            tenant.clock,
            secret="unused",
        )
        invoke = bot.accepted()[-1]
        assert (invoke["type"], invoke["name"]) == ("invoke", "adaptiveCard/action")
        assert invoke["value"]["action"]["verb"] == "reject_operation"
        assert invoke["value"]["action"]["data"] == {"operation": "op-7", "rejection_reason": "Too expensive"}
        assert invoke["replyToId"] == card_id

        redrawn = await http.put(
            f"{CONNECTOR}v3/conversations/{chat.id}/activities/{card_id}",
            json={"type": "message", "attachments": [{"contentType": cards.wire.ADAPTIVE_CARD, "content": UPDATED}]},
            headers=bearer(connector),
        )
        assert redrawn.status_code == 200, redrawn.text

    steps = [
        (e.actor, e.operation, type(e.after).__name__)
        for e in tenant.store.events()
        if e.entity.provider == "microsoft" and e.operation is not Operation.READ and e.actor is not Actor.SCENARIO
    ]
    assert steps == [
        (Actor.PERSON, Operation.UPDATE, "NoneType"),  # the install
        (Actor.PERSON, Operation.CREATE, "MessageSnapshot"),  # Sofia's message
        (Actor.AGENT, Operation.CREATE, "MessageSnapshot"),  # the card
        (Actor.PERSON, Operation.CREATE, "InteractionSnapshot"),  # the press
        (Actor.AGENT, Operation.UPDATE, "NoneType"),  # the bot's answer to the invoke
        (Actor.AGENT, Operation.UPDATE, "MessageSnapshot"),  # the card redrawn from the invoke answer
        (Actor.AGENT, Operation.UPDATE, "MessageSnapshot"),  # the bot's own update-activity call
    ]
    pressed = next(e.after for e in tenant.store.events() if isinstance(e.after, InteractionSnapshot))
    assert isinstance(pressed, InteractionSnapshot)
    assert (pressed.person, pressed.label, pressed.form) == ("sofia", "Reject", ["Too expensive"])
    final = tenant.world.message(card_id)
    assert final is not None
    assert json.dumps(final[1].attachments[0].content if final[1].attachments else None) == json.dumps(UPDATED)
