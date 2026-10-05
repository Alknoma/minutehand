"""The real `slack_sdk.WebClient`, over a real socket, against the app served by uvicorn.

The server runs on the test's own event loop, because the store's SQLite
connection belongs to the thread that opened it; the blocking client runs in a
worker thread so the loop stays free to answer it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

import pytest
import uvicorn
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse

from minutehand.adapters.providers.slack import state
from minutehand.domain.world import Actor, MessageSnapshot, Operation
from tests.providers.slack.slack_workspace import GENERAL, TOKEN, Workspace

T = TypeVar("T")


@pytest.fixture
async def sdk(workspace: Workspace) -> AsyncIterator[WebClient]:
    server = uvicorn.Server(
        uvicorn.Config(
            workspace.provider.app(workspace.store, workspace.clock),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield WebClient(token=TOKEN, base_url=f"http://127.0.0.1:{port}/api/")
    server.should_exit = True
    await serving


async def off_loop(call: Callable[[], T]) -> T:
    return await asyncio.to_thread(call)


async def sent(call: Callable[[], SlackResponse]) -> dict[str, Any]:
    """One call, off the loop, and the JSON Slack answered with."""
    response = await off_loop(call)
    assert isinstance(response.data, dict)
    return response.data


async def test_the_sdk_posts_to_a_dm_it_opened_and_reads_it_back(sdk: WebClient, workspace: Workspace) -> None:
    who = await sent(sdk.auth_test)
    found = await sent(lambda: sdk.users_lookupByEmail(email="iris@example.com"))
    opened = await sent(lambda: sdk.conversations_open(users=[found["user"]["id"]]))
    posted = await sent(lambda: sdk.chat_postMessage(channel=opened["channel"]["id"], text="ready for review?"))
    history = await sent(lambda: sdk.conversations_history(channel=opened["channel"]["id"], limit=10))

    assert who["user_id"] == state.BOT_USER_ID
    assert opened["channel"]["id"] == workspace.dm("iris")
    assert [m["text"] for m in history["messages"]] == ["ready for review?"]
    assert history["messages"][0]["ts"] == posted["ts"]


async def test_the_sdk_follows_cursors_to_the_last_page(sdk: WebClient) -> None:
    pages = await off_loop(lambda: [page.get("members") for page in sdk.users_list(limit=1)])
    assert len(pages) == 4 and all(isinstance(p, list) and len(p) == 1 for p in pages)


async def test_the_sdk_threads_a_reply_and_reads_the_thread(sdk: WebClient) -> None:
    root = await sent(lambda: sdk.chat_postMessage(channel=GENERAL, text="root"))
    await sent(lambda: sdk.chat_postMessage(channel=GENERAL, text="reply", thread_ts=root["ts"]))
    thread = await sent(lambda: sdk.conversations_replies(channel=GENERAL, ts=root["ts"]))
    assert [m["text"] for m in thread["messages"]] == ["root", "reply"]


async def test_a_refusal_reaches_the_sdk_as_its_own_error(sdk: WebClient) -> None:
    with pytest.raises(SlackApiError) as refused:
        await off_loop(lambda: sdk.conversations_info(channel="C0NOSUCHCHAN"))
    assert refused.value.response["error"] == "channel_not_found"


async def test_the_sdk_posts_an_ephemeral_message_that_only_its_member_was_shown(
    sdk: WebClient, workspace: Workspace
) -> None:
    iris = state.user_id("iris")
    posted = await sent(lambda: sdk.chat_postEphemeral(channel=GENERAL, user=iris, text="Sign in to continue."))
    history = await sent(lambda: sdk.conversations_history(channel=GENERAL))

    assert posted["ok"] is True and isinstance(posted["message_ts"], str)
    assert [m["ts"] for m in history["messages"]] == [], "an ephemeral message is in no history"
    [shown] = [e for e in workspace.store.events() if e.entity == state.message_ref(posted["message_ts"])]
    assert shown.actor is Actor.AGENT and shown.operation is Operation.CREATE
    assert isinstance(shown.after, MessageSnapshot)
    assert (shown.after.text, shown.after.channel, shown.after.recipient_emails) == (
        "Sign in to continue.",
        GENERAL,
        ["iris@example.com"],
    )
