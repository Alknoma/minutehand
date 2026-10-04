"""The fake refuses what Slack refuses, and pages where Slack pages.

Ported from the refusal suite of the emulator this provider replaces. Each case
there was a leniency that hid a real defect: `conversations.open` accepting any
string let a client address DMs with ids that were never Slack members.
The token-scope cases are not ported: any Slack-shaped token is accepted here.
"""

from __future__ import annotations

import json

import httpx

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.state import BOT_ID, BOT_USER_ID
from minutehand.domain.world import Actor, Operation
from tests.providers.slack.slack_workspace import GENERAL, Workspace, body, form, messages_of, text_of


def _ids(answer: dict[str, object], key: str) -> list[str]:
    found = answer[key]
    assert isinstance(found, list)
    return [item if isinstance(item, str) else item["id"] for item in found]


def _cursor(answer: dict[str, object]) -> str:
    metadata = answer["response_metadata"]
    assert isinstance(metadata, dict)
    return metadata["next_cursor"]


# ---------------------------------------------------------------------- conversations.open


async def test_open_reaches_the_seeded_dm_and_is_idempotent(workspace: Workspace, client: httpx.AsyncClient) -> None:
    first = await form(client, "conversations.open", users=state.user_id("iris"))
    again = await form(client, "conversations.open", users=state.user_id("iris"))
    assert first["channel"] == again["channel"] == {"id": workspace.dm("iris")}
    assert again["already_open"] is True


async def test_open_with_an_id_that_is_no_member_is_refused_user_not_found(client: httpx.AsyncClient) -> None:
    assert await form(client, "conversations.open", users="zlqYFcbnX5gBhZ9xQwErTyUiOp01") == {
        "ok": False, "error": "user_not_found"
    }


async def test_open_with_one_unknown_id_in_a_group_is_refused_whole(client: httpx.AsyncClient) -> None:
    before = await form(client, "conversations.list", types="mpim")
    answer = await form(client, "conversations.open", users=f"{state.user_id('iris')},ws_94ba9c4021f3")
    assert answer == {"ok": False, "error": "user_not_found"}
    assert await form(client, "conversations.list", types="mpim") == before


async def test_open_with_two_people_makes_one_group_dm(client: httpx.AsyncClient) -> None:
    pair = f"{state.user_id('iris')},{state.user_id('tomas')}"
    first = await form(client, "conversations.open", users=pair, return_im="true")
    channel = first["channel"]
    assert isinstance(channel, dict) and channel["is_mpim"] is True
    members = await form(client, "conversations.members", channel=channel["id"])
    assert sorted(_ids(members, "members")) == sorted([BOT_USER_ID, state.user_id("iris"), state.user_id("tomas")])


# ---------------------------------------------------------------------- channels


async def test_an_unknown_channel_is_refused_channel_not_found(client: httpx.AsyncClient) -> None:
    assert await form(client, "conversations.info", channel="C0NOSUCHCHAN") == {
        "ok": False, "error": "channel_not_found"
    }


async def test_a_channel_name_is_refused_where_an_id_belongs(client: httpx.AsyncClient) -> None:
    assert await form(client, "conversations.info", channel="general") == {"ok": False, "error": "channel_not_found"}


async def test_a_member_id_as_channel_posts_to_their_dm(workspace: Workspace, client: httpx.AsyncClient) -> None:
    answer = await body(client, "chat.postMessage", channel=state.user_id("noor"), text="straight to the DM")
    assert answer["ok"] is True and answer["channel"] == workspace.dm("noor")


async def test_a_member_id_nobody_has_is_refused_channel_not_found(client: httpx.AsyncClient) -> None:
    assert await body(client, "chat.postMessage", channel="U_NOSUCHPERSON", text="nope") == {
        "ok": False, "error": "channel_not_found"
    }


async def test_reading_a_channel_the_app_is_not_in_is_refused_not_in_channel(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    leadership = workspace.channel_without_the_app("leadership", members=["iris"])
    assert await form(client, "conversations.history", channel=leadership) == {"ok": False, "error": "not_in_channel"}
    assert await body(client, "chat.postMessage", channel=leadership, text="hi") == {
        "ok": False, "error": "not_in_channel"
    }


async def test_is_member_reports_the_app_not_the_humans(workspace: Workspace, client: httpx.AsyncClient) -> None:
    leadership = workspace.channel_without_the_app("leadership", members=["iris", "tomas"])
    joined = await form(client, "conversations.info", channel=GENERAL)
    not_joined = await form(client, "conversations.info", channel=leadership)
    assert isinstance(joined["channel"], dict) and joined["channel"]["is_member"] is True
    assert isinstance(not_joined["channel"], dict) and not_joined["channel"]["is_member"] is False


async def test_another_persons_dm_is_refused_channel_not_found(workspace: Workspace, client: httpx.AsyncClient) -> None:
    theirs = state.conversation_id([state.user_id("iris"), state.user_id("tomas")])
    workspace.slack.open_conversation([state.user_id("iris"), state.user_id("tomas")], created=0,
                                      actor=Actor.SCENARIO)
    assert await form(client, "conversations.history", channel=theirs) == {"ok": False, "error": "channel_not_found"}


# ---------------------------------------------------------------------- messages


async def test_deleting_a_message_nobody_posted_is_refused_message_not_found(client: httpx.AsyncClient) -> None:
    assert await form(client, "chat.delete", channel=GENERAL, ts="1.000000") == {
        "ok": False, "error": "message_not_found"
    }


async def test_reacting_to_a_message_nobody_posted_is_refused_message_not_found(client: httpx.AsyncClient) -> None:
    assert await form(client, "reactions.add", channel=GENERAL, timestamp="1.000000", name="eyes") == {
        "ok": False, "error": "message_not_found"
    }


async def test_updating_a_message_nobody_posted_is_refused_message_not_found(client: httpx.AsyncClient) -> None:
    assert await body(client, "chat.update", channel=GENERAL, ts="1.000000", text="x") == {
        "ok": False, "error": "message_not_found"
    }


async def test_more_than_fifty_blocks_is_refused_invalid_blocks(client: httpx.AsyncClient) -> None:
    answer = await body(client, "chat.postMessage", channel=GENERAL, text="x", blocks=[{"type": "divider"}] * 51)
    assert answer == {"ok": False, "error": "invalid_blocks"}


async def test_fifty_blocks_sent_as_a_form_are_accepted(client: httpx.AsyncClient) -> None:
    answer = await form(client, "chat.postMessage", channel=GENERAL, text="x", blocks=json.dumps([{"type": "divider"}] * 50))
    assert answer["ok"] is True


async def test_text_past_forty_thousand_chars_is_refused_msg_too_long(client: httpx.AsyncClient) -> None:
    assert await body(client, "chat.postMessage", channel=GENERAL, text="x" * 40_001) == {
        "ok": False, "error": "msg_too_long"
    }


async def test_a_message_with_no_text_is_refused_no_text(client: httpx.AsyncClient) -> None:
    assert await body(client, "chat.postMessage", channel=GENERAL, text="") == {"ok": False, "error": "no_text"}


async def test_as_user_is_a_boolean_and_never_the_author(client: httpx.AsyncClient) -> None:
    """slack_sdk urlencodes True as "1"; read as a user id it would make "1" the author."""
    answer = await form(client, "chat.postMessage", channel=GENERAL, text="hi", as_user="1")
    message = answer["message"]
    assert isinstance(message, dict) and message["user"] == BOT_USER_ID and message["bot_id"] == BOT_ID


async def test_a_thread_parent_carries_its_reply_summary(client: httpx.AsyncClient) -> None:
    parent = await body(client, "chat.postMessage", channel=GENERAL, text="root")
    reply = await body(client, "chat.postMessage", channel=GENERAL, text="reply", thread_ts=parent["ts"])

    root = messages_of(await form(client, "conversations.history", channel=GENERAL))[0]

    assert root["ts"] == parent["ts"]
    assert (root["reply_count"], root["latest_reply"], root["reply_users"]) == (1, reply["ts"], [BOT_USER_ID])


async def test_history_returns_roots_not_replies(client: httpx.AsyncClient) -> None:
    parent = await body(client, "chat.postMessage", channel=GENERAL, text="root")
    await body(client, "chat.postMessage", channel=GENERAL, text="reply", thread_ts=parent["ts"])
    assert text_of(await form(client, "conversations.history", channel=GENERAL)) == ["root"]


async def test_replies_return_the_parent_then_the_thread(client: httpx.AsyncClient) -> None:
    parent = await body(client, "chat.postMessage", channel=GENERAL, text="root")
    first = await body(client, "chat.postMessage", channel=GENERAL, text="one", thread_ts=parent["ts"])
    await body(client, "chat.postMessage", channel=GENERAL, text="two", thread_ts=first["ts"])
    thread = await form(client, "conversations.replies", channel=GENERAL, ts=str(first["ts"]))
    assert text_of(thread) == ["root", "one", "two"]


async def test_a_reply_to_a_thread_nobody_started_is_refused_thread_not_found(client: httpx.AsyncClient) -> None:
    assert await body(client, "chat.postMessage", channel=GENERAL, text="x", thread_ts="1.000000") == {
        "ok": False, "error": "thread_not_found"
    }


async def test_history_honours_inclusive(client: httpx.AsyncClient) -> None:
    """A card reads itself back with `latest=<its ts>, inclusive=1, limit=1`."""
    await body(client, "chat.postMessage", channel=GENERAL, text="the one before")
    card = await body(client, "chat.postMessage", channel=GENERAL, text="the card")

    inclusive = await form(client, "conversations.history", channel=GENERAL, latest=str(card["ts"]), inclusive="1", limit="1")
    exclusive = await form(client, "conversations.history", channel=GENERAL, latest=str(card["ts"]), inclusive="0", limit="1")

    assert text_of(inclusive) == ["the card"]
    assert text_of(exclusive) == ["the one before"]


async def test_editing_a_persons_message_is_refused_cant_update_message(workspace: Workspace, client: httpx.AsyncClient) -> None:
    ts = workspace.slack.next_ts(workspace.clock)
    theirs = wire.SlackMessage(ts=ts, user=state.user_id("iris"), text="mine", team=state.TEAM_ID)
    workspace.slack.write(state.message_ref(ts), theirs, operation=Operation.CREATE, actor=Actor.PERSON, parent=GENERAL)
    assert await body(client, "chat.update", channel=GENERAL, ts=ts, text="edited") == {
        "ok": False, "error": "cant_update_message"
    }
    assert await form(client, "chat.delete", channel=GENERAL, ts=ts) == {"ok": False, "error": "cant_delete_message"}


async def test_a_limit_that_is_not_a_number_is_refused_invalid_arguments(client: httpx.AsyncClient) -> None:
    assert await form(client, "users.list", limit="many") == {"ok": False, "error": "invalid_arguments"}


async def test_a_cursor_nobody_issued_is_refused_invalid_cursor(client: httpx.AsyncClient) -> None:
    assert await form(client, "users.list", cursor="bm90LWEtY3Vyc29y") == {"ok": False, "error": "invalid_cursor"}


# ---------------------------------------------------------------------- pagination


async def test_conversations_list_pages(workspace: Workspace, client: httpx.AsyncClient) -> None:
    for name in ("design", "launch", "ops", "support"):
        workspace.public_channel(name)
    first = await form(client, "conversations.list", types="public_channel", limit="3")
    second = await form(client, "conversations.list", types="public_channel", limit="3", cursor=_cursor(first))

    assert len(_ids(first, "channels")) == 3
    assert len(_ids(second, "channels")) == 2 and _cursor(second) == ""
    assert set(_ids(first, "channels")).isdisjoint(_ids(second, "channels"))


async def test_conversations_list_lists_the_dms_only_when_asked(workspace: Workspace, client: httpx.AsyncClient) -> None:
    public = await form(client, "conversations.list")
    ims = await form(client, "conversations.list", types="im")
    assert _ids(public, "channels") == [GENERAL]
    assert sorted(_ids(ims, "channels")) == sorted(workspace.dm(k) for k in ("iris", "tomas", "noor"))


async def test_users_list_pages(client: httpx.AsyncClient) -> None:
    first = await form(client, "users.list", limit="3")
    rest = await form(client, "users.list", limit="3", cursor=_cursor(first))
    assert len(_ids(first, "members")) == 3 and len(_ids(rest, "members")) == 1 and _cursor(rest) == ""


async def test_archived_channels_are_excluded_on_request(workspace: Workspace, client: httpx.AsyncClient) -> None:
    retired = workspace.public_channel("retired", archived=True)
    with_archived = await form(client, "conversations.list", types="public_channel")
    without = await form(client, "conversations.list", types="public_channel", exclude_archived="1")
    assert retired in _ids(with_archived, "channels") and retired not in _ids(without, "channels")


async def test_conversations_members_pages(client: httpx.AsyncClient) -> None:
    first = await form(client, "conversations.members", channel=GENERAL, limit="2")
    rest = await form(client, "conversations.members", channel=GENERAL, limit="2", cursor=_cursor(first))
    assert len(_ids(first, "members")) == 2 and len(_ids(rest, "members")) == 2 and _cursor(rest) == ""
    assert BOT_USER_ID in _ids(first, "members") + _ids(rest, "members")


async def test_history_pages_newest_first(client: httpx.AsyncClient) -> None:
    for n in range(5):
        await body(client, "chat.postMessage", channel=GENERAL, text=f"m{n}")
    first = await form(client, "conversations.history", channel=GENERAL, limit="2")
    second = await form(client, "conversations.history", channel=GENERAL, limit="2", cursor=_cursor(first))
    third = await form(client, "conversations.history", channel=GENERAL, limit="2", cursor=_cursor(second))
    assert [text_of(p) for p in (first, second, third)] == [["m4", "m3"], ["m2", "m1"], ["m0"]]
    assert (first["has_more"], third["has_more"]) == (True, False)


async def test_replies_page_oldest_first(client: httpx.AsyncClient) -> None:
    parent = await body(client, "chat.postMessage", channel=GENERAL, text="root")
    for n in range(3):
        await body(client, "chat.postMessage", channel=GENERAL, text=f"r{n}", thread_ts=parent["ts"])
    first = await form(client, "conversations.replies", channel=GENERAL, ts=str(parent["ts"]), limit="2")
    rest = await form(client, "conversations.replies", channel=GENERAL, ts=str(parent["ts"]), limit="2",
                      cursor=_cursor(first))
    assert text_of(first) == ["root", "r0"] and text_of(rest) == ["r1", "r2"] and rest["has_more"] is False
