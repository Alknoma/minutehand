"""One message's permalink, reactions and pins, round-tripped through stock `slack_sdk` and the real proxy.

Each test says whether the claim is DOCUMENTED, with the page; `CLAIMS.md` beside the provider is the table of them.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.seed import person_message
from minutehand.adapters.providers.slack.state import BOT_USER_ID
from minutehand.domain.world import Actor, Operation
from tests.providers.slack.intercepted import SECRET, AgentEndpoint, Intercepted, data
from tests.providers.slack.slack_workspace import GENERAL, Workspace
from tests.providers.slack.test_slack_conversations import not_served, refusal

IRIS = state.user_id("iris")


async def posted(slack: Intercepted, text: str = "Launch is Thursday", **more: Any) -> str:
    return str(data(await slack.asynchronous().chat_postMessage(channel=GENERAL, text=text, **more))["ts"])


def reacted_by_someone(workspace: Workspace, users: list[str], name: str = "eyes") -> str:
    """A message a person wrote that the given people reacted to."""
    ts = workspace.slack.ts_at(int(workspace.clock.now().timestamp()))
    message = person_message(ts, IRIS, "Look at this", None, [], state.TEAM_ID).model_copy(
        update={"reactions": [wire.SlackReaction(name=name, users=users, count=len(users))]}
    )
    workspace.slack.write(
        state.message_ref(ts), message, operation=Operation.CREATE, actor=Actor.PERSON, parent=GENERAL
    )
    return ts


# ---------------------------------------------------------------------- chat.getPermalink


async def test_a_message_has_a_permalink_of_its_workspace_channel_and_stamp(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `https://<workspace>.slack.com/archives/<channel>/p<ts without the dot>`.
    https://docs.slack.dev/reference/methods/chat.getPermalink"""
    ts = await posted(slack)

    found = data(await slack.asynchronous().chat_getPermalink(channel=GENERAL, message_ts=ts))

    assert found["channel"] == GENERAL
    assert found["permalink"] == f"https://{state.TEAM_DOMAIN}.slack.com/archives/{GENERAL}/p{ts.replace('.', '')}"


async def test_a_reply_has_a_permalink_that_names_its_thread(slack: Intercepted) -> None:
    """DOCUMENTED: for a message in a thread "the permalink format changes": `?thread_ts=<parent>&cid=<channel>`.
    https://docs.slack.dev/reference/methods/chat.getPermalink"""
    root = await posted(slack)
    reply = await posted(slack, "Noted", thread_ts=root)

    found = data(await slack.asynchronous().chat_getPermalink(channel=GENERAL, message_ts=reply))
    parent = data(await slack.asynchronous().chat_getPermalink(channel=GENERAL, message_ts=root))

    assert found["permalink"].endswith(f"/p{reply.replace('.', '')}?thread_ts={root}&cid={GENERAL}")
    assert "?" not in parent["permalink"]


async def test_a_permalink_for_no_message_or_no_channel_is_refused(slack: Intercepted) -> None:
    """DOCUMENTED: `message_not_found`, `channel_not_found`. https://docs.slack.dev/reference/methods/chat.getPermalink"""
    sdk = slack.asynchronous()

    assert (await refusal(sdk.chat_getPermalink(channel=GENERAL, message_ts="1.000001")))[
        "error"
    ] == "message_not_found"
    assert (await refusal(sdk.chat_getPermalink(channel="C0NOSUCH", message_ts="1.000001")))[
        "error"
    ] == "channel_not_found"


# ---------------------------------------------------------------------- reactions


async def test_a_reaction_the_app_added_is_read_with_reactions_get_and_gone_after_reactions_remove(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `reactions.get` answers the message with its `reactions` (`name`, `users`, `count`) and a
    `permalink`; `reactions.remove` removes the caller's reaction. https://docs.slack.dev/reference/methods/reactions.get,
    https://docs.slack.dev/reference/methods/reactions.remove"""
    sdk = slack.asynchronous()
    ts = await posted(slack)
    await sdk.reactions_add(channel=GENERAL, timestamp=ts, name="thumbsup")

    got = data(await sdk.reactions_get(channel=GENERAL, timestamp=ts))

    assert (got["type"], got["channel"]) == ("message", GENERAL)
    assert got["message"]["reactions"] == [{"name": "thumbsup", "users": [BOT_USER_ID], "count": 1}]
    assert got["message"]["permalink"].endswith(f"/p{ts.replace('.', '')}")
    assert data(await sdk.reactions_remove(channel=GENERAL, timestamp=ts, name="thumbsup")) == {"ok": True}
    after = data(await sdk.reactions_get(channel=GENERAL, timestamp=ts))["message"]
    assert "reactions" not in after
    history = data(await sdk.conversations_history(channel=GENERAL))["messages"]
    assert "reactions" not in history[0]


async def test_removing_a_shared_reaction_leaves_the_others_and_counts_one_less(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: "`count` ... will always represent the count of all users who made that reaction"; removing
    removes the caller's. https://docs.slack.dev/reference/methods/reactions.get"""
    ts = reacted_by_someone(workspace, [IRIS, BOT_USER_ID])
    sdk = slack.asynchronous()

    await sdk.reactions_remove(channel=GENERAL, timestamp=ts, name="eyes")

    got = data(await sdk.reactions_get(channel=GENERAL, timestamp=ts))["message"]
    assert got["reactions"] == [{"name": "eyes", "users": [IRIS], "count": 1}]


async def test_removing_a_reaction_the_app_did_not_make_is_refused_no_reaction(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `no_reaction`, "The specified reaction does not exist, or the requestor is not the original
    reaction author". https://docs.slack.dev/reference/methods/reactions.remove"""
    sdk = slack.asynchronous()
    theirs = reacted_by_someone(workspace, [IRIS])
    mine = await posted(slack)

    assert (await refusal(sdk.reactions_remove(channel=GENERAL, timestamp=theirs, name="eyes")))[
        "error"
    ] == "no_reaction"
    assert (await refusal(sdk.reactions_remove(channel=GENERAL, timestamp=mine, name="eyes")))["error"] == "no_reaction"
    got = data(await sdk.reactions_get(channel=GENERAL, timestamp=theirs))["message"]
    assert got["reactions"][0]["users"] == [IRIS]


@pytest.mark.parametrize("method", ["reactions_remove", "reactions_get"])
async def test_a_reaction_call_naming_no_item_a_bad_stamp_or_no_message_is_refused(
    slack: Intercepted, method: str
) -> None:
    """DOCUMENTED: `no_item_specified`, `bad_timestamp`, `message_not_found`, `channel_not_found`.
    https://docs.slack.dev/reference/methods/reactions.remove, https://docs.slack.dev/reference/methods/reactions.get"""
    call = getattr(slack.asynchronous(), method)
    more = {"name": "eyes"} if method == "reactions_remove" else {}

    assert (await refusal(call(**more)))["error"] == "no_item_specified"
    assert (await refusal(call(channel=GENERAL, **more)))["error"] == "no_item_specified"
    assert (await refusal(call(channel=GENERAL, timestamp="soon", **more)))["error"] == "bad_timestamp"
    assert (await refusal(call(channel=GENERAL, timestamp="1.000001", **more)))["error"] == "message_not_found"
    assert (await refusal(call(channel="C0NOSUCH", timestamp="1.000001", **more)))["error"] == "channel_not_found"


async def test_removing_a_reaction_with_no_name_is_refused_invalid_name(slack: Intercepted) -> None:
    """DOCUMENTED: `invalid_name`. https://docs.slack.dev/reference/methods/reactions.remove"""
    ts = await posted(slack)

    answer = await refusal(slack.asynchronous().reactions_remove(channel=GENERAL, timestamp=ts, name=""))

    assert answer["error"] == "invalid_name"


@pytest.mark.parametrize(("method", "more"), [("reactions_remove", {"name": "eyes"}), ("reactions_get", {})])
async def test_a_file_or_a_file_comment_is_refused_by_name(
    slack: Intercepted, method: str, more: dict[str, str]
) -> None:
    """DOCUMENTED GAP: the pages take a `file` or a `file_comment`; the fake serves reactions on messages."""
    call = getattr(slack.asynchronous(), method)

    assert "file" in await not_served(call(file="F1", **more))
    assert "file" in await not_served(call(file_comment="Fc1", **more))


async def test_removing_a_reaction_sends_the_agent_reaction_removed(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: `reaction_removed` with `user`, `reaction`, `item_user`, `item` and `event_ts`.
    https://docs.slack.dev/reference/events/reaction_removed"""
    sdk = slack.asynchronous()
    ts = await posted(slack)
    await sdk.reactions_add(channel=GENERAL, timestamp=ts, name="eyes")
    workspace.provider.listen(agent.target(), SECRET)

    await sdk.reactions_remove(channel=GENERAL, timestamp=ts, name="eyes")

    for _ in range(300):
        if agent.received:
            break
        await asyncio.sleep(0.02)
    event = agent.received[0].json["event"]
    assert event["type"] == "reaction_removed"
    assert (event["user"], event["reaction"], event["item_user"]) == (BOT_USER_ID, "eyes", BOT_USER_ID)
    assert event["item"] == {"type": "message", "channel": GENERAL, "ts": ts}
    assert event["event_ts"]


async def test_reactions_list_lists_what_the_caller_reacted_to(slack: Intercepted) -> None:
    """DOCUMENTED: it lists the items with reactions made by `user`, "Defaults to the authed user"; a message item
    has `type`, `channel` and `message`. https://docs.slack.dev/reference/methods/reactions.list"""
    sdk = slack.asynchronous()
    ts = await posted(slack)
    await posted(slack, "No reaction here")
    await sdk.reactions_add(channel=GENERAL, timestamp=ts, name="thumbsup")

    listed = data(await sdk.reactions_list())
    other = data(await sdk.reactions_list(user=IRIS))

    assert [(i["type"], i["channel"], i["message"]["ts"]) for i in listed["items"]] == [("message", GENERAL, ts)]
    assert listed["items"][0]["message"]["reactions"] == [{"name": "thumbsup", "users": [BOT_USER_ID], "count": 1}]
    assert listed["response_metadata"]["next_cursor"] == "" and other["items"] == []


async def test_reactions_list_of_a_user_slack_has_no_member_for_is_refused_user_not_found(slack: Intercepted) -> None:
    """DOCUMENTED: `user_not_found`. https://docs.slack.dev/reference/methods/reactions.list"""
    answer = await refusal(slack.asynchronous().reactions_list(user="U0NOSUCH"))

    assert answer["error"] == "user_not_found"


async def test_reactions_list_of_more_than_one_item_is_refused_by_name(slack: Intercepted) -> None:
    """DOCUMENTED GAP: the page gives no order for the items, and shows no message listing more than the one reaction."""
    sdk = slack.asynchronous()
    for text in ("one", "two"):
        await sdk.reactions_add(channel=GENERAL, timestamp=await posted(slack, text), name="eyes")

    said = await not_served(sdk.reactions_list())

    assert "order" in said


# ---------------------------------------------------------------------- pins


async def test_a_pinned_message_is_listed_with_who_pinned_it_and_when_and_gone_once_removed(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `pins.list` items have `type`, `channel`, `created`, `created_by` and the `message` with its
    `permalink` and `pinned_to`. https://docs.slack.dev/reference/methods/pins.list"""
    sdk = slack.asynchronous()
    ts = await posted(slack, "What is the meaning of life?")

    assert data(await sdk.pins_add(channel=GENERAL, timestamp=ts)) == {"ok": True}

    pins = data(await sdk.pins_list(channel=GENERAL))["items"]
    assert len(pins) == 1
    assert (pins[0]["type"], pins[0]["channel"], pins[0]["created_by"]) == ("message", GENERAL, BOT_USER_ID)
    assert pins[0]["created"] == int(workspace.clock.now().timestamp())
    assert pins[0]["message"]["ts"] == ts and pins[0]["message"]["pinned_to"] == [GENERAL]
    assert pins[0]["message"]["permalink"].endswith(f"/p{ts.replace('.', '')}")
    assert data(await sdk.pins_remove(channel=GENERAL, timestamp=ts)) == {"ok": True}
    assert data(await sdk.pins_list(channel=GENERAL))["items"] == []


async def test_pins_are_listed_newest_first(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: the two items of the page's example are listed with the later `created` first.
    https://docs.slack.dev/reference/methods/pins.list"""
    sdk = slack.asynchronous()
    first, second = await posted(slack, "first"), await posted(slack, "second")
    await sdk.pins_add(channel=GENERAL, timestamp=first)
    workspace.clock.jump(workspace.clock.now() + timedelta(minutes=5))
    await sdk.pins_add(channel=GENERAL, timestamp=second)

    pins = data(await sdk.pins_list(channel=GENERAL))["items"]

    assert [p["message"]["text"] for p in pins] == ["second", "first"]


async def test_pinning_twice_is_refused_already_pinned_and_unpinning_what_is_not_pinned_no_pin(
    slack: Intercepted,
) -> None:
    """DOCUMENTED: `already_pinned`; the page's typical error for removing a pin that is not there is `no_pin`.
    https://docs.slack.dev/reference/methods/pins.add, https://docs.slack.dev/reference/methods/pins.remove"""
    sdk = slack.asynchronous()
    ts = await posted(slack)
    await sdk.pins_add(channel=GENERAL, timestamp=ts)

    assert (await refusal(sdk.pins_add(channel=GENERAL, timestamp=ts)))["error"] == "already_pinned"
    other = await posted(slack, "other")
    assert (await refusal(sdk.pins_remove(channel=GENERAL, timestamp=other)))["error"] == "no_pin"


@pytest.mark.parametrize("method", ["pins_add", "pins_remove"])
async def test_a_pin_call_naming_no_message_a_bad_stamp_or_no_channel_is_refused(
    slack: Intercepted, method: str
) -> None:
    """DOCUMENTED: `no_item_specified`, `bad_timestamp`, `message_not_found`, `channel_not_found`.
    https://docs.slack.dev/reference/methods/pins.add, https://docs.slack.dev/reference/methods/pins.remove"""
    call = getattr(slack.asynchronous(), method)

    assert (await refusal(call(channel=GENERAL)))["error"] == "no_item_specified"
    assert (await refusal(call(channel=GENERAL, timestamp="soon")))["error"] == "bad_timestamp"
    assert (await refusal(call(channel=GENERAL, timestamp="1.000001")))["error"] == "message_not_found"
    assert (await refusal(call(channel="C0NOSUCH", timestamp="1.000001")))["error"] == "channel_not_found"


async def test_listing_the_pins_of_no_channel_is_refused_channel_not_found(slack: Intercepted) -> None:
    """DOCUMENTED: `channel_not_found`. https://docs.slack.dev/reference/methods/pins.list"""
    answer = await refusal(slack.asynchronous().pins_list(channel="C0NOSUCH"))

    assert answer["error"] == "channel_not_found"


async def test_a_pin_from_outside_the_channel_or_of_a_message_with_a_file_is_refused_by_name(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED GAP: the pages of `pins.*` name no error for an app outside the channel, and say files cannot be
    pinned without saying of a message that shares one."""
    sdk = slack.asynchronous()
    outside = workspace.channel_without_the_app("elsewhere", members=["iris"])
    ts = workspace.slack.ts_at(int(workspace.clock.now().timestamp()))
    shared = wire.SlackFile(
        id="F1",
        created=1,
        timestamp=1,
        name="a.txt",
        title="a",
        mimetype="text/plain",
        filetype="txt",
        pretty_type="TXT",
        user=IRIS,
        user_team=state.TEAM_ID,
        size=1,
        is_public=False,
        url_private="https://files.slack.com/x",
        url_private_download="https://files.slack.com/x",
        permalink="https://x.slack.com/files/x",
    )
    workspace.slack.write(
        state.message_ref(ts),
        person_message(ts, IRIS, "See file", None, [shared], state.TEAM_ID),
        operation=Operation.CREATE,
        actor=Actor.PERSON,
        parent=GENERAL,
    )

    assert "not in the channel" in await not_served(sdk.pins_add(channel=outside, timestamp="1.000001"))
    assert "file" in await not_served(sdk.pins_add(channel=GENERAL, timestamp=ts))
