"""The fake held to the owner's three rules, through stock `slack_sdk` and the real proxy:

- authentication always passes: any token, or none, is answered; only world data (who is in a channel) decides what
  a caller sees;
- what the agent writes reads back as it wrote it, and the fake adds only what Slack assigns or computes;
- an argument Slack documents for a served method, which the fake does not model, is refused by name, never ignored.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Absence, Person, Scenario, SignIn
from tests.providers.slack.intercepted import Intercepted, data
from tests.providers.slack.slack_workspace import GENERAL, SCENARIO, START, Workspace, client_for, form

# ---------------------------------------------------------------------- authentication always passes


@pytest.mark.parametrize(
    "token",
    [None, "", "ghp_not-a-slack-token", "xoxb-well-shaped-but-never-issued", "xoxp-a-user-token", "xapp-1-app-level"],
)
async def test_any_token_or_none_is_answered_as_the_apps_bot(slack: Intercepted, token: str | None) -> None:
    """Slack answers `not_authed` with no token and `invalid_auth` for one it never issued
    (`data/observed/not_authed.http`, `data/observed/invalid_auth.http`); Minutehand deliberately answers both, and
    an app-level token on a bot method, as the app's bot."""
    sdk = AsyncWebClient(token=token, proxy=slack.proxy.url, ssl=slack.trust)

    who = data(await sdk.auth_test())
    posted = data(await sdk.chat_postMessage(channel=GENERAL, text="any key opens it"))

    assert (who["team_id"], who["user_id"]) == (state.TEAM_ID, state.BOT_USER_ID)
    assert posted["message"]["user"] == state.BOT_USER_ID


async def test_a_token_no_declared_sign_in_names_is_answered_too(tmp_path: Path) -> None:
    """A Slack sign-in the scenario declares no longer closes the workspace to every other token."""
    scenario = SCENARIO.model_copy(update={"sign_ins": [SignIn(provider="slack", credential="xoxb-declared")]})
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(scenario, store)
    async with client_for(provider, store, clock) as c:
        declared = await form(c, "auth.test", token="xoxb-declared")
        stranger = await form(c, "auth.test", token="xoxb-never-declared")

    assert declared["ok"] is True and stranger == declared


async def test_a_channel_the_app_is_not_in_is_refused_not_in_channel_whatever_the_token(
    slack: Intercepted, workspace: Workspace
) -> None:
    """Membership is world state, not a credential: a channel the app was never invited to stays closed."""
    closed = workspace.channel_without_the_app("leadership", members=["iris"])

    with pytest.raises(SlackApiError) as refused:
        await slack.asynchronous(token="xoxb-anything").conversations_history(channel=closed)

    assert refused.value.response["error"] == "not_in_channel"


# ---------------------------------------------------------------------- what is written reads back as written

ODD_TEXT = "Ship it? <@U0NOBODY> & <https://example.com|the doc> :tada: é中\U0001f680 *bold* `code`\n  trailing  "
BLOCKS: list[Any] = [
    {"type": "section", "block_id": "intro", "text": {"type": "mrkdwn", "text": "Two invoices wait on you"}},
    {"type": "divider", "block_id": "rule"},
    {
        "type": "actions",
        "block_id": "choose",
        "elements": [{"type": "button", "action_id": "ok", "text": {"type": "plain_text", "text": "OK"}}],
    },
]
ATTACHMENTS: list[Any] = [{"color": "#36a64f", "fallback": "f", "text": "attached", "fields": [{"title": "t"}]}]


async def test_a_posted_message_reads_back_exactly_as_written(slack: Intercepted) -> None:
    sdk = slack.asynchronous()
    posted = data(await sdk.chat_postMessage(channel=GENERAL, text=ODD_TEXT, blocks=BLOCKS, attachments=ATTACHMENTS))
    [read] = data(await sdk.conversations_history(channel=GENERAL, limit=1))["messages"]

    for message in (posted["message"], read):
        assert (message["text"], message["blocks"], message["attachments"]) == (ODD_TEXT, BLOCKS, ATTACHMENTS)


async def test_an_updated_message_reads_back_exactly_as_rewritten(slack: Intercepted) -> None:
    sdk = slack.asynchronous()
    posted = data(await sdk.chat_postMessage(channel=GENERAL, text="draft"))
    updated = data(await sdk.chat_update(channel=GENERAL, ts=posted["ts"], text=ODD_TEXT, blocks=BLOCKS))
    [read] = data(await sdk.conversations_history(channel=GENERAL, limit=1))["messages"]

    assert updated["text"] == ODD_TEXT
    for message in (updated["message"], read):
        assert (message["text"], message["blocks"]) == (ODD_TEXT, BLOCKS)


@pytest.mark.parametrize("name", ["eyes", ":eyes:", "+1::skin-tone-2"])
async def test_a_reaction_name_is_kept_as_sent(slack: Intercepted, name: str) -> None:
    sdk = slack.asynchronous()
    posted = data(await sdk.chat_postMessage(channel=GENERAL, text="react to me"))
    await sdk.reactions_add(channel=GENERAL, timestamp=posted["ts"], name=name)
    [read] = data(await sdk.conversations_history(channel=GENERAL, limit=1))["messages"]

    assert read["reactions"] == [{"name": name, "users": [state.BOT_USER_ID], "count": 1}]


async def test_a_home_tab_reads_back_every_field_it_was_published_with(slack: Intercepted) -> None:
    view = {
        "type": "home",
        "blocks": BLOCKS,
        "private_metadata": '{"step": 2} & <more>',
        "callback_id": "home-v2",
        "external_id": "home-iris",
        "submit_disabled": True,
    }
    shown = data(await slack.asynchronous().views_publish(user_id=state.user_id("iris"), view=view))["view"]

    assert {k: shown[k] for k in view} == view


# ---------------------------------------------------------------------- threads


async def test_a_reply_carries_its_parents_author_and_a_broadcast_reply_is_in_history(slack: Intercepted) -> None:
    """DOCUMENTED: a reply carries `parent_user_id` (https://docs.slack.dev/messaging/retrieving-messages#threading);
    `reply_broadcast` makes a reply "visible to everyone in the channel"
    (https://docs.slack.dev/reference/methods/chat.postMessage), served as a `thread_broadcast` with its `root`
    (https://docs.slack.dev/reference/events/message/thread_broadcast)."""
    sdk = slack.asynchronous()
    root = data(await sdk.chat_postMessage(channel=GENERAL, text="root"))
    quiet = data(await sdk.chat_postMessage(channel=GENERAL, text="quiet", thread_ts=root["ts"]))
    loud = data(await sdk.chat_postMessage(channel=GENERAL, text="loud", thread_ts=root["ts"], reply_broadcast=True))
    history = data(await sdk.conversations_history(channel=GENERAL, limit=2))["messages"]
    thread = data(await sdk.conversations_replies(channel=GENERAL, ts=root["ts"]))["messages"]

    assert quiet["message"]["parent_user_id"] == state.BOT_USER_ID
    assert [m["text"] for m in history] == ["loud", "root"]
    assert history[0]["subtype"] == "thread_broadcast" and history[0]["root"]["ts"] == root["ts"]
    assert loud["message"]["subtype"] == "thread_broadcast"
    assert [m["text"] for m in thread] == ["root", "quiet", "loud"]
    assert [m.get("parent_user_id") for m in thread] == [None, state.BOT_USER_ID, state.BOT_USER_ID]


async def test_a_thread_ts_naming_a_reply_or_no_message_is_refused_by_name(slack: Intercepted) -> None:
    """Slack's page says only "Avoid using a reply's ts value; use its parent instead" and lists no error for either
    case, so the fake refuses both by name rather than choose."""
    sdk = slack.asynchronous()
    root = data(await sdk.chat_postMessage(channel=GENERAL, text="root"))
    reply = data(await sdk.chat_postMessage(channel=GENERAL, text="reply", thread_ts=root["ts"]))

    said: list[str] = []
    for thread_ts in (reply["ts"], "1.000000"):
        with pytest.raises(SlackApiError) as refused:
            await sdk.chat_postMessage(channel=GENERAL, text="x", thread_ts=thread_ts)
        assert refused.value.response.status_code == 501
        said.append(refused.value.response["response_metadata"]["messages"][0])

    assert "names a reply" in said[0] and "names no message" in said[1]


async def test_replies_come_a_thousand_to_a_page_when_no_limit_is_given(workspace: Workspace) -> None:
    """DOCUMENTED: `conversations.replies` `limit` "Default: 1000"
    (https://docs.slack.dev/reference/methods/conversations.replies)."""
    async with client_for(workspace.provider, workspace.store, workspace.clock) as c:
        root = await form(c, "chat.postMessage", channel=GENERAL, text="root")
        for n in range(120):
            await form(c, "chat.postMessage", channel=GENERAL, text=str(n), thread_ts=str(root["ts"]))
        thread = await form(c, "conversations.replies", channel=GENERAL, ts=str(root["ts"]))

    assert len(thread["messages"]) == 121 and thread["has_more"] is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------- listings


async def test_users_list_with_no_limit_answers_every_member(tmp_path: Path) -> None:
    """DOCUMENTED: "Providing no `limit` value will result in Slack attempting to deliver you the entire result set"
    (https://docs.slack.dev/reference/methods/users.list)."""
    many = [Person(key=f"p{n:03d}", name=f"Person {n}", email=f"p{n:03d}@example.com") for n in range(130)]
    scenario = Scenario(name="crowd", goal="g", owner="p000", starts_at=START, people=many)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(scenario, store)
    async with client_for(provider, store, clock) as c:
        everyone = await form(c, "users.list")
        paged = await form(c, "users.list", limit="50")

    members = everyone["members"]
    assert isinstance(members, list) and len(members) == 132, "130 people, the app's bot and Slackbot"
    assert everyone["response_metadata"] == {"next_cursor": ""}
    assert len(paged["members"]) == 50  # type: ignore[arg-type]


async def test_include_num_members_counts_the_channels_members(slack: Intercepted) -> None:
    """DOCUMENTED: `include_num_members` (https://docs.slack.dev/reference/methods/conversations.info)."""
    sdk = slack.asynchronous()
    counted = data(await sdk.conversations_info(channel=GENERAL, include_num_members=True))["channel"]
    plain = data(await sdk.conversations_info(channel=GENERAL))["channel"]

    assert counted["num_members"] == 4, "three people and the app's bot"
    assert "num_members" not in plain


# ---------------------------------------------------------------------- what the fake no longer makes up


async def test_an_absence_shows_its_reason_as_the_status_and_nothing_the_scenario_did_not_give(tmp_path: Path) -> None:
    people = [
        SCENARIO.people[0].model_copy(update={"absences": [Absence(lasts=timedelta(days=2), reason="Parental leave")]}),
        SCENARIO.people[1].model_copy(update={"absences": [Absence(lasts=timedelta(days=2))]}),
        SCENARIO.people[2],
    ]
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO.model_copy(update={"people": people}), store)
    async with client_for(provider, store, clock) as c:
        iris = await form(c, "users.profile.get", user=state.user_id("iris"))
        tomas = await form(c, "users.profile.get", user=state.user_id("tomas"))
        dnd = await form(c, "dnd.info", user=state.user_id("tomas"))
        noor_dnd = await form(c, "dnd.info", user=state.user_id("noor"))

    shown: Any = iris["profile"]
    assert shown["status_text"] == "Parental leave" and "status_emoji" not in shown
    assert shown["status_expiration"] == int((START + timedelta(days=2)).timestamp())
    assert "status_text" not in tomas["profile"], "no reason given, no status written"  # type: ignore[operator]
    assert dnd["dnd_enabled"] is True and dnd["snooze_enabled"] is True
    assert noor_dnd == {"ok": True, "dnd_enabled": False, "snooze_enabled": False}, "no schedule is made up"


# ---------------------------------------------------------------------- arguments the fake does not serve


@pytest.mark.parametrize(
    ("method", "arguments", "named"),
    [
        ("chat.postMessage", {"channel": GENERAL, "text": "x", "username": "Robo"}, "username"),
        ("chat.postMessage", {"channel": GENERAL, "text": "x", "icon_emoji": ":robot_face:"}, "icon_emoji"),
        ("chat.postMessage", {"channel": GENERAL, "text": "x", "link_names": True}, "link_names"),
        ("chat.postMessage", {"channel": GENERAL, "text": "x", "mrkdwn": False}, "mrkdwn"),
        ("chat.postMessage", {"channel": GENERAL, "text": "x", "parse": "full"}, "parse"),
        ("chat.postMessage", {"channel": GENERAL, "text": "x", "as_user": True}, "as_user"),
        ("chat.postMessage", {"channel": GENERAL, "text": "x", "unfurl_links": True}, "unfurl_links"),
        (
            "chat.postMessage",
            {"channel": GENERAL, "text": "x", "metadata": {"event_type": "e", "event_payload": {}}},
            "metadata",
        ),
        ("chat.postEphemeral", {"channel": GENERAL, "user": "U0", "text": "x", "icon_url": "https://x"}, "icon_url"),
        ("users.list", {"include_locale": True}, "include_locale"),
        ("conversations.open", {"users": "U0", "prevent_creation": True}, "prevent_creation"),
        ("oauth.v2.access", {"grant_type": "refresh_token", "refresh_token": "r"}, "grant_type"),
    ],
)
async def test_an_argument_the_fake_does_not_model_is_refused_naming_it(
    slack: Intercepted, method: str, arguments: dict[str, Any], named: str
) -> None:
    with pytest.raises(SlackApiError) as refused:
        await slack.asynchronous().api_call(method, json=arguments)

    assert refused.value.response.status_code == 501
    said = refused.value.response["response_metadata"]["messages"][0]
    assert f"{method} with the argument {named}=" in said


async def test_an_unmodelled_argument_at_its_default_is_served(slack: Intercepted) -> None:
    posted = data(
        await slack.asynchronous().api_call(
            "chat.postMessage",
            json={"channel": GENERAL, "text": "plain", "mrkdwn": True, "as_user": False, "parse": "none"},
        )
    )
    assert posted["message"]["text"] == "plain"


def test_every_unmodelled_argument_table_names_a_served_method(workspace: Workspace) -> None:
    from minutehand.adapters.providers.slack.app import UNSERVED_ARGUMENTS, SlackApi

    assert set(UNSERVED_ARGUMENTS) <= SlackApi(workspace.store, workspace.clock).served
