"""People with no email, people named otherwise in Slack, and the ids a seed declares: driven with slack_sdk through
the proxy, as the agent calls Slack."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.application.run_clock import RunClock
from minutehand.domain.provider import PersonChange
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, MessageSnapshot
from tests.providers.slack.intercepted import data

START = datetime(2026, 9, 1, 9, tzinfo=UTC)
TOKEN = "xoxb-people-and-ids"
POSTED = int(START.timestamp()) - 3600
"""A second an hour before the start: the `ts` a recorded message declares."""

WRITTEN: dict[str, Any] = {
    "name": "people_and_ids",
    "goal": "-",
    "owner": "owen",
    "starts_at": START.isoformat(),
    "people": [
        {"key": "owen", "name": "Owen Owner", "email": "owen@example.com"},
        {"key": "svc", "name": "Build Service", "accounts": [{"provider": "slack", "id": "U0SVC0001"}]},
        {
            "key": "hana",
            "name": "Hana Hidden",
            "email": "hana@example.com",
            "accounts": [{"provider": "slack", "email_visible": False, "name": "hana.h"}],
        },
    ],
    "channels": [
        {
            "provider": "slack",
            "name": "backend",
            "id": "C0BACKEND1",
            "members": ["owen", "svc"],
            "history": [
                {
                    "by": "owen",
                    "text": "deploy is green",
                    "ago": "PT1H",
                    "id": f"{POSTED}.000142",
                    "replies": [{"by": "svc", "text": "confirmed", "ago": "PT30M", "id": f"{POSTED + 1800}.000007"}],
                },
                {"by": "svc", "text": "nightly ran", "ago": "PT2H"},
            ],
        },
        {"provider": "slack", "members": ["svc"], "id": "D0SVCDM01"},
    ],
}


def scenario(**changes: Any) -> Scenario:
    return Scenario.model_validate({**WRITTEN, **changes})


@pytest.fixture
async def sdk(tmp_path: Path) -> AsyncIterator[tuple[AsyncWebClient, SqliteStore]]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "people-and-ids", clock)
    provider = build()
    provider.seed(scenario(), store)
    proxy = Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca")
    async with proxy:
        proxy.mount(store, clock, {"slack": provider.app(store, clock)})
        trust = ssl.create_default_context(cafile=str(proxy.ca_bundle))
        yield AsyncWebClient(token=TOKEN, proxy=proxy.url, ssl=trust), store
    store.close()


def _seed(tmp_path: Path, seeded: Scenario) -> None:
    store = SqliteStore(tmp_path / "w.db", "refused", RunClock(START))
    try:
        build().seed(seeded, store)
    finally:
        store.close()


async def test_a_person_with_no_email_is_listed_without_one_and_a_message_still_reaches_them(
    sdk: tuple[AsyncWebClient, SqliteStore],
) -> None:
    """DOCUMENTED: a profile's `email` is served only when the member has one and the app holds `users:read.email`.
    https://docs.slack.dev/reference/objects/user-object"""
    client, store = sdk
    info = data(await client.users_info(user="U0SVC0001"))["user"]
    assert info["name"] == "svc" and info["real_name"] == "Build Service"
    assert "email" not in info["profile"]
    opened = data(await client.conversations_open(users=["U0SVC0001"]))["channel"]["id"]
    await client.chat_postMessage(channel=opened, text="Is the build green?")
    sent = [e.after for e in store.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)]
    assert sent and isinstance(sent[-1], MessageSnapshot)
    assert (sent[-1].recipients, sent[-1].recipient_emails) == (["svc"], [])


async def test_an_account_whose_email_is_hidden_shows_none_and_is_not_found_by_it(
    sdk: tuple[AsyncWebClient, SqliteStore],
) -> None:
    client, store = sdk
    members = {m["name"]: m for m in data(await client.users_list())["members"]}
    hana = members["hana"]
    assert "email" not in hana["profile"] and hana["profile"]["display_name"] == "hana.h"
    with pytest.raises(SlackApiError) as refused:
        await client.users_lookupByEmail(email="hana@example.com")
    assert refused.value.response["error"] == "users_not_found"
    opened = data(await client.conversations_open(users=[hana["id"]]))["channel"]["id"]
    await client.chat_postMessage(channel=opened, text="Free Thursday?")
    sent = [e.after for e in store.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)]
    assert isinstance(sent[-1], MessageSnapshot)
    assert (sent[-1].recipients, sent[-1].recipient_emails) == (["hana"], ["hana@example.com"])


async def test_declared_channel_post_and_thread_ids_are_the_ones_slack_answers_with(
    sdk: tuple[AsyncWebClient, SqliteStore],
) -> None:
    client, _ = sdk
    history = data(await client.conversations_history(channel="C0BACKEND1"))["messages"]
    assert [m["text"] for m in history] == ["deploy is green", "nightly ran"]
    assert (history[0]["ts"], history[1]["user"]) == (f"{POSTED}.000142", "U0SVC0001")
    thread = data(await client.conversations_replies(channel="C0BACKEND1", ts=f"{POSTED}.000142"))["messages"]
    assert [(m["ts"], m["text"]) for m in thread] == [
        (f"{POSTED}.000142", "deploy is green"),
        (f"{POSTED + 1800}.000007", "confirmed"),
    ]
    info = data(await client.conversations_info(channel="C0BACKEND1"))["channel"]
    assert info["name"] == "backend"


async def test_a_declared_direct_conversation_id_is_the_one_opening_it_answers(
    sdk: tuple[AsyncWebClient, SqliteStore],
) -> None:
    client, _ = sdk
    opened = data(await client.conversations_open(users=["U0SVC0001"]))
    assert opened["channel"]["id"] == "D0SVCDM01" and opened["already_open"] is True


def test_a_person_with_a_declared_id_is_deactivated_by_who_they_are(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "w.db", "change", RunClock(START))
    try:
        provider = build()
        seeded = scenario()
        provider.seed(seeded, store)
        svc = next(p for p in seeded.people if p.key == "svc")
        provider.change_person(PersonChange.DEACTIVATED, svc, store, RunClock(START))
        user = state.SlackWorld(store).user("U0SVC0001")
        assert user is not None and user.deleted
    finally:
        store.close()


@pytest.mark.parametrize(
    ("changed", "refusal"),
    [
        ({"people": [{"key": "owen", "name": "O", "accounts": [{"provider": "slack", "id": "u-1"}]}]}, "is not one"),
        (
            {"channels": [{"provider": "slack", "name": "backend", "id": "X0BAD"}], "people": WRITTEN["people"]},
            "is not one",
        ),
        (
            {"channels": [{"provider": "slack", "name": "general2", "id": state.named_channel_id(state.GENERAL)}]},
            "another channel has",
        ),
        (
            {
                "channels": [
                    {
                        "provider": "slack",
                        "name": "c",
                        "history": [{"by": "owen", "text": "t", "ago": "PT1H", "id": "12.5"}],
                    }
                ]
            },
            "is not one",
        ),
        (
            {
                "channels": [
                    {
                        "provider": "slack",
                        "name": "c",
                        "history": [
                            {"by": "owen", "text": "t", "ago": "PT1H", "id": f"{int(START.timestamp())}.000001"}
                        ],
                    }
                ]
            },
            "not before the scenario's start",
        ),
        ({"channels": [{"provider": "slack", "members": ["owen"], "topic": "x"}]}, "has no topic"),
        ({"channels": [{"provider": "slack", "members": ["owen"], "id": "C0NOTDM"}]}, "starts with D"),
    ],
)
def test_a_declared_slack_id_out_of_format_or_taken_is_refused(
    tmp_path: Path, changed: dict[str, Any], refusal: str
) -> None:
    given = {"people": [{"key": "owen", "name": "Owen Owner", "email": "owen@example.com"}], "channels": []}
    with pytest.raises(ValueError, match=refusal):
        _seed(tmp_path, scenario(**{**given, **changed}))


def test_a_declared_user_id_in_two_workspaces_is_refused(tmp_path: Path) -> None:
    seeded = scenario(
        channels=[],
        provider_seeds=[
            {
                "provider": "slack",
                "body": {
                    "workspaces": [{"team_id": "T0ONE"}, {"team_id": "T0TWO", "bot_user_id": "U0BOT2", "domain": "two"}]
                },
            }
        ],
    )
    with pytest.raises(ValueError, match="more than one workspace"):
        _seed(tmp_path, seeded)


def test_a_slack_account_entry_naming_a_login_is_refused() -> None:
    seeded = scenario(
        channels=[], people=[{"key": "owen", "name": "O", "accounts": [{"provider": "slack", "login": "owen.o"}]}]
    )
    with pytest.raises(RunRefused, match="sets login"):
        refuse_unheld(seeded, {MANIFEST.key: MANIFEST})
