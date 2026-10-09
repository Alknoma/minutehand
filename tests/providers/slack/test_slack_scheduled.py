"""Messages the agent schedules, posted when the run's clock reaches their moment.

Each test says whether the claim is DOCUMENTED, with the page; `CLAIMS.md` beside the provider is the table of them.
The dispatch table is played as the orchestrator plays it: the booking the provider makes, the clock jumped to its
moment, `deliver_booking` called.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.state import BOT_USER_ID
from minutehand.application.orchestrator import Services
from minutehand.domain.clock import Due, DueKind
from tests.providers.slack.intercepted import Intercepted, data
from tests.providers.slack.slack_workspace import GENERAL, Workspace
from tests.providers.slack.test_slack_conversations import not_served, refusal

HOUR = 3600
DAY = 24 * HOUR


class Bookings:
    """The dispatch table, as `Wakes`: what the provider booked and what it cancelled."""

    def __init__(self) -> None:
        self.booked: dict[str, Due] = {}
        self.cancelled: list[str] = []

    def book(self, due: Due) -> None:
        self.booked[due.ref] = due

    def cancel(self, ref: str) -> None:
        self.booked.pop(ref, None)
        self.cancelled.append(ref)


@pytest.fixture
def bookings(workspace: Workspace) -> Bookings:
    table = Bookings()
    workspace.provider.bind(table)
    return table


def now(workspace: Workspace) -> int:
    return int(workspace.clock.now().timestamp())


async def fire(workspace: Workspace, bookings: Bookings, ref: str) -> None:
    """The clock reaches the booking's moment and the run loop delivers it."""
    due = bookings.booked.pop(ref)
    workspace.clock.jump(due.at)
    await workspace.provider.deliver_booking(ref, workspace.store, workspace.clock)
    await workspace.provider.advance_booking(ref, workspace.store, workspace.clock)


async def history(slack: Intercepted, channel: str) -> list[dict[str, Any]]:
    found = data(await slack.asynchronous().conversations_history(channel=channel))["messages"]
    assert isinstance(found, list)
    return found


async def schedule(slack: Intercepted, workspace: Workspace, after: int, **more: Any) -> dict[str, Any]:
    return data(
        await slack.asynchronous().chat_scheduleMessage(
            channel=more.pop("channel", GENERAL), post_at=now(workspace) + after, **more
        )
    )


async def test_a_scheduled_message_is_listed_until_its_moment_and_then_posted_by_the_app(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED: it returns the `scheduled_message_id` and `post_at`; `chat.scheduledMessages.list` lists it
    (`id` the number of the `Q…` id, `channel_id`, `post_at`, `date_created`, `text`); at `post_at` it is a message.
    https://docs.slack.dev/reference/methods/chat.scheduleMessage,
    https://docs.slack.dev/reference/methods/chat.scheduledMessages.list"""
    sdk = slack.asynchronous()
    at = now(workspace) + 2 * HOUR

    scheduled = data(await sdk.chat_scheduleMessage(channel=GENERAL, post_at=at, text="Standup in five"))

    assert scheduled["channel"] == GENERAL and scheduled["post_at"] == str(at)
    ident = scheduled["scheduled_message_id"]
    assert ident.startswith("Q") and ident[1:].isdigit()
    assert scheduled["message"] == {
        "text": "Standup in five",
        "bot_id": workspace.slack.team.bot_id,
        "type": "delayed_message",
        "subtype": "bot_message",
    }
    listed = data(await sdk.chat_scheduledMessages_list())["scheduled_messages"]
    assert listed == [
        {
            "id": int(ident[1:]),
            "channel_id": GENERAL,
            "post_at": at,
            "date_created": now(workspace),
            "text": "Standup in five",
        }
    ]
    assert await history(slack, GENERAL) == []
    assert (
        bookings.booked[ident].at == datetime.fromtimestamp(at, UTC)
        and bookings.booked[ident].kind is DueKind.AGENT_WAKE
    )

    await fire(workspace, bookings, ident)

    posted = await history(slack, GENERAL)
    assert [(m["text"], m["user"], m["bot_id"]) for m in posted] == [
        ("Standup in five", BOT_USER_ID, workspace.slack.team.bot_id)
    ]
    assert posted[0]["ts"].split(".")[0] == str(at)
    assert data(await sdk.chat_scheduledMessages_list())["scheduled_messages"] == []


async def test_a_scheduled_message_keeps_its_blocks_attachments_and_thread(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED: it is posted with the arguments `chat.postMessage` takes (`blocks`, `attachments`, `thread_ts`,
    `reply_broadcast`). https://docs.slack.dev/reference/methods/chat.scheduleMessage"""
    sdk = slack.asynchronous()
    root = data(await sdk.chat_postMessage(channel=GENERAL, text="Launch thread"))["ts"]
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Checklist*"}}]
    attachments = [{"text": "Attached", "fallback": "Attached"}]

    scheduled = await schedule(
        slack,
        workspace,
        HOUR,
        text="Reminder",
        blocks=blocks,
        attachments=attachments,
        thread_ts=root,
        reply_broadcast=True,
    )
    await fire(workspace, bookings, scheduled["scheduled_message_id"])

    replies = data(await sdk.conversations_replies(channel=GENERAL, ts=root))["messages"]
    reply = replies[1]
    assert (reply["text"], reply["thread_ts"], reply["subtype"], reply["attachments"][0]["text"]) == (
        "Reminder",
        root,
        "thread_broadcast",
        "Attached",
    )
    assert reply["blocks"][0]["text"]["text"] == "*Checklist*" and reply["blocks"][0]["block_id"]


async def test_a_second_delivery_posts_nothing_more(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """Slack posts a scheduled message once, whatever a scenario's dispatch rules deliver twice."""
    ident = (await schedule(slack, workspace, HOUR, text="Once"))["scheduled_message_id"]
    await fire(workspace, bookings, ident)

    await workspace.provider.deliver_booking(ident, workspace.store, workspace.clock)

    assert [m["text"] for m in await history(slack, GENERAL)] == ["Once"]


async def test_a_message_whose_channel_was_archived_before_its_moment_is_not_posted(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED GAP: what Slack does with a scheduled message whose channel is archived by its moment is not said;
    the fake posts nothing and takes it off the list."""
    sdk = slack.asynchronous()
    channel = data(await sdk.conversations_create(name="short-lived"))["channel"]["id"]
    ident = (await schedule(slack, workspace, HOUR, channel=channel, text="Too late"))["scheduled_message_id"]
    await sdk.conversations_archive(channel=channel)

    await fire(workspace, bookings, ident)

    assert data(await sdk.chat_scheduledMessages_list())["scheduled_messages"] == []
    assert [m for m in data(await sdk.conversations_history(channel=channel))["messages"]] == []


# ---------------------------------------------------------------------- the time it can be scheduled for


async def test_a_moment_in_the_past_is_refused_time_in_past(slack: Intercepted, workspace: Workspace) -> None:
    """DOCUMENTED: `time_in_past`. https://docs.slack.dev/reference/methods/chat.scheduleMessage"""
    answer = await refusal(
        slack.asynchronous().chat_scheduleMessage(channel=GENERAL, post_at=now(workspace) - 1, text="Late")
    )

    assert answer["error"] == "time_in_past"


async def test_the_next_moment_is_refused_by_name_because_the_page_calls_it_neither_past_nor_future(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED GAP: the page says a `post_at` "in the past" is `time_in_past`, and "up to 120 days into the
    future" is allowed; this very second is neither."""
    said = await not_served(
        slack.asynchronous().chat_scheduleMessage(channel=GENERAL, post_at=now(workspace), text="Now")
    )

    assert "very second" in said


async def test_one_hundred_and_twenty_days_is_the_furthest_and_a_second_more_is_time_too_far(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED: "up to 120 days into the future"; beyond it `time_too_far`.
    https://docs.slack.dev/reference/methods/chat.scheduleMessage"""
    sdk = slack.asynchronous()

    ok = data(await sdk.chat_scheduleMessage(channel=GENERAL, post_at=now(workspace) + 120 * DAY, text="Far"))
    refused = await refusal(
        sdk.chat_scheduleMessage(channel=GENERAL, post_at=now(workspace) + 120 * DAY + 1, text="Too far")
    )

    assert ok["ok"] is True and refused["error"] == "time_too_far"


async def test_a_moment_that_is_no_number_is_refused_invalid_time(slack: Intercepted) -> None:
    """DOCUMENTED: `invalid_time`. https://docs.slack.dev/reference/methods/chat.scheduleMessage"""
    answer = await refusal(slack.asynchronous().chat_scheduleMessage(channel=GENERAL, post_at="soon", text="x"))

    assert answer["error"] == "invalid_time"


async def test_more_than_thirty_messages_within_five_minutes_to_one_channel_is_restricted_too_many(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED: "you cannot schedule more than 30 messages to post within a 5-minute window to the same channel";
    `restricted_too_many`. https://docs.slack.dev/reference/methods/chat.scheduleMessage"""
    sdk = slack.asynchronous()
    for n in range(30):
        await schedule(slack, workspace, HOUR + n, text=f"m{n}")

    answer = await refusal(sdk.chat_scheduleMessage(channel=GENERAL, post_at=now(workspace) + HOUR + 30, text="31st"))
    elsewhere = await schedule(slack, workspace, HOUR, channel=workspace.dm("iris"), text="Another conversation")
    later = await schedule(slack, workspace, HOUR + 30 * 60, text="Half an hour on")

    assert answer["error"] == "restricted_too_many"
    assert elsewhere["ok"] and later["ok"]


# ---------------------------------------------------------------------- where


async def test_a_channel_the_app_is_not_in_archived_unknown_or_given_by_name_is_refused(
    slack: Intercepted, workspace: Workspace
) -> None:
    """DOCUMENTED: `not_in_channel`, `is_archived`, `channel_not_found`, `no_text`; a channel passed by name is
    allowed by the page and refused 501 here. https://docs.slack.dev/reference/methods/chat.scheduleMessage"""
    sdk = slack.asynchronous()
    outside = workspace.channel_without_the_app("elsewhere", members=["iris"])
    archived = workspace.public_channel("old", archived=True)
    at = now(workspace) + HOUR

    assert (await refusal(sdk.chat_scheduleMessage(channel=outside, post_at=at, text="x")))["error"] == "not_in_channel"
    assert (await refusal(sdk.chat_scheduleMessage(channel=archived, post_at=at, text="x")))["error"] == "is_archived"
    assert (await refusal(sdk.chat_scheduleMessage(channel="C0NOSUCH", post_at=at, text="x")))[
        "error"
    ] == "channel_not_found"
    assert (await refusal(sdk.chat_scheduleMessage(channel=GENERAL, post_at=at)))["error"] == "no_text"
    assert "channel name" in await not_served(sdk.chat_scheduleMessage(channel="#general", post_at=at, text="x"))


async def test_a_provider_with_no_run_clock_to_post_on_refuses_to_schedule_by_name(
    slack: Intercepted, workspace: Workspace
) -> None:
    """The fake will not accept a message it cannot post: with no dispatch table bound it is refused 501."""
    said = await not_served(
        slack.asynchronous().chat_scheduleMessage(channel=GENERAL, post_at=now(workspace) + HOUR, text="x")
    )

    assert "run clock" in said


# ---------------------------------------------------------------------- list


async def test_the_list_can_be_cut_by_channel_and_by_range_and_paged(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED: `channel`, `oldest` and `latest` cut the list, `limit` and `cursor` page it, an unknown `channel`
    is `invalid_channel`. https://docs.slack.dev/reference/methods/chat.scheduledMessages.list"""
    sdk = slack.asynchronous()
    channel = data(await sdk.conversations_create(name="plans"))["channel"]["id"]
    one = await schedule(slack, workspace, HOUR, text="one")
    two = await schedule(slack, workspace, 2 * HOUR, text="two", channel=channel)
    three = await schedule(slack, workspace, 3 * HOUR, text="three")
    start = now(workspace)

    def texts(found: dict[str, Any]) -> list[str]:
        return [m["text"] for m in found["scheduled_messages"]]

    assert texts(data(await sdk.chat_scheduledMessages_list())) == ["one", "two", "three"]
    assert texts(data(await sdk.chat_scheduledMessages_list(channel=channel))) == ["two"]
    assert texts(
        data(await sdk.chat_scheduledMessages_list(oldest=str(start + HOUR + 1), latest=str(start + 3 * HOUR - 1)))
    ) == ["two"]
    first = data(await sdk.chat_scheduledMessages_list(limit=2))
    assert texts(first) == ["one", "two"] and first["response_metadata"]["next_cursor"]
    last = data(await sdk.chat_scheduledMessages_list(limit=2, cursor=first["response_metadata"]["next_cursor"]))
    assert texts(last) == ["three"] and last["response_metadata"]["next_cursor"] == ""
    assert (await refusal(sdk.chat_scheduledMessages_list(channel="C0NOSUCH")))["error"] == "invalid_channel"
    assert one["scheduled_message_id"] != three["scheduled_message_id"] != two["scheduled_message_id"]


async def test_a_range_edge_on_a_message_or_an_inverted_range_is_refused_by_name(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED GAP: the page gives no error for `oldest` not less than `latest`, and does not say whether a bound
    is inside the range."""
    sdk = slack.asynchronous()
    scheduled = await schedule(slack, workspace, HOUR, text="one")
    at = int(scheduled["post_at"])

    assert "oldest" in await not_served(sdk.chat_scheduledMessages_list(oldest=str(at), latest=str(at)))
    assert "bound" in await not_served(sdk.chat_scheduledMessages_list(oldest=str(at)))
    assert "bound" in await not_served(sdk.chat_scheduledMessages_list(latest=str(at)))


# ---------------------------------------------------------------------- delete


async def test_a_deleted_scheduled_message_is_never_posted(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED: it "deletes a pending scheduled message before it is sent".
    https://docs.slack.dev/reference/methods/chat.deleteScheduledMessage"""
    sdk = slack.asynchronous()
    ident = (await schedule(slack, workspace, HOUR, text="Never"))["scheduled_message_id"]

    deleted = data(await sdk.chat_deleteScheduledMessage(channel=GENERAL, scheduled_message_id=ident))

    assert deleted == {"ok": True} and ident in bookings.cancelled
    assert data(await sdk.chat_scheduledMessages_list())["scheduled_messages"] == []
    await workspace.provider.deliver_booking(ident, workspace.store, workspace.clock)
    assert await history(slack, GENERAL) == []


async def test_a_message_within_sixty_seconds_of_posting_or_already_posted_cannot_be_deleted(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED: "You cannot delete scheduled messages that have already been posted to Slack or that will be
    posted to Slack within 60 seconds of the delete request": `invalid_scheduled_message_id`.
    https://docs.slack.dev/reference/methods/chat.deleteScheduledMessage"""
    sdk = slack.asynchronous()
    posted = (await schedule(slack, workspace, HOUR, text="posted"))["scheduled_message_id"]
    await fire(workspace, bookings, posted)
    soon = (await schedule(slack, workspace, 30, text="soon"))["scheduled_message_id"]
    far = (await schedule(slack, workspace, 61, text="not so soon"))["scheduled_message_id"]

    for ident in (soon, posted, "Q0000000000"):
        answer = await refusal(sdk.chat_deleteScheduledMessage(channel=GENERAL, scheduled_message_id=ident))
        assert answer["error"] == "invalid_scheduled_message_id"
    assert data(await sdk.chat_deleteScheduledMessage(channel=GENERAL, scheduled_message_id=far))["ok"] is True


async def test_a_message_exactly_sixty_seconds_from_posting_is_refused_by_name(
    slack: Intercepted, workspace: Workspace, bookings: Bookings
) -> None:
    """DOCUMENTED GAP: "within 60 seconds" does not say whether 60 is within."""
    ident = (await schedule(slack, workspace, 60, text="edge"))["scheduled_message_id"]

    said = await not_served(
        slack.asynchronous().chat_deleteScheduledMessage(channel=GENERAL, scheduled_message_id=ident)
    )

    assert "60" in said


async def test_deleting_in_an_unknown_channel_is_refused_channel_not_found(slack: Intercepted) -> None:
    """DOCUMENTED: `channel_not_found`. https://docs.slack.dev/reference/methods/chat.deleteScheduledMessage"""
    answer = await refusal(
        slack.asynchronous().chat_deleteScheduledMessage(channel="C0NOSUCH", scheduled_message_id="Q1")
    )

    assert answer["error"] == "channel_not_found"


def test_slack_books_work_without_being_a_wake_scheduler(workspace: Workspace) -> None:
    """A run holds Slack to `BooksWakes` for its scheduled messages, and a `Booked` agent does not name it."""
    services = Services(
        providers=[workspace.provider],
        pushes={"slack": workspace.provider},
        schedulers={"slack": workspace.provider},
    )

    assert services.schedulers["slack"] is workspace.provider
    assert workspace.provider.manifest.books_work and not workspace.provider.manifest.books_wakes
    assert state.TEAM_ID
