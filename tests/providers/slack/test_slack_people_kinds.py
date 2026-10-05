"""Every kind of Slack member a service meets, through stock `slack_sdk`: one whose profile carries no email,
Slackbot, guests of both kinds, another app's bot, and a deactivated member."""

from __future__ import annotations

import json
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, MessageSnapshot
from tests.providers.slack.intercepted import data

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
TOKEN = "xoxb-people-kinds"

SCENARIO = Scenario.model_validate(
    {
        "name": "people_kinds",
        "goal": "-",
        "owner": "owen",
        "starts_at": START.isoformat(),
        "people": [
            {"key": "owen", "name": "Owen Owner", "email": "owen@example.com"},
            {"key": "hidden", "name": "Hana Hidden", "email": "hana@example.com"},
            {"key": "guest", "name": "Gus Guest", "email": "gus@partner.example", "account": "guest"},
            {"key": "single", "name": "Sid Single", "email": "sid@partner.example", "account": "guest"},
            {"key": "helper", "name": "Helper Bot", "email": "helper@bots.example", "account": "bot"},
            {"key": "gone", "name": "Gone Person", "email": "gone@example.com", "account": "deactivated"},
        ],
        "provider_seeds": [
            {
                "provider": "slack",
                "body": json.dumps({"without_email": ["hidden"], "single_channel_guests": ["single"]}),
            }
        ],
    }
)


@pytest.fixture
async def sdk(tmp_path: Path) -> AsyncIterator[tuple[AsyncWebClient, SqliteStore]]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "people-kinds", clock)
    provider = build()
    provider.seed(SCENARIO, store)
    proxy = Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca")
    async with proxy:
        proxy.mount(store, clock, {"slack": provider.app(store, clock)})
        trust = ssl.create_default_context(cafile=str(proxy.ca_bundle))
        yield AsyncWebClient(token=TOKEN, proxy=proxy.url, ssl=trust), store
    store.close()


async def _members(sdk: AsyncWebClient) -> dict[str, dict[str, Any]]:
    return {m["name"]: m for m in data(await sdk.users_list())["members"]}


async def test_a_member_declared_without_email_is_listed_with_no_email_and_not_found_by_it(
    sdk: tuple[AsyncWebClient, SqliteStore],
) -> None:
    """DOCUMENTED: a profile's `email` is served only with `users:read.email`, and a member may have none.
    https://docs.slack.dev/reference/objects/user-object"""
    client, _ = sdk
    hidden = (await _members(client))["hidden"]
    assert "email" not in hidden["profile"]
    info = data(await client.users_info(user=hidden["id"]))["user"]
    assert "email" not in info["profile"] and info["real_name"] == "Hana Hidden"
    with pytest.raises(SlackApiError) as refused:
        await client.users_lookupByEmail(email="hana@example.com")
    assert refused.value.response["error"] == "users_not_found"
    assert data(await client.users_lookupByEmail(email="owen@example.com"))["user"]["name"] == "owen"


async def test_a_message_to_a_member_without_email_still_reaches_them_in_the_world(
    sdk: tuple[AsyncWebClient, SqliteStore],
) -> None:
    client, store = sdk
    hidden = (await _members(client))["hidden"]["id"]
    channel = data(await client.conversations_open(users=[hidden]))["channel"]["id"]
    await client.chat_postMessage(channel=channel, text="Are you free Thursday?")
    sent = [e for e in store.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)]
    assert sent and isinstance(sent[-1].after, MessageSnapshot)
    assert sent[-1].after.recipient_emails == ["hana@example.com"]


async def test_slackbot_is_listed_and_answered_as_slack_serves_it(sdk: tuple[AsyncWebClient, SqliteStore]) -> None:
    """DOCUMENTED: "Slackbot is special, so `is_bot` will be false for it."
    https://docs.slack.dev/reference/objects/user-object. OBSERVED: its id is `USLACKBOT` in every workspace,
    `users.list` lists it, and its profile carries no email."""
    client, _ = sdk
    slackbot = (await _members(client))["slackbot"]
    assert slackbot["id"] == state.SLACKBOT_ID and slackbot["is_bot"] is False and slackbot["deleted"] is False
    assert "email" not in slackbot["profile"] and slackbot["real_name"] == "Slackbot"
    info = data(await client.users_info(user="USLACKBOT"))["user"]
    assert info["id"] == "USLACKBOT" and info["is_bot"] is False


async def test_guests_bots_and_deactivated_members_carry_their_own_flags(
    sdk: tuple[AsyncWebClient, SqliteStore],
) -> None:
    """DOCUMENTED: `is_restricted` is a guest, `is_ultra_restricted` a single-channel guest, `deleted` a deactivated
    member, `is_bot` a bot user; `users.list` includes deactivated members.
    https://docs.slack.dev/reference/objects/user-object, https://docs.slack.dev/reference/methods/users.list"""
    client, _ = sdk
    found = await _members(client)
    assert (found["guest"]["is_restricted"], found["guest"]["is_ultra_restricted"]) == (True, False)
    assert (found["single"]["is_restricted"], found["single"]["is_ultra_restricted"]) == (True, True)
    assert found["helper"]["is_bot"] is True and "email" not in found["helper"]["profile"]
    assert found["gone"]["deleted"] is True
    assert (found["owen"]["is_restricted"], found["owen"]["is_bot"], found["owen"]["deleted"]) == (False, False, False)


def test_a_single_channel_guest_who_is_no_guest_is_refused(tmp_path: Path) -> None:
    body = json.dumps({"single_channel_guests": ["owen"]})
    scenario = SCENARIO.model_copy(
        update={"provider_seeds": [SCENARIO.provider_seeds[0].model_copy(update={"body": body})]}
    )
    store = SqliteStore(tmp_path / "w.db", "refused", RunClock(START))
    try:
        with pytest.raises(ValueError, match="owen is not one"):
            build().seed(scenario, store)
    finally:
        store.close()
