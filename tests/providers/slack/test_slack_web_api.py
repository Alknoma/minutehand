"""The Web API over the store: what the agent writes reads back, on the run's clock, recorded for the checks."""

from __future__ import annotations

from datetime import timedelta

import httpx

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.providers.slack.state import BOT_USER_ID
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation
from minutehand.ports.provider import Provider, PushesEvents
from tests.providers.slack.slack_workspace import (
    GENERAL,
    START,
    TOKEN,
    Workspace,
    body,
    form,
    messages_of,
    text_of,
)


def test_the_provider_is_a_provider_that_pushes_events() -> None:
    provider = build()
    as_provider: Provider = provider
    as_pusher: PushesEvents = provider
    assert as_provider.manifest.key == "slack" and as_pusher is provider


async def test_a_posted_message_reads_back_in_history(client: httpx.AsyncClient) -> None:
    posted = await body(client, "chat.postMessage", channel=GENERAL, text="checklist is up")
    assert posted["ok"] is True
    await form(client, "chat.postMessage", channel=GENERAL, text="sent as a form")

    history = await form(client, "conversations.history", channel=GENERAL)

    assert text_of(history) == ["sent as a form", "checklist is up"]
    assert messages_of(history)[1]["ts"] == posted["ts"]


async def test_query_string_arguments_are_read_on_a_get(client: httpx.AsyncClient) -> None:
    await body(client, "chat.postMessage", channel=GENERAL, text="by query")
    response = await client.get(f"/api/conversations.history?channel={GENERAL}&token={TOKEN}")
    assert text_of(response.json()) == ["by query"]


async def test_ts_is_the_clock_and_unique_inside_one_instant(workspace: Workspace, client: httpx.AsyncClient) -> None:
    first = await body(client, "chat.postMessage", channel=GENERAL, text="one")
    second = await body(client, "chat.postMessage", channel=GENERAL, text="two")
    workspace.clock.jump(START + timedelta(days=3))
    third = await body(client, "chat.postMessage", channel=GENERAL, text="three")

    stamps = [str(m["ts"]) for m in (first, second, third)]
    assert [int(s.split(".")[0]) for s in stamps] == [int(START.timestamp())] * 2 + [
        int((START + timedelta(days=3)).timestamp())
    ]
    assert len(set(stamps)) == 3
    assert [float(s) for s in stamps] == sorted(float(s) for s in stamps)


async def test_a_dm_records_the_recipient_email_on_its_snapshot(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    opened = await form(client, "conversations.open", users=state.user_id("tomas"))
    channel = opened["channel"]
    assert isinstance(channel, dict)
    posted = await body(client, "chat.postMessage", channel=channel["id"], text="can you sign off today?")

    event = workspace.store.events()[-1]
    stored = workspace.store.get(event.entity)

    assert event.actor is Actor.AGENT and event.operation is Operation.CREATE
    assert event.entity.kind is EntityKind.MESSAGE and event.entity.external_id == posted["ts"]
    assert event.after == MessageSnapshot(
        text="can you sign off today?", channel=workspace.dm("tomas"), recipient_emails=["tomas@example.com"]
    )
    assert stored is not None and stored.parent == workspace.dm("tomas")


async def test_a_channel_message_is_addressed_to_every_human_in_it(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    await body(client, "chat.postMessage", channel=GENERAL, text="all hands")
    after = workspace.store.events()[-1].after
    assert isinstance(after, MessageSnapshot)
    assert sorted(after.recipient_emails) == ["iris@example.com", "noor@example.com", "tomas@example.com"]


async def test_a_blocks_only_message_is_recorded_with_the_text_its_reader_sees(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "sign-off needed"}}]
    await body(client, "chat.postMessage", channel=GENERAL, blocks=blocks)
    after = workspace.store.events()[-1].after
    assert isinstance(after, MessageSnapshot) and after.text == "sign-off needed"


async def test_reads_are_recorded_as_reads_and_listings_as_searches(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    before = workspace.store.head()
    await form(client, "conversations.history", channel=GENERAL)
    await form(client, "users.info", user=state.user_id("iris"))
    await form(client, "users.list")
    await form(client, "users.lookupByEmail", email="noor@example.com")

    events = workspace.store.events(since=before)
    assert [(e.actor, e.operation, e.entity.external_id) for e in events] == [
        (Actor.AGENT, Operation.READ, GENERAL),
        (Actor.AGENT, Operation.READ, state.user_id("iris")),
        (Actor.AGENT, Operation.SEARCH, state.TEAM_ID),
        (Actor.AGENT, Operation.SEARCH, state.TEAM_ID),
    ]


async def test_an_edit_and_a_delete_are_agent_writes(workspace: Workspace, client: httpx.AsyncClient) -> None:
    posted = await body(client, "chat.postMessage", channel=GENERAL, text="draft")
    updated = await body(client, "chat.update", channel=GENERAL, ts=posted["ts"], text="final")
    assert updated["text"] == "final"
    assert text_of(await form(client, "conversations.history", channel=GENERAL)) == ["final"]
    edit = workspace.store.events()[-2]
    assert edit.operation is Operation.UPDATE and isinstance(edit.after, MessageSnapshot) and edit.after.text == "final"

    deleted = await form(client, "chat.delete", channel=GENERAL, ts=str(posted["ts"]))
    assert deleted["ok"] is True
    assert text_of(await form(client, "conversations.history", channel=GENERAL)) == []
    assert workspace.store.events()[-2].operation is Operation.DELETE


async def test_a_reaction_lands_on_the_message(client: httpx.AsyncClient) -> None:
    posted = await body(client, "chat.postMessage", channel=GENERAL, text="react to me")
    assert (await form(client, "reactions.add", channel=GENERAL, timestamp=str(posted["ts"]), name="eyes"))["ok"]
    again = await form(client, "reactions.add", channel=GENERAL, timestamp=str(posted["ts"]), name="eyes")
    assert again == {"ok": False, "error": "already_reacted"}
    assert messages_of(await form(client, "conversations.history", channel=GENERAL))[0]["reactions"] == [
        {"name": "eyes", "users": [BOT_USER_ID], "count": 1}
    ]


async def test_auth_test_names_the_app_and_its_workspace(client: httpx.AsyncClient) -> None:
    answer = await form(client, "auth.test")
    assert answer["ok"] is True
    assert (answer["user_id"], answer["team_id"], answer["bot_id"]) == (BOT_USER_ID, state.TEAM_ID, state.BOT_ID)


async def test_a_token_in_the_form_body_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/auth.test", data={"token": "xoxp-a-user-token"})
    assert response.json()["ok"] is True


async def test_lookup_by_email_finds_the_seeded_person(client: httpx.AsyncClient) -> None:
    answer = await form(client, "users.lookupByEmail", email="tomas@example.com")
    user = answer["user"]
    assert isinstance(user, dict) and user["id"] == state.user_id("tomas") and user["tz"] == "Europe/Lisbon"
    assert await form(client, "users.lookupByEmail", email="nobody@example.com") == {
        "ok": False,
        "error": "users_not_found",
    }
