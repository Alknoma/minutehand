"""The conversations an agent makes and changes, round-tripped through stock `slack_sdk` and the real proxy.

Each test says whether the claim is DOCUMENTED, with the page; `CLAIMS.md` beside the provider is the table of them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.state import BOT_USER_ID
from tests.providers.slack.intercepted import SECRET, AgentEndpoint, Intercepted, data
from tests.providers.slack.slack_workspace import GENERAL, Workspace

IRIS = state.user_id("iris")
TOMAS = state.user_id("tomas")
NOOR = state.user_id("noor")


async def refusal(call: Any) -> dict[str, Any]:
    """The body of a call Slack refused: HTTP 200, `ok` false and the code."""
    with pytest.raises(SlackApiError) as refused:
        await call
    assert refused.value.response.status_code == 200
    answer = refused.value.response.data
    assert isinstance(answer, dict) and answer["ok"] is False
    return answer


async def not_served(call: Any) -> str:
    """The message of a call this fake refuses by name: HTTP 501 `not_implemented`."""
    with pytest.raises(SlackApiError) as refused:
        await call
    assert refused.value.response.status_code == 501
    answer = refused.value.response.data
    assert isinstance(answer, dict) and answer["error"] == "not_implemented"
    return str(answer["response_metadata"]["messages"][0])


async def members(slack: Intercepted, channel: str) -> list[str]:
    found = data(await slack.asynchronous().conversations_members(channel=channel))["members"]
    assert isinstance(found, list)
    return found


async def made(slack: Intercepted, name: str = "launch-room", **more: Any) -> str:
    created = data(await slack.asynchronous().conversations_create(name=name, **more))
    return str(created["channel"]["id"])


# ---------------------------------------------------------------------- create


async def test_a_created_channel_reads_back_with_its_creator_as_its_only_member(slack: Intercepted) -> None:
    """DOCUMENTED: the response is a conversation object with the creator, `is_member` true and an empty topic and
    purpose; the creator is in it. https://docs.slack.dev/reference/methods/conversations.create"""
    sdk = slack.asynchronous()
    created = data(await sdk.conversations_create(name="launch-room"))["channel"]

    assert created["name"] == "launch-room" and created["creator"] == BOT_USER_ID
    assert created["is_channel"] is True and created["is_private"] is False and created["is_member"] is True
    assert created["topic"] == {"value": "", "creator": "", "last_set": 0}
    assert created["purpose"] == {"value": "", "creator": "", "last_set": 0}
    read = data(await sdk.conversations_info(channel=created["id"], include_num_members=True))["channel"]
    assert (read["id"], read["name"], read["creator"], read["num_members"]) == (
        created["id"],
        "launch-room",
        BOT_USER_ID,
        1,
    )
    assert await members(slack, created["id"]) == [BOT_USER_ID]
    listed = data(await sdk.conversations_list(limit=999))["channels"]
    assert "launch-room" in [c["name"] for c in listed]


async def test_a_created_channel_is_stamped_from_the_run_clock(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: `created` is a timestamp Slack assigns. https://docs.slack.dev/reference/methods/conversations.create"""
    created = data(await slack.asynchronous().conversations_create(name="later"))["channel"]

    assert created["created"] == int(workspace.clock.now().timestamp())


async def test_a_private_channel_is_made_and_only_the_app_sees_it(slack: Intercepted) -> None:
    """DOCUMENTED: `is_private` creates a private channel instead of a public one.
    https://docs.slack.dev/reference/methods/conversations.create"""
    sdk = slack.asynchronous()
    created = data(await sdk.conversations_create(name="secret-room", is_private=True))["channel"]

    assert created["is_private"] is True and created["is_channel"] is False and created["is_group"] is True
    private = data(await sdk.conversations_list(types="private_channel"))["channels"]
    assert [c["name"] for c in private] == ["secret-room"]
    public = data(await sdk.conversations_list(types="public_channel", limit=999))["channels"]
    assert "secret-room" not in [c["name"] for c in public]


async def test_a_channel_made_under_a_name_takes_the_id_its_name_stands_for(slack: Intercepted) -> None:
    """Minutehand's own: a channel id is assigned, and one derived from the name lets a scenario's people address
    the channel by name (`inbound.conversation`)."""
    assert await made(slack, "launch-room") == state.named_channel_id("launch-room")


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("", "invalid_name_required"),
        ("x" * 81, "invalid_name_maxlength"),
        ("Launch-Room", "invalid_name_specials"),
        ("launch room", "invalid_name_specials"),
        ("---", "invalid_name_punctuation"),
    ],
)
async def test_creating_a_channel_with_a_broken_name_is_refused_with_the_code_for_how_it_is_broken(
    slack: Intercepted, name: str, code: str
) -> None:
    """DOCUMENTED: "lowercase letters, numbers, hyphens, and underscores, and must be 80 characters or less", and a
    code for an empty name, one too long, one with special or upper case characters, one of only punctuation.
    https://docs.slack.dev/reference/methods/conversations.create"""
    answer = await refusal(slack.asynchronous().conversations_create(name=name))

    assert answer["error"] == code


async def test_a_name_of_eighty_characters_is_accepted(slack: Intercepted) -> None:
    """DOCUMENTED: eighty is "80 characters or less". https://docs.slack.dev/reference/methods/conversations.create"""
    created = data(await slack.asynchronous().conversations_create(name="x" * 80))["channel"]

    assert created["name"] == "x" * 80


async def test_creating_a_channel_under_a_name_in_use_is_refused_name_taken(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `name_taken` when the name is in use. https://docs.slack.dev/reference/methods/conversations.create"""
    workspace.public_channel("taken")

    answer = await refusal(slack.asynchronous().conversations_create(name="taken"))

    assert answer["error"] == "name_taken"


async def test_creating_a_channel_under_the_name_of_an_archived_one_is_refused_by_name(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED GAP: the page says only that a channel "cannot be created with the given name"; whether an archived
    channel keeps its name is not said, so the fake refuses it 501."""
    workspace.public_channel("old-news", archived=True)

    said = await not_served(slack.asynchronous().conversations_create(name="old-news"))

    assert "old-news" in said


async def test_a_name_of_no_letter_and_no_punctuation_is_refused_by_name(slack: Intercepted) -> None:
    """DOCUMENTED GAP: the page gives no code for a name of only spaces."""
    said = await not_served(slack.asynchronous().conversations_create(name="   "))

    assert "no page gives a code" in said


# ---------------------------------------------------------------------- join


async def test_joining_a_public_channel_adds_the_app_to_its_members(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: joins a user to an existing conversation and returns the conversation object.
    https://docs.slack.dev/reference/methods/conversations.join"""
    channel = workspace.channel_without_the_app("watercooler", members=["iris"])
    sdk = slack.asynchronous()

    joined = data(await sdk.conversations_join(channel=channel))

    assert joined["channel"]["id"] == channel and joined["channel"]["is_member"] is True
    assert "warning" not in joined
    assert sorted(await members(slack, channel)) == sorted([IRIS, BOT_USER_ID])
    assert data(await sdk.conversations_info(channel=channel))["channel"]["is_member"] is True


async def test_joining_a_channel_the_app_is_in_warns_already_in_channel(slack: Intercepted) -> None:
    """DOCUMENTED: "If the calling token has already joined, it'll warn you about it too" with `warning` and
    `response_metadata.warnings` of `already_in_channel`. https://docs.slack.dev/reference/methods/conversations.join"""
    joined = data(await slack.asynchronous().conversations_join(channel=GENERAL))

    assert joined["ok"] is True
    assert joined["warning"] == "already_in_channel"
    assert joined["response_metadata"] == {"warnings": ["already_in_channel"]}


async def test_joining_an_archived_channel_is_refused_is_archived(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: `is_archived`. https://docs.slack.dev/reference/methods/conversations.join"""
    channel = workspace.public_channel("old", archived=True)

    answer = await refusal(slack.asynchronous().conversations_join(channel=channel))

    assert answer["error"] == "is_archived"


async def test_joining_a_channel_nobody_made_is_refused_channel_not_found(slack: Intercepted) -> None:
    """DOCUMENTED: `channel_not_found`. https://docs.slack.dev/reference/methods/conversations.join"""
    answer = await refusal(slack.asynchronous().conversations_join(channel="C0NOSUCH"))

    assert answer["error"] == "channel_not_found"


async def test_joining_a_direct_message_is_refused_by_name(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED GAP: the page names no code for a direct message."""
    said = await not_served(slack.asynchronous().conversations_join(channel=workspace.dm("iris")))

    assert "conversations.join" in said


# ---------------------------------------------------------------------- invite


async def test_inviting_members_adds_them_and_answers_the_channel(slack: Intercepted) -> None:
    """DOCUMENTED: invites users to a channel the caller is in and answers the conversation object.
    https://docs.slack.dev/reference/methods/conversations.invite"""
    channel = await made(slack)
    sdk = slack.asynchronous()

    invited = data(await sdk.conversations_invite(channel=channel, users=f"{IRIS},{TOMAS}"))

    assert invited["channel"]["id"] == channel
    assert sorted(await members(slack, channel)) == sorted([BOT_USER_ID, IRIS, TOMAS])
    assert data(await sdk.conversations_info(channel=channel, include_num_members=True))["channel"]["num_members"] == 3


async def test_inviting_oneself_is_refused_cant_invite_self_and_names_the_user(slack: Intercepted) -> None:
    """DOCUMENTED: `cant_invite_self`, with the user in `errors`.
    https://docs.slack.dev/reference/methods/conversations.invite"""
    channel = await made(slack)

    answer = await refusal(slack.asynchronous().conversations_invite(channel=channel, users=BOT_USER_ID))

    assert answer["error"] == "cant_invite_self"
    assert answer["errors"] == [{"user": BOT_USER_ID, "ok": False, "error": "cant_invite_self"}]


async def test_several_bad_users_are_all_listed_and_the_first_is_the_error_and_nobody_is_invited(
    slack: Intercepted,
) -> None:
    """DOCUMENTED: "even valid users ... will not be invited if any of the user invites fail", and the response lists
    each failing user. https://docs.slack.dev/reference/methods/conversations.invite"""
    channel = await made(slack)

    answer = await refusal(
        slack.asynchronous().conversations_invite(channel=channel, users=f"U0NOSUCH,{BOT_USER_ID},{IRIS}")
    )

    assert answer["error"] == "user_not_found"
    assert answer["errors"] == [
        {"user": "U0NOSUCH", "ok": False, "error": "user_not_found"},
        {"user": BOT_USER_ID, "ok": False, "error": "cant_invite_self"},
    ]
    assert await members(slack, channel) == [BOT_USER_ID]


async def test_inviting_a_member_who_is_in_is_refused_already_in_channel(slack: Intercepted) -> None:
    """DOCUMENTED: `already_in_channel`. https://docs.slack.dev/reference/methods/conversations.invite"""
    channel = await made(slack)
    await slack.asynchronous().conversations_invite(channel=channel, users=IRIS)

    answer = await refusal(slack.asynchronous().conversations_invite(channel=channel, users=IRIS))

    assert answer["error"] == "already_in_channel"


async def test_inviting_from_a_channel_the_app_is_not_in_is_refused_not_in_channel(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: "The calling user must be a member of the channel"; `not_in_channel`.
    https://docs.slack.dev/reference/methods/conversations.invite"""
    channel = workspace.channel_without_the_app("elsewhere", members=["iris"])

    answer = await refusal(slack.asynchronous().conversations_invite(channel=channel, users=TOMAS))

    assert answer["error"] == "not_in_channel"


async def test_inviting_into_an_archived_channel_is_refused_is_archived(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `is_archived`. https://docs.slack.dev/reference/methods/conversations.invite"""
    channel = workspace.public_channel("old", archived=True)

    answer = await refusal(slack.asynchronous().conversations_invite(channel=channel, users=TOMAS))

    assert answer["error"] == "is_archived"


async def test_inviting_nobody_is_refused_no_user(slack: Intercepted) -> None:
    """DOCUMENTED: `no_user`, "No value was passed for `users`". https://docs.slack.dev/reference/methods/conversations.invite"""
    channel = await made(slack)

    answer = await refusal(slack.asynchronous().conversations_invite(channel=channel, users=""))

    assert answer["error"] == "no_user"


async def test_force_with_an_invalid_user_is_refused_by_name(slack: Intercepted) -> None:
    """DOCUMENTED GAP: `force` continues "inviting the valid ones", and the page shows no success or error answer
    for it, so the fake refuses it 501."""
    channel = await made(slack)

    said = await not_served(
        slack.asynchronous().conversations_invite(channel=channel, users=f"U0NOSUCH,{IRIS}", force=True)
    )

    assert "force" in said
    assert await members(slack, channel) == [BOT_USER_ID]


async def test_force_with_only_valid_users_invites_them(slack: Intercepted) -> None:
    """DOCUMENTED: `force` changes nothing when no user is invalid. https://docs.slack.dev/reference/methods/conversations.invite"""
    channel = await made(slack)

    await slack.asynchronous().conversations_invite(channel=channel, users=f"{IRIS},{TOMAS}", force=True)

    assert sorted(await members(slack, channel)) == sorted([BOT_USER_ID, IRIS, TOMAS])


# ---------------------------------------------------------------------- kick


async def test_kicking_a_member_removes_them_from_the_channel(slack: Intercepted) -> None:
    """DOCUMENTED: removes a user from a channel; `{"ok": true, "errors": {}}`.
    https://docs.slack.dev/reference/methods/conversations.kick"""
    channel = await made(slack)
    sdk = slack.asynchronous()
    await sdk.conversations_invite(channel=channel, users=f"{IRIS},{TOMAS}")

    kicked = data(await sdk.conversations_kick(channel=channel, user=TOMAS))

    assert kicked == {"ok": True, "errors": {}}
    assert sorted(await members(slack, channel)) == sorted([BOT_USER_ID, IRIS])


async def test_kicking_oneself_is_refused_cant_kick_self(slack: Intercepted) -> None:
    """DOCUMENTED: `cant_kick_self`. https://docs.slack.dev/reference/methods/conversations.kick"""
    channel = await made(slack)

    answer = await refusal(slack.asynchronous().conversations_kick(channel=channel, user=BOT_USER_ID))

    assert answer["error"] == "cant_kick_self"


async def test_kicking_from_general_is_refused_cant_kick_from_general(slack: Intercepted) -> None:
    """DOCUMENTED: `cant_kick_from_general`. https://docs.slack.dev/reference/methods/conversations.kick"""
    answer = await refusal(slack.asynchronous().conversations_kick(channel=GENERAL, user=IRIS))

    assert answer["error"] == "cant_kick_from_general"


async def test_kicking_someone_who_is_not_in_is_refused_not_in_channel(slack: Intercepted) -> None:
    """DOCUMENTED: `not_in_channel`, "User was not in the channel". https://docs.slack.dev/reference/methods/conversations.kick"""
    channel = await made(slack)

    answer = await refusal(slack.asynchronous().conversations_kick(channel=channel, user=IRIS))

    assert answer["error"] == "not_in_channel"


@pytest.mark.parametrize(("user", "code"), [("U0NOSUCH", "user_not_found"), ("", "no_user")])
async def test_kicking_nobody_or_a_stranger_is_refused_with_the_code_for_it(
    slack: Intercepted, user: str, code: str
) -> None:
    """DOCUMENTED: `user_not_found` for an invalid `user`, `no_user` for none.
    https://docs.slack.dev/reference/methods/conversations.kick"""
    channel = await made(slack)

    answer = await refusal(slack.asynchronous().conversations_kick(channel=channel, user=user))

    assert answer["error"] == code


# ---------------------------------------------------------------------- leave


async def test_leaving_removes_the_app_from_the_channel(slack: Intercepted) -> None:
    """DOCUMENTED: leaves a conversation; `{"ok": true}`. https://docs.slack.dev/reference/methods/conversations.leave"""
    channel = await made(slack)
    sdk = slack.asynchronous()
    await sdk.conversations_invite(channel=channel, users=IRIS)

    left = data(await sdk.conversations_leave(channel=channel))

    assert left == {"ok": True}
    assert await members(slack, channel) == [IRIS]
    assert data(await sdk.conversations_info(channel=channel))["channel"]["is_member"] is False


async def test_leaving_a_channel_the_app_is_not_in_answers_a_not_in_channel_note_and_no_error(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: it "will not return an error if the user was not in the conversation": `ok` false and a
    `not_in_channel` property. https://docs.slack.dev/reference/methods/conversations.leave"""
    channel = workspace.channel_without_the_app("elsewhere", members=["iris"])

    answer = await refusal(slack.asynchronous().conversations_leave(channel=channel))

    assert answer == {"ok": False, "not_in_channel": True}


async def test_leaving_general_is_refused_cant_leave_general(slack: Intercepted) -> None:
    """DOCUMENTED: `cant_leave_general`. https://docs.slack.dev/reference/methods/conversations.leave"""
    answer = await refusal(slack.asynchronous().conversations_leave(channel=GENERAL))

    assert answer["error"] == "cant_leave_general"


async def test_the_last_member_cannot_leave(slack: Intercepted) -> None:
    """DOCUMENTED: `last_member`, "Someone else must join the channel before this user is permitted to exit".
    https://docs.slack.dev/reference/methods/conversations.leave"""
    channel = await made(slack)

    answer = await refusal(slack.asynchronous().conversations_leave(channel=channel))

    assert answer["error"] == "last_member"
    assert await members(slack, channel) == [BOT_USER_ID]


async def test_leaving_an_archived_channel_is_refused_is_archived(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: `is_archived`. https://docs.slack.dev/reference/methods/conversations.leave"""
    channel = workspace.public_channel("old", archived=True)

    answer = await refusal(slack.asynchronous().conversations_leave(channel=channel))

    assert answer["error"] == "is_archived"


# ---------------------------------------------------------------------- archive, unarchive


async def test_an_archived_channel_reads_back_archived_and_is_left_out_on_request(slack: Intercepted) -> None:
    """DOCUMENTED: archives a conversation; `exclude_archived` leaves archived channels out.
    https://docs.slack.dev/reference/methods/conversations.archive,
    https://docs.slack.dev/reference/methods/conversations.list"""
    channel = await made(slack)
    sdk = slack.asynchronous()

    assert data(await sdk.conversations_archive(channel=channel)) == {"ok": True}

    assert data(await sdk.conversations_info(channel=channel))["channel"]["is_archived"] is True
    kept = data(await sdk.conversations_list(exclude_archived=True, limit=999))["channels"]
    assert channel not in [c["id"] for c in kept]
    everything = data(await sdk.conversations_list(limit=999))["channels"]
    assert channel in [c["id"] for c in everything]
    assert (await refusal(sdk.chat_postMessage(channel=channel, text="hi")))["error"] == "is_archived"


async def test_archiving_twice_is_refused_already_archived(slack: Intercepted) -> None:
    """DOCUMENTED: `already_archived`. https://docs.slack.dev/reference/methods/conversations.archive"""
    channel = await made(slack)
    await slack.asynchronous().conversations_archive(channel=channel)

    answer = await refusal(slack.asynchronous().conversations_archive(channel=channel))

    assert answer["error"] == "already_archived"


async def test_archiving_general_is_refused_cant_archive_general(slack: Intercepted) -> None:
    """DOCUMENTED: `cant_archive_general`. https://docs.slack.dev/reference/methods/conversations.archive"""
    answer = await refusal(slack.asynchronous().conversations_archive(channel=GENERAL))

    assert answer["error"] == "cant_archive_general"


async def test_archiving_a_channel_the_app_is_not_in_is_refused_not_in_channel(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `not_in_channel`, "The token can not be found in channel".
    https://docs.slack.dev/reference/methods/conversations.archive"""
    channel = workspace.channel_without_the_app("elsewhere", members=["iris"])

    answer = await refusal(slack.asynchronous().conversations_archive(channel=channel))

    assert answer["error"] == "not_in_channel"


async def test_a_bot_token_unarchiving_is_refused_by_name(slack: Intercepted) -> None:
    """DOCUMENTED GAP: "Bot tokens (`xoxb-...`) cannot currently be used to unarchive conversations", and the page
    gives no error for it. https://docs.slack.dev/reference/methods/conversations.unarchive"""
    channel = await made(slack)
    await slack.asynchronous().conversations_archive(channel=channel)

    said = await not_served(slack.asynchronous().conversations_unarchive(channel=channel))

    assert "bot token" in said


async def test_a_user_token_unarchives_and_the_caller_is_added(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: reverses archival and "The calling user is added to the conversation".
    https://docs.slack.dev/reference/methods/conversations.unarchive"""
    channel = workspace.channel_without_the_app("old", members=["iris"], archived=True)
    sdk = slack.asynchronous("xoxp-1-2-3")

    assert data(await sdk.conversations_unarchive(channel=channel)) == {"ok": True}

    assert data(await sdk.conversations_info(channel=channel))["channel"]["is_archived"] is False
    assert sorted(await members(slack, channel)) == sorted([IRIS, BOT_USER_ID])


async def test_unarchiving_a_channel_that_is_not_archived_is_refused_not_archived(slack: Intercepted) -> None:
    """DOCUMENTED: `not_archived`. https://docs.slack.dev/reference/methods/conversations.unarchive"""
    channel = await made(slack)

    answer = await refusal(slack.asynchronous("xoxp-1-2-3").conversations_unarchive(channel=channel))

    assert answer["error"] == "not_archived"


# ---------------------------------------------------------------------- rename


async def test_a_renamed_channel_reads_back_under_its_new_name_and_remembers_the_old(slack: Intercepted) -> None:
    """DOCUMENTED: renames a conversation and answers the channel object, whose `previous_names` lists the names it
    had. https://docs.slack.dev/reference/methods/conversations.rename"""
    channel = await made(slack, "launch-room")
    sdk = slack.asynchronous()

    renamed = data(await sdk.conversations_rename(channel=channel, name="go-live"))["channel"]

    assert (renamed["id"], renamed["name"], renamed["previous_names"]) == (channel, "go-live", ["launch-room"])
    read = data(await sdk.conversations_info(channel=channel))["channel"]
    assert (read["name"], read["name_normalized"], read["previous_names"]) == ("go-live", "go-live", ["launch-room"])
    await made(slack, "launch-room")


async def test_renaming_to_a_name_in_use_is_refused_name_taken(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: `name_taken`. https://docs.slack.dev/reference/methods/conversations.rename"""
    workspace.public_channel("taken")
    channel = await made(slack)

    answer = await refusal(slack.asynchronous().conversations_rename(channel=channel, name="taken"))

    assert answer["error"] == "name_taken"


async def test_only_the_creator_may_rename_so_a_channel_made_by_someone_else_is_refused_not_authorized(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: "Only the user that originally created a channel, a Workspace Admin, or a user with the Channel
    Manager role may rename it. Others will receive a `not_authorized` error."
    https://docs.slack.dev/reference/methods/conversations.rename"""
    channel = workspace.channel_without_the_app("theirs", members=["iris"])
    await slack.asynchronous().conversations_join(channel=channel)

    answer = await refusal(slack.asynchronous().conversations_rename(channel=channel, name="mine"))

    assert answer["error"] == "not_authorized"


async def test_renaming_a_channel_the_app_is_not_in_is_refused_not_in_channel(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `not_in_channel`, "Caller is not a member of the channel".
    https://docs.slack.dev/reference/methods/conversations.rename"""
    channel = workspace.channel_without_the_app("theirs", members=["iris"])

    answer = await refusal(slack.asynchronous().conversations_rename(channel=channel, name="mine"))

    assert answer["error"] == "not_in_channel"


async def test_renaming_an_archived_channel_is_refused_is_archived(slack: Intercepted) -> None:
    """DOCUMENTED: `is_archived`. https://docs.slack.dev/reference/methods/conversations.rename"""
    channel = await made(slack)
    await slack.asynchronous().conversations_archive(channel=channel)

    answer = await refusal(slack.asynchronous().conversations_rename(channel=channel, name="other"))

    assert answer["error"] == "is_archived"


async def test_renaming_to_a_name_slack_would_modify_is_refused_by_name(slack: Intercepted) -> None:
    """DOCUMENTED GAP: the page says Slack will "validate the submitted channel name and modify it", and gives codes
    for names that fail it, without saying which names are modified or how."""
    channel = await made(slack)

    said = await not_served(slack.asynchronous().conversations_rename(channel=channel, name="Go Live"))

    assert "modifies" in said


async def test_renaming_a_direct_message_is_refused_by_name(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED GAP: "Some types of conversations cannot be renamed", and the page names none."""
    said = await not_served(slack.asynchronous().conversations_rename(channel=workspace.dm("iris"), name="x"))

    assert "conversations.rename" in said


# ---------------------------------------------------------------------- topic, purpose


async def test_a_topic_set_reads_back_with_who_set_it_and_when(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: the response is the conversation object with `topic` `value`, `creator`, `last_set`.
    https://docs.slack.dev/reference/methods/conversations.setTopic"""
    channel = await made(slack)
    sdk = slack.asynchronous()

    set_ = data(await sdk.conversations_setTopic(channel=channel, topic="Apply topically for best effects"))["channel"]

    expected = {
        "value": "Apply topically for best effects",
        "creator": BOT_USER_ID,
        "last_set": int(workspace.clock.now().timestamp()),
    }
    assert set_["topic"] == expected
    assert data(await sdk.conversations_info(channel=channel))["channel"]["topic"] == expected


async def test_a_purpose_set_answers_the_purpose_and_reads_back(slack: Intercepted) -> None:
    """DOCUMENTED: `{"ok": true, "purpose": "…"}`. https://docs.slack.dev/reference/methods/conversations.setPurpose"""
    channel = await made(slack)
    sdk = slack.asynchronous()

    set_ = data(await sdk.conversations_setPurpose(channel=channel, purpose="Anything goes!"))

    assert set_["purpose"] == "Anything goes!"
    read = data(await sdk.conversations_info(channel=channel))["channel"]["purpose"]
    assert (read["value"], read["creator"]) == ("Anything goes!", BOT_USER_ID)


@pytest.mark.parametrize("method", ["conversations_setTopic", "conversations_setPurpose"])
async def test_a_topic_or_purpose_of_more_than_250_characters_is_refused_too_long_and_250_is_kept(
    slack: Intercepted, method: str
) -> None:
    """DOCUMENTED: `too_long`, "longer than 250 characters". https://docs.slack.dev/reference/methods/conversations.setTopic,
    https://docs.slack.dev/reference/methods/conversations.setPurpose"""
    channel = await made(slack)
    argument = "topic" if method.endswith("Topic") else "purpose"
    call = getattr(slack.asynchronous(), method)

    assert (await refusal(call(channel=channel, **{argument: "x" * 251})))["error"] == "too_long"
    assert data(await call(channel=channel, **{argument: "x" * 250}))["ok"] is True


@pytest.mark.parametrize("method", ["conversations_setTopic", "conversations_setPurpose"])
async def test_a_topic_or_purpose_set_from_outside_the_channel_or_in_an_archived_one_is_refused(
    slack: Intercepted, workspace: Workspace, method: str
) -> None:
    """DOCUMENTED: "The calling user must be a member of the conversation": `not_in_channel`; and `is_archived`.
    https://docs.slack.dev/reference/methods/conversations.setTopic,
    https://docs.slack.dev/reference/methods/conversations.setPurpose"""
    argument = "topic" if method.endswith("Topic") else "purpose"
    call = getattr(slack.asynchronous(), method)
    outside = workspace.channel_without_the_app("elsewhere", members=["iris"])
    archived = workspace.public_channel("old", archived=True)

    assert (await refusal(call(channel=outside, **{argument: "x"})))["error"] == "not_in_channel"
    assert (await refusal(call(channel=archived, **{argument: "x"})))["error"] == "is_archived"


async def test_a_topic_with_none_given_is_refused_invalid_arguments(slack: Intercepted) -> None:
    """DOCUMENTED: `invalid_arguments` for "missing required field: topic".
    https://docs.slack.dev/reference/methods/conversations.setTopic"""
    channel = await made(slack)

    answer = await refusal(slack.asynchronous().api_call("conversations.setTopic", params={"channel": channel}))

    assert answer["error"] == "invalid_arguments"


async def test_a_topic_in_a_direct_message_is_refused_by_name(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED GAP: "Not all conversation types support a new topic", and the page names none."""
    said = await not_served(slack.asynchronous().conversations_setTopic(channel=workspace.dm("iris"), topic="x"))

    assert "conversations.setTopic" in said


# ---------------------------------------------------------------------- conversations.info


async def test_conversations_info_carries_the_fields_slack_computes(slack: Intercepted) -> None:
    """DOCUMENTED: the public channel example carries `unlinked`, `name_normalized`, `is_shared`, `is_frozen`,
    `is_org_shared`, `is_pending_ext_shared`, `pending_shared`, `context_team_id`, `is_ext_shared`,
    `shared_team_ids`, `pending_connected_team_ids` and `previous_names`.
    https://docs.slack.dev/reference/methods/conversations.info"""
    info = data(await slack.asynchronous().conversations_info(channel=GENERAL))["channel"]

    assert info["name_normalized"] == "general" and info["unlinked"] == 0 and info["previous_names"] == []
    assert (info["is_shared"], info["is_frozen"], info["is_org_shared"], info["is_ext_shared"]) == (False,) * 4
    assert (info["is_pending_ext_shared"], info["pending_shared"], info["pending_connected_team_ids"]) == (
        False,
        [],
        [],
    )
    assert info["context_team_id"] == state.TEAM_ID and info["shared_team_ids"] == [state.TEAM_ID]


async def test_a_direct_message_carries_none_of_a_channels_computed_fields(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: the 1:1 direct message example has `id`, `created`, `is_im`, `user`, … and none of those.
    https://docs.slack.dev/reference/methods/conversations.info"""
    info = data(await slack.asynchronous().conversations_info(channel=workspace.dm("iris")))["channel"]

    assert info["is_im"] is True
    assert not {"name_normalized", "unlinked", "context_team_id", "shared_team_ids"} & set(info)


# ---------------------------------------------------------------------- users.conversations


async def test_users_conversations_lists_what_the_app_is_in_without_membership_fields(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: lists the conversations of the token's user, by type, leaving out `is_member` and `num_members`.
    https://docs.slack.dev/reference/methods/users.conversations"""
    workspace.channel_without_the_app("elsewhere", members=["iris"])
    made_here = await made(slack, "launch-room")
    sdk = slack.asynchronous()

    publics = data(await sdk.users_conversations())["channels"]
    both = data(await sdk.users_conversations(types="public_channel,im"))["channels"]

    assert sorted(c["name"] for c in publics) == sorted(["general", "launch-room"])
    assert made_here in [c["id"] for c in publics]
    assert not any("is_member" in c or "num_members" in c for c in publics + both)
    assert sorted(c["id"] for c in both if c.get("is_im")) == sorted(workspace.dm(k) for k in ("iris", "tomas", "noor"))


async def test_users_conversations_pages_by_cursor(slack: Intercepted) -> None:
    """DOCUMENTED: `limit` (at most 999, default 100) and `response_metadata.next_cursor`, empty on the last page.
    https://docs.slack.dev/reference/methods/users.conversations"""
    for name in ("one", "two", "three"):
        await made(slack, name)
    sdk = slack.asynchronous()

    seen: list[str] = []
    cursor = ""
    pages = 0
    while True:
        page = data(await sdk.users_conversations(limit=2, cursor=cursor or None))
        seen += [c["name"] for c in page["channels"]]
        pages += 1
        cursor = page["response_metadata"]["next_cursor"]
        if not cursor:
            break

    assert sorted(seen) == sorted(["general", "one", "two", "three"]) and pages == 2


async def test_users_conversations_excludes_archived_on_request(slack: Intercepted) -> None:
    """DOCUMENTED: `exclude_archived`. https://docs.slack.dev/reference/methods/users.conversations"""
    channel = await made(slack, "gone")
    await slack.asynchronous().conversations_archive(channel=channel)
    sdk = slack.asynchronous()

    kept = data(await sdk.users_conversations(exclude_archived=True))["channels"]
    all_ = data(await sdk.users_conversations())["channels"]

    assert "gone" not in [c["name"] for c in kept] and "gone" in [c["name"] for c in all_]


async def test_users_conversations_of_another_user_lists_where_they_are_and_the_app_is_too(
    slack: Intercepted,
) -> None:
    """DOCUMENTED: `user` browses another member's conversations; "Non-public channels are restricted to those where
    the calling user shares membership". https://docs.slack.dev/reference/methods/users.conversations"""
    shared = await made(slack, "shared", is_private=True)
    await made(slack, "mine-only", is_private=True)
    await slack.asynchronous().conversations_invite(channel=shared, users=IRIS)

    found = data(await slack.asynchronous().users_conversations(user=IRIS, types="public_channel,private_channel"))

    assert sorted(c["name"] for c in found["channels"]) == ["general", "shared"]


async def test_a_type_slack_does_not_list_is_refused_invalid_types(slack: Intercepted) -> None:
    """DOCUMENTED: `invalid_types`. https://docs.slack.dev/reference/methods/users.conversations"""
    answer = await refusal(slack.asynchronous().users_conversations(types="public_channel,galaxy"))

    assert answer["error"] == "invalid_types"


async def test_a_user_slack_has_no_member_for_is_refused_user_not_found(slack: Intercepted) -> None:
    """DOCUMENTED: a `user` that is no member. https://docs.slack.dev/reference/methods/users.conversations"""
    answer = await refusal(slack.asynchronous().users_conversations(user="U0NOSUCH"))

    assert answer["error"] == "user_not_found"


async def test_excluding_muted_channels_is_refused_by_name(slack: Intercepted) -> None:
    """DOCUMENTED GAP: `exclude_muted` is an argument of the page, and the world holds no mute."""
    said = await not_served(slack.asynchronous().users_conversations(exclude_muted=True))

    assert "exclude_muted" in said


# ---------------------------------------------------------------------- events


async def received_types(agent: AgentEndpoint, count: int) -> list[str]:
    for _ in range(300):
        if len(agent.received) >= count:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    return [r.json["event"]["type"] for r in agent.received]


async def test_what_the_agent_does_to_conversations_reaches_it_as_signed_events_api_callbacks(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: a new channel sends `channel_created` and `member_joined_channel` ("also triggered when creating a
    new channel"), an invitation `member_joined_channel` with the `inviter`, a rename `channel_rename`, an archive
    `channel_archive`, leaving `member_left_channel`; each in the Events API's `event_callback`, signed.
    https://docs.slack.dev/reference/events/channel_created, https://docs.slack.dev/reference/events/member_joined_channel,
    https://docs.slack.dev/reference/events/channel_rename, https://docs.slack.dev/reference/events/channel_archive,
    https://docs.slack.dev/reference/events/member_left_channel"""
    workspace.provider.listen(agent.target(), SECRET)
    sdk = slack.asynchronous()

    channel = await made(slack, "launch-room")
    assert await received_types(agent, 2) == ["channel_created", "member_joined_channel"]
    await sdk.conversations_invite(channel=channel, users=IRIS)
    await sdk.conversations_rename(channel=channel, name="go-live")
    await sdk.conversations_leave(channel=channel)
    assert await received_types(agent, 5) == [
        "channel_created",
        "member_joined_channel",
        "member_joined_channel",
        "channel_rename",
        "member_left_channel",
    ]
    assert agent.forged == []

    created, own_join, invited, renamed, left = (r.json for r in agent.received)
    assert created["type"] == "event_callback" and created["team_id"] == state.TEAM_ID
    assert created["event"]["channel"] == {
        "id": channel,
        "name": "launch-room",
        "created": int(workspace.clock.now().timestamp()),
        "creator": BOT_USER_ID,
    }
    assert (own_join["event"]["user"], own_join["event"]["channel_type"], "inviter" in own_join["event"]) == (
        BOT_USER_ID,
        "C",
        False,
    )
    assert (invited["event"]["user"], invited["event"]["inviter"], invited["event"]["channel"]) == (
        IRIS,
        BOT_USER_ID,
        channel,
    )
    assert renamed["event"]["channel"] == {
        "id": channel,
        "name": "go-live",
        "created": created["event"]["channel"]["created"],
    }
    assert (left["event"]["user"], left["event"]["channel"], left["event"]["team"]) == (
        BOT_USER_ID,
        channel,
        state.TEAM_ID,
    )
    assert len({r.json["event_id"] for r in agent.received}) == 5


async def test_archiving_a_public_channel_sends_channel_archive_and_a_private_one_sends_nothing(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED: `channel_archive` "is sent ... when a public channel is archived".
    https://docs.slack.dev/reference/events/channel_archive"""
    public = await made(slack, "open-room")
    private = await made(slack, "closed-room", is_private=True)
    workspace.provider.listen(agent.target(), SECRET)
    sdk = slack.asynchronous()

    await sdk.conversations_archive(channel=private)
    await sdk.conversations_archive(channel=public)

    assert await received_types(agent, 1) == ["channel_archive"]
    assert agent.received[0].json["event"] == {"type": "channel_archive", "channel": public, "user": BOT_USER_ID}


async def test_a_private_channel_made_sends_only_the_membership_not_a_channel_created(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """DOCUMENTED GAP: `channel_created` is "sent to all connections for a workspace" and the page says nothing of
    private channels, which are not the workspace's to see; the fake sends it for public channels only.
    https://docs.slack.dev/reference/events/channel_created"""
    workspace.provider.listen(agent.target(), SECRET)

    await made(slack, "closed-room", is_private=True)

    assert await received_types(agent, 1) == ["member_joined_channel"]


async def test_an_agent_that_declares_no_slack_target_is_sent_nothing(
    slack: Intercepted, workspace: Workspace, agent: AgentEndpoint
) -> None:
    """The agent subscribed to nothing, so nothing is sent (`pushing`)."""
    workspace.provider.listen(None, None)

    await made(slack, "quiet-room")
    await asyncio.sleep(0.2)

    assert agent.received == []
