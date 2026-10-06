"""What Slack does, as the emulator this provider replaced recorded it, asserted from stock `slack_sdk` clients
through the real proxy.

Each test is one claim. Its docstring says whether the claim is DOCUMENTED (Slack's public reference says it, and
the page is named) or OBSERVED (someone saw Slack do it; no page says so). `CLAIMS.md` beside the provider is the
table of them, and the list of the old emulator's habits that were not carried over.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.providers.slack.state import BOT_ID, BOT_USER_ID
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, Operation
from tests.providers.slack.intercepted import Intercepted, data, off_loop
from tests.providers.slack.slack_workspace import GENERAL, SCENARIO, START, Workspace, client_for, form

DIVIDER: dict[str, Any] = {"type": "divider"}
PROSE: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": "Two invoices wait on you"}}


async def refusal(call: Awaitable[AsyncSlackResponse]) -> dict[str, Any]:
    """The body of a call Slack refused: `ok` false, HTTP 200, and the code."""
    with pytest.raises(SlackApiError) as refused:
        await call
    assert refused.value.response.status_code == 200
    answer = refused.value.response.data
    assert isinstance(answer, dict) and answer["ok"] is False
    return answer


def _ids(items: list[Any]) -> list[str]:
    return [item if isinstance(item, str) else item["id"] for item in items]


def _issue_trigger(workspace: Workspace, trigger: str) -> str:
    """A trigger as a press hands one to the app, fresh at the run's start."""
    workspace.slack.write(
        state.trigger_ref(trigger),
        wire.SlackTrigger(id=trigger, user=state.user_id("iris"), issued=int(START.timestamp())),
        operation=Operation.CREATE,
        actor=Actor.SCENARIO,
        parent=state.TRIGGERS,
    )
    return trigger


# ---------------------------------------------------------------------- direct messages


async def test_opening_a_dm_with_a_member_answers_an_im_id(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: one id in `users` opens a 1:1 DM, whose id starts with D.
    https://docs.slack.dev/reference/methods/conversations.open"""
    opened = data(await slack.asynchronous().conversations_open(users=state.user_id("tomas")))

    assert opened["channel"]["id"] == workspace.dm("tomas") and opened["channel"]["id"].startswith("D")


@pytest.mark.parametrize("stranger", ["Kq83mPzLwT2vRbN6yHcXe0aJ4sF1", "ws_7c1e0d93ab55"])
async def test_opening_a_dm_with_an_id_that_is_no_member_is_refused_user_not_found(
    slack: Intercepted, stranger: str
) -> None:
    """DOCUMENTED: a value in `users` the workspace has no member for is `user_not_found`, not a new DM.
    https://docs.slack.dev/reference/methods/conversations.open"""
    answer = await refusal(slack.asynchronous().conversations_open(users=stranger))

    assert answer["error"] == "user_not_found"


async def test_one_unknown_id_in_a_group_dm_refuses_the_whole_open_user_not_found(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `user_not_found` is "value(s) passed for users was invalid" — one bad id fails the call, and
    no group DM is opened for the rest. https://docs.slack.dev/reference/methods/conversations.open"""
    sdk = slack.asynchronous()
    head = workspace.store.head()

    answer = await refusal(sdk.conversations_open(users=[state.user_id("noor"), "ws_7c1e0d93ab55"]))

    assert answer["error"] == "user_not_found"
    assert data(await sdk.conversations_list(types="mpim"))["channels"] == []
    assert [e for e in workspace.store.events() if e.operation is Operation.CREATE and e.seq > head] == []


# ---------------------------------------------------------------------- tokens and people


async def test_a_token_that_is_not_slacks_is_refused_invalid_auth(slack: Intercepted) -> None:
    """DOCUMENTED: a token not shaped like Slack's is `invalid_auth`.
    https://docs.slack.dev/reference/methods/auth.test"""
    answer = await refusal(slack.asynchronous(token="ghp_not-a-slack-token").auth_test())

    assert answer["error"] == "invalid_auth"


async def test_every_member_is_served_with_a_timezone(slack: Intercepted) -> None:
    """DOCUMENTED: a user object carries `tz` and `tz_offset`, also for a member who never set one.
    https://docs.slack.dev/reference/methods/users.info"""
    sdk = slack.asynchronous()
    lisbon = data(await sdk.users_info(user=state.user_id("tomas")))["user"]
    unset = data(await sdk.users_info(user=state.user_id("noor")))["user"]

    assert lisbon["tz"] == "Europe/Lisbon"
    assert unset["tz"] and "tz_offset" in unset


# ---------------------------------------------------------------------- channels


@pytest.mark.parametrize("channel", ["C0GHOSTROOM", "general"])
async def test_a_channel_that_is_not_an_existing_id_is_refused_channel_not_found(
    slack: Intercepted, channel: str
) -> None:
    """DOCUMENTED: `channel` takes a conversation id; an id nobody made, or a channel's name, is
    `channel_not_found` and is not provisioned. https://docs.slack.dev/reference/methods/conversations.info"""
    answer = await refusal(slack.asynchronous().conversations_info(channel=channel))

    assert answer["error"] == "channel_not_found"


async def test_a_member_id_as_the_channel_posts_into_the_dm_with_the_app(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: a user id as `channel` opens the bot's 1:1 conversation with that person if it is not open, and
    posts there. https://docs.slack.dev/reference/methods/chat.postMessage (the same page also says a U id lands in
    the person's DM with Slackbot; this provider follows the bot-DM reading, which is what callers rely on)."""
    posted = data(await slack.asynchronous().chat_postMessage(channel=state.user_id("iris"), text="Your slot is 3pm"))

    assert posted["channel"] == workspace.dm("iris") and posted["channel"].startswith("D")


async def test_a_member_id_nobody_has_as_the_channel_is_refused_channel_not_found(slack: Intercepted) -> None:
    """DOCUMENTED: a `channel` value that names nothing is `channel_not_found`.
    https://docs.slack.dev/reference/methods/chat.postMessage"""
    answer = await refusal(slack.asynchronous().chat_postMessage(channel="U0GHOSTUSER", text="anyone?"))

    assert answer["error"] == "channel_not_found"


async def test_reading_or_posting_in_a_channel_without_the_app_is_refused_not_in_channel(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: a bot that was never invited reads nothing and posts nothing there: `not_in_channel`.
    https://docs.slack.dev/reference/methods/conversations.history and
    https://docs.slack.dev/reference/methods/chat.postMessage"""
    finance = workspace.channel_without_the_app("finance", members=["iris", "noor"])
    sdk = slack.asynchronous()

    read = await refusal(sdk.conversations_history(channel=finance))
    posted = await refusal(sdk.chat_postMessage(channel=finance, text="quarter close"))

    assert read["error"] == posted["error"] == "not_in_channel"


async def test_is_member_is_the_apps_membership_not_anyone_elses(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: `is_member` says whether the caller itself is in the conversation, however many people are.
    https://docs.slack.dev/reference/objects/conversation-object"""
    finance = workspace.channel_without_the_app("finance", members=["iris", "tomas", "noor"])
    sdk = slack.asynchronous()

    joined = data(await sdk.conversations_info(channel=GENERAL))["channel"]
    outside = data(await sdk.conversations_info(channel=finance))["channel"]

    assert (joined["is_member"], outside["is_member"]) == (True, False)


# ---------------------------------------------------------------------- messages


async def test_deleting_a_message_nobody_posted_is_refused_message_not_found(slack: Intercepted) -> None:
    """DOCUMENTED: no message at that `ts` is `message_not_found`. https://docs.slack.dev/reference/methods/chat.delete"""
    answer = await refusal(slack.asynchronous().chat_delete(channel=GENERAL, ts="1700000000.000100"))

    assert answer["error"] == "message_not_found"


async def test_reacting_to_a_message_nobody_posted_is_refused_message_not_found(slack: Intercepted) -> None:
    """DOCUMENTED: no message at `channel` and `timestamp` is `message_not_found`.
    https://docs.slack.dev/reference/methods/reactions.add"""
    answer = await refusal(
        slack.asynchronous().reactions_add(channel=GENERAL, timestamp="1700000000.000100", name="tada")
    )

    assert answer["error"] == "message_not_found"


async def test_a_message_of_more_than_fifty_blocks_is_refused_invalid_blocks(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: a message holds up to 50 blocks (https://docs.slack.dev/reference/block-kit/blocks), and blocks
    Slack will not render are `invalid_blocks` (https://docs.slack.dev/reference/methods/chat.postMessage)."""
    sdk = slack.asynchronous()

    answer = await refusal(sdk.chat_postMessage(channel=GENERAL, text="digest", blocks=[DIVIDER] * 51))
    fifty = data(await sdk.chat_postMessage(channel=GENERAL, text="digest", blocks=[DIVIDER] * 50))

    assert answer["error"] == "invalid_blocks"
    assert len(fifty["message"]["blocks"]) == 50


async def test_as_user_true_leaves_the_app_as_the_author(slack: Intercepted) -> None:
    """OBSERVED: `as_user` is a legacy flag; sent form-encoded as "1" by slack_sdk, it is a boolean and never a user
    id, and a bot token's message stays the app's own, with its `bot_id`. The reference describes the flag as legacy
    (https://docs.slack.dev/reference/methods/chat.postMessage) without saying what a bot token gets."""
    sdk = slack.sync()

    posted = data(
        await off_loop(
            lambda: sdk.api_call(
                "chat.postMessage", params={"channel": GENERAL, "text": "stand-up moved", "as_user": True}
            )
        )
    )

    assert (posted["message"]["user"], posted["message"]["bot_id"]) == (BOT_USER_ID, BOT_ID)


async def test_an_update_draws_the_blocks_it_is_given(slack: Intercepted) -> None:
    """DOCUMENTED: `chat.update` with `blocks` renders those blocks in place of the old ones, and `text` is the
    fallback, written as given. https://docs.slack.dev/reference/methods/chat.update"""
    sdk = slack.asynchronous()
    sent = data(await sdk.chat_postMessage(channel=GENERAL, text="One invoice waits", blocks=[DIVIDER]))

    data(await sdk.chat_update(channel=GENERAL, ts=sent["ts"], text="Two invoices wait", blocks=[PROSE, DIVIDER]))
    shown = data(await sdk.conversations_history(channel=GENERAL, limit=1))["messages"][0]

    assert shown["ts"] == sent["ts"] and shown["text"] == "Two invoices wait"
    assert [b["type"] for b in shown["blocks"]] == ["section", "divider"]


async def test_an_update_with_text_and_no_blocks_drops_the_old_blocks(slack: Intercepted) -> None:
    """DOCUMENTED: when `text` is given and `blocks` is not, the old blocks are removed and the text is what renders;
    the old blocks survive only an update that gives neither. https://docs.slack.dev/reference/methods/chat.update"""
    sdk = slack.asynchronous()
    sent = data(await sdk.chat_postMessage(channel=GENERAL, text="Two invoices wait", blocks=[PROSE]))

    data(await sdk.chat_update(channel=GENERAL, ts=sent["ts"], text="Both invoices paid"))
    shown = data(await sdk.conversations_history(channel=GENERAL, limit=1))["messages"][0]

    assert shown["text"] == "Both invoices paid"
    assert "blocks" not in shown or shown["blocks"] == []


async def test_a_message_past_forty_thousand_characters_is_posted_and_truncated(slack: Intercepted) -> None:
    """DOCUMENTED: `chat.postMessage` does not refuse a long `text`; Slack truncates a message past 40,000
    characters, and its error list has no `msg_too_long`. https://docs.slack.dev/reference/methods/chat.postMessage"""
    sdk = slack.asynchronous()
    ledger = "".join(f"line {n:05d} reconciled\n" for n in range(2_000))
    assert len(ledger) > 40_000

    sent = data(await sdk.chat_postMessage(channel=GENERAL, text=ledger))
    shown = data(await sdk.conversations_history(channel=GENERAL, limit=1))["messages"][0]

    assert shown["ts"] == sent["ts"]
    assert shown["text"] == ledger[:40_000] and sent["message"]["text"] == ledger[:40_000]


async def test_an_update_past_four_thousand_characters_is_refused_msg_too_long(slack: Intercepted) -> None:
    """DOCUMENTED: `chat.update` lists `msg_too_long` — its `text` cannot exceed 4,000 characters — and the message
    keeps its old text. https://docs.slack.dev/reference/methods/chat.update"""
    sdk = slack.asynchronous()
    sent = data(await sdk.chat_postMessage(channel=GENERAL, text="Quarter close is on track"))

    answer = await refusal(sdk.chat_update(channel=GENERAL, ts=sent["ts"], text="q" * 4_001))
    shown_before = data(await sdk.conversations_history(channel=GENERAL, limit=1))["messages"][0]
    data(await sdk.chat_update(channel=GENERAL, ts=sent["ts"], text="r" * 4_000))
    shown = data(await sdk.conversations_history(channel=GENERAL, limit=1))["messages"][0]

    assert answer["error"] == "msg_too_long"
    assert shown_before["text"] == "Quarter close is on track"
    assert shown["text"] == "r" * 4_000


# ---------------------------------------------------------------------- history and threads


async def test_a_thread_parent_carries_its_reply_summary(slack: Intercepted) -> None:
    """DOCUMENTED: a parent read from history carries `reply_count`, `reply_users` and `latest_reply` — the one sign
    in history that a thread exists. https://docs.slack.dev/messaging/retrieving-messages"""
    sdk = slack.asynchronous()
    parent = data(await sdk.chat_postMessage(channel=GENERAL, text="Who takes the venue call?"))
    data(await sdk.chat_postMessage(channel=GENERAL, text="I can", thread_ts=parent["ts"]))
    last = data(await sdk.chat_postMessage(channel=GENERAL, text="Booked", thread_ts=parent["ts"]))

    [root] = data(await sdk.conversations_history(channel=GENERAL))["messages"]

    assert (root["ts"], root["reply_count"], root["latest_reply"]) == (parent["ts"], 2, last["ts"])
    assert root["reply_users"] == [BOT_USER_ID]


async def test_history_holds_thread_roots_and_replies_come_from_conversations_replies(slack: Intercepted) -> None:
    """OBSERVED: `conversations.history` answers the channel's top-level messages; a reply posted only to its thread
    is not among them and is read with `conversations.replies`. The reference sends readers to
    `conversations.replies` for threads (https://docs.slack.dev/reference/methods/conversations.history) without
    saying history leaves the replies out."""
    sdk = slack.asynchronous()
    parent = data(await sdk.chat_postMessage(channel=GENERAL, text="Who takes the venue call?"))
    reply = data(await sdk.chat_postMessage(channel=GENERAL, text="I can", thread_ts=parent["ts"]))

    history = data(await sdk.conversations_history(channel=GENERAL))["messages"]
    thread = data(await sdk.conversations_replies(channel=GENERAL, ts=parent["ts"]))["messages"]

    assert [m["ts"] for m in history] == [parent["ts"]]
    assert [m["ts"] for m in thread] == [parent["ts"], reply["ts"]]


async def test_history_honours_inclusive_at_latest(slack: Intercepted) -> None:
    """DOCUMENTED: `inclusive` includes the message AT `latest`; without it only messages before `latest` come back.
    https://docs.slack.dev/reference/methods/conversations.history"""
    sdk = slack.asynchronous()
    data(await sdk.chat_postMessage(channel=GENERAL, text="agenda draft"))
    card = data(await sdk.chat_postMessage(channel=GENERAL, text="agenda final"))

    at = data(await sdk.conversations_history(channel=GENERAL, latest=card["ts"], inclusive=True, limit=1))
    before = data(await sdk.conversations_history(channel=GENERAL, latest=card["ts"], inclusive=False, limit=1))

    assert [m["text"] for m in at["messages"]] == ["agenda final"]
    assert [m["text"] for m in before["messages"]] == ["agenda draft"]


# ---------------------------------------------------------------------- pagination


ListCall = Callable[[str | None], Awaitable[AsyncSlackResponse]]


async def _every_page(call: ListCall, key: str) -> list[list[str]]:
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        page = data(await call(cursor))
        pages.append(_ids(page[key]))
        cursor = page["response_metadata"]["next_cursor"]
        if not cursor:
            return pages


async def test_conversations_list_pages_by_cursor_and_excludes_archived_on_request(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `limit` bounds a page, `next_cursor` fetches the rest, and `exclude_archived` leaves archived
    channels out. https://docs.slack.dev/reference/methods/conversations.list and
    https://docs.slack.dev/apis/web-api/pagination"""
    for name in ("budget", "hiring", "offsite"):
        workspace.public_channel(name)
    shelved = workspace.public_channel("old-launch", archived=True)
    sdk = slack.asynchronous()

    pages = await _every_page(
        lambda cursor: sdk.conversations_list(types="public_channel", limit=2, cursor=cursor), "channels"
    )
    current = _ids(data(await sdk.conversations_list(types="public_channel", exclude_archived=True))["channels"])

    assert [len(p) for p in pages] == [2, 2, 1]
    every = [c for p in pages for c in p]
    assert len(set(every)) == 5 and shelved in every
    assert shelved not in current and len(current) == 4


async def test_users_list_pages_by_cursor(slack: Intercepted) -> None:
    """DOCUMENTED: `users.list` answers `limit` members and a `next_cursor` for the rest.
    https://docs.slack.dev/reference/methods/users.list"""
    sdk = slack.asynchronous()

    pages = await _every_page(lambda cursor: sdk.users_list(limit=3, cursor=cursor), "members")

    assert [len(p) for p in pages] == [3, 2]
    assert BOT_USER_ID in pages[0] + pages[1] and pages[1][-1] == "USLACKBOT"


async def test_conversations_members_pages_by_cursor(slack: Intercepted) -> None:
    """DOCUMENTED: a channel's members come a page at a time, so a caller that stops at the first page comes back
    short. https://docs.slack.dev/reference/methods/conversations.members"""
    sdk = slack.asynchronous()

    pages = await _every_page(
        lambda cursor: sdk.conversations_members(channel=GENERAL, limit=3, cursor=cursor), "members"
    )

    assert [len(p) for p in pages] == [3, 1]
    assert sorted(pages[0] + pages[1]) == sorted([BOT_USER_ID, *(state.user_id(k) for k in ("iris", "tomas", "noor"))])


# ---------------------------------------------------------------------- views


async def test_a_modal_title_past_twenty_four_characters_is_refused(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: a modal's `title` is plain text of at most 24 characters.
    https://docs.slack.dev/reference/views/modal-views"""
    trigger = _issue_trigger(workspace, "13345224609.738474920.8088930838d88f008e0")
    title = "Reschedule the quarterly review"
    assert len(title) > 24
    modal = {"type": "modal", "title": {"type": "plain_text", "text": title}, "blocks": [PROSE]}

    answer = await refusal(slack.asynchronous().views_open(trigger_id=trigger, view=modal))

    assert answer["error"] == "invalid_arguments"


async def test_a_view_of_more_than_a_hundred_blocks_is_refused_invalid_arguments(slack: Intercepted) -> None:
    """DOCUMENTED: a modal or Home tab holds up to 100 blocks (https://docs.slack.dev/reference/block-kit/blocks).
    The code is `invalid_arguments`: `views.publish` documents no `invalid_blocks`, which is what the old emulator
    answered (https://docs.slack.dev/reference/methods/views.publish)."""
    sdk = slack.asynchronous()
    home = {"type": "home", "blocks": [DIVIDER] * 101}

    answer = await refusal(sdk.views_publish(user_id=state.user_id("iris"), view=home))
    shown = data(await sdk.views_publish(user_id=state.user_id("iris"), view={**home, "blocks": [DIVIDER] * 100}))

    assert answer["error"] == "invalid_arguments"
    assert len(shown["view"]["blocks"]) == 100


SIGNED_IN = "xoxb-the-one-this-workspace-issued"


async def test_a_token_the_workspace_never_issued_is_refused_invalid_auth(tmp_path: Path) -> None:
    """DOCUMENTED: a token Slack did not issue for this workspace is `invalid_auth`, while the one it issued
    is answered. A scenario that declares a Slack sign-in names every token the workspace knows.
    https://docs.slack.dev/reference/methods/auth.test"""
    scenario = Scenario.model_validate(
        {**SCENARIO.model_dump(), "sign_ins": [{"provider": "slack", "credential": SIGNED_IN}]}
    )
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(scenario, store)
    async with client_for(provider, store, clock) as c:
        known = await form(c, "auth.test", token=SIGNED_IN)
        stranger = await form(c, "auth.test", token="xoxb-well-shaped-but-never-issued")

    assert known["ok"] is True
    assert stranger["ok"] is False and stranger["error"] == "invalid_auth"
