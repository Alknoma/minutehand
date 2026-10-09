"""What a person does in Teams without the agent, pushed as Teams pushes it, and what the bot's validation refuses."""

from __future__ import annotations

from typing import Any

import pytest

from minutehand.adapters.providers.microsoft.inbound import DeliveryRefused, activity_token
from minutehand.adapters.providers.microsoft.state import APPS, AppRecord, app_ref
from minutehand.domain.people import PersonMessage, PersonReply, Press
from minutehand.domain.scenario import (
    PersonDeletes,
    PersonEdits,
    PersonJoins,
    PersonOpensAgent,
    PersonPosts,
    PersonReacts,
)
from minutehand.domain.world import Actor, EntityKind, EntityRef, MessageSnapshot, Operation
from tests.providers.microsoft.tenant import CONNECTOR, Bot, Intercepted, Tenant, bearer, token, validate


async def _happen(tenant: Tenant, bot: Bot, happening: Any) -> None:
    await tenant.provider.happen(happening, bot.target(), tenant.store, tenant.clock, secret="unused")


async def test_a_post_its_edit_its_reaction_and_its_delete_reach_the_bot(tenant: Tenant, bot: Bot) -> None:
    await _happen(tenant, bot, PersonPosts(provider="microsoft", person="sofia", text="Draft is up", key="draft"))
    await _happen(tenant, bot, PersonEdits(provider="microsoft", person="sofia", post="draft", text="Final is up"))
    await _happen(tenant, bot, PersonDeletes(provider="microsoft", person="sofia", post="draft"))
    pushed = bot.accepted()
    assert [(a["type"], a["channelData"].get("eventType")) for a in pushed] == [
        ("message", None),
        ("messageUpdate", "editMessage"),
        ("messageDelete", "softDeleteMessage"),
    ]
    assert pushed[1]["id"] == pushed[0]["id"] == pushed[2]["id"]
    written = [(e.actor, e.operation) for e in tenant.store.events() if e.entity.kind is EntityKind.MESSAGE]
    assert written == [
        (Actor.PERSON, Operation.CREATE),
        (Actor.PERSON, Operation.UPDATE),
        (Actor.PERSON, Operation.DELETE),
    ]
    texts = [
        e.after.text if isinstance(e.after, MessageSnapshot) else None
        for e in tenant.store.events()
        if e.entity.kind is EntityKind.MESSAGE
    ]
    assert texts == ["Draft is up", "Final is up", "Final is up"], "a deleted post still says what it said"


async def test_a_reaction_to_the_agents_message_and_a_member_joining_reach_the_bot(
    tenant: Tenant, microsoft: Intercepted, bot: Bot
) -> None:
    sofia = tenant.world.person("sofia")
    assert sofia is not None
    chat = tenant.world.personal_with(sofia.user.id, tenant.directory.tenant_id)
    assert chat is not None
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
        await http.post(
            f"{CONNECTOR}v3/conversations/{chat.id}/activities", json={"type": "message", "text": "Done?"}, headers=auth
        )
    await _happen(tenant, bot, PersonReacts(provider="microsoft", person="sofia", reaction="like"))
    reacted = bot.accepted()[-1]
    assert reacted["type"] == "messageReaction" and reacted["reactionsAdded"] == [{"type": "like"}]
    world = tenant.world
    general = world.conversation(tenant.directory.general_channel_id)
    assert general is not None
    world.write_conversation(
        general.model_copy(update={"members": [m for m in general.members if m != sofia.user.id]}),
        operation=Operation.UPDATE,
        actor=Actor.SCENARIO,
    )
    await _happen(tenant, bot, PersonJoins(provider="microsoft", person="sofia", channel="general"))
    joined = bot.accepted()[-1]
    assert joined["type"] == "conversationUpdate" and joined["membersAdded"][0]["aadObjectId"] == sofia.user.id


async def test_opening_the_agent_has_no_teams_form_and_is_refused(tenant: Tenant, bot: Bot) -> None:
    with pytest.raises(ValueError, match="no Teams form"):
        await _happen(tenant, bot, PersonOpensAgent(provider="microsoft", person="sofia"))


async def test_a_submit_button_arrives_as_a_message_with_its_value(
    tenant: Tenant, microsoft: Intercepted, bot: Bot
) -> None:
    sofia = tenant.world.person("sofia")
    assert sofia is not None
    chat = tenant.world.personal_with(sofia.user.id, tenant.directory.tenant_id)
    assert chat is not None
    card = {
        "type": "AdaptiveCard",
        "version": "1.2",
        "body": [{"type": "Input.ChoiceSet", "id": "assign_user_email", "choices": []}],
        "actions": [{"type": "Action.Submit", "title": "Assign", "id": "assign", "data": {"op": "9"}}],
    }
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
        sent = await http.post(
            f"{CONNECTOR}v3/conversations/{chat.id}/activities",
            json={
                "type": "message",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
            },
            headers=auth,
        )
    pressed = PersonReply(
        person="sofia",
        in_reply_to=EntityRef(provider="microsoft", kind=EntityKind.MESSAGE, external_id=sent.json()["id"]),
        text="Assign",
        at=tenant.clock.now(),
        press=Press(action_id="assign", label="Assign", picks="dania"),
    )
    await tenant.provider.press(pressed, bot.target(), tenant.store, tenant.clock, secret="unused")
    submitted = bot.accepted()[-1]
    dania = tenant.world.person("dania")
    assert dania is not None
    assert submitted["type"] == "message" and submitted["value"] == {"op": "9", "assign_user_email": dania.user.id}
    with pytest.raises(LookupError, match="no button"):
        await tenant.provider.press(
            pressed.model_copy(update={"press": Press(action_id="nothing", label="Nothing")}),
            bot.target(),
            tenant.store,
            tenant.clock,
            secret="unused",
        )


async def test_the_bots_validation_rejects_a_token_for_another_app_or_another_service_url(
    tenant: Tenant, microsoft: Intercepted, bot: Bot
) -> None:
    other = AppRecord(
        app_id="someone-elses-app", secret="s", display_name="Other", tenant_id=tenant.directory.tenant_id
    )
    refused = await validate(f"Bearer {activity_token(other)}", tenant.directory.bot_app_id, CONNECTOR, microsoft)
    assert refused is not None and "audience" in refused.lower()
    ours = activity_token(tenant.world.apps()[0])
    assert await validate(f"Bearer {ours}", tenant.directory.bot_app_id, CONNECTOR, microsoft) is None
    mismatched = await validate(f"Bearer {ours}", tenant.directory.bot_app_id, "https://evil.example.com/", microsoft)
    assert mismatched == "serviceurl does not match the activity"


async def test_a_push_the_bot_refuses_fails_the_delivery(tenant: Tenant, bot: Bot) -> None:
    tenant.world.write(
        app_ref(tenant.directory.bot_app_id),
        tenant.world.apps()[0].model_copy(update={"app_id": "renamed-app"}),
        operation=Operation.UPDATE,
        actor=Actor.SCENARIO,
        parent=APPS,
    )
    with pytest.raises(DeliveryRefused, match="401"):
        await tenant.provider.say(
            PersonMessage(person="sofia", text="hello", at=tenant.clock.now()),
            bot.target(),
            tenant.store,
            tenant.clock,
            secret="unused",
        )


def test_the_provider_holds_to_every_port_its_manifest_claims() -> None:
    from minutehand.adapters.providers.microsoft.manifest import MANIFEST
    from minutehand.adapters.providers.microsoft.provider import build
    from minutehand.application.conversations import LandsAnswers, PushesAnswers, PushesPresses
    from minutehand.ports.provider import PushesEvents
    from minutehand.ports.transitions import TalksToAgent

    provider = build()
    assert MANIFEST.pushes_events and isinstance(provider, PushesEvents) and isinstance(provider, TalksToAgent)
    assert isinstance(provider, PushesAnswers | PushesPresses) and isinstance(provider, LandsAnswers)
