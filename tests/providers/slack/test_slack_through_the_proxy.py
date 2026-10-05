"""Every Slack call a production caller makes, from stock `slack_sdk` clients (the blocking `WebClient` and the
`AsyncWebClient`) with no base URL, through the real proxy, answered by the fake in Slack's own wire format."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.providers.slack.seed import FaultSeed, RateLimited, Refused, SlackSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import ProviderSeed
from minutehand.domain.world import Actor, ControlKind, MessageAction, MessageSnapshot, Operation, RecordSnapshot
from tests.providers.slack.intercepted import Intercepted, data, off_loop
from tests.providers.slack.slack_workspace import GENERAL, SCENARIO, START, Workspace

CARD = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Send the contract to Acme?*"}},
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": "approve_op1",
                "text": {"type": "plain_text", "text": "Accept"},
                "value": "op1",
                "style": "primary",
            },
            {
                "type": "button",
                "action_id": "reject_op1",
                "text": {"type": "plain_text", "text": "Reject"},
                "value": "op1",
                "style": "danger",
            },
        ],
    },
]


async def test_the_blocking_client_reads_the_workspace_as_the_caller_does(slack: Intercepted) -> None:
    sdk = slack.sync()
    who = data(await off_loop(sdk.auth_test))
    people = data(await off_loop(lambda: sdk.users_list(limit=1000)))
    iris = data(await off_loop(lambda: sdk.users_info(user=state.user_id("iris"))))
    channels = data(
        await off_loop(
            lambda: sdk.conversations_list(types="public_channel,private_channel", exclude_archived=True, limit=1000)
        )
    )
    info = data(await off_loop(lambda: sdk.conversations_info(channel=GENERAL)))
    members = data(await off_loop(lambda: sdk.conversations_members(channel=GENERAL, limit=200)))

    assert who["team_id"] == state.TEAM_ID and who["user_id"] == state.BOT_USER_ID
    assert {m["id"] for m in people["members"]} >= {state.BOT_USER_ID, state.user_id("iris")}
    assert all(
        {"id", "name", "real_name", "is_bot", "is_app_user", "deleted", "tz", "profile"} <= m.keys()
        for m in people["members"]
    )
    assert (
        iris["user"]["profile"]["email"] == "iris@example.com" and iris["user"]["profile"]["title"] == "Programme lead"
    )
    assert [c["name"] for c in channels["channels"]] == ["general"]
    assert info["channel"]["name"] == "general" and info["channel"]["is_member"] is True
    assert state.user_id("tomas") in members["members"]
    assert people["response_metadata"]["next_cursor"] == ""


async def test_the_async_client_opens_a_dm_posts_a_card_and_reads_it_back_with_its_ids_and_profile(
    slack: Intercepted, workspace: Workspace
) -> None:
    sdk = slack.asynchronous()
    opened = data(await sdk.conversations_open(users=[state.user_id("iris")]))
    again = data(await sdk.conversations_open(users=[state.user_id("iris")]))
    posted = data(
        await sdk.chat_postMessage(
            channel=opened["channel"]["id"],
            text="Approval Required",
            blocks=CARD,
            unfurl_links=False,
            unfurl_media=False,
        )
    )
    history = data(
        await sdk.conversations_history(channel=opened["channel"]["id"], latest=posted["ts"], inclusive=True, limit=1)
    )

    assert again["channel"]["id"] == opened["channel"]["id"] and again["already_open"] is True
    [message] = history["messages"]
    assert message["bot_profile"]["name"] == state.BOT_NAME and message["bot_id"] == state.BOT_ID
    assert all("block_id" in b for b in message["blocks"]), "Slack gives every block an id"
    assert message["blocks"][1]["elements"][0]["action_id"] == "approve_op1"
    [shown] = [e for e in workspace.store.events() if e.entity == state.message_ref(posted["ts"])]
    assert isinstance(shown.after, MessageSnapshot)
    assert shown.after.actions == [
        MessageAction(action_id="approve_op1", label="Accept", value="op1", control=ControlKind.BUTTON),
        MessageAction(action_id="reject_op1", label="Reject", value="op1", control=ControlKind.BUTTON),
    ]


async def test_an_update_round_trips_its_blocks_and_an_empty_list_clears_them(slack: Intercepted) -> None:
    sdk = slack.asynchronous()
    posted = data(await sdk.chat_postMessage(channel=GENERAL, text="Thinking…"))
    data(await sdk.chat_update(channel=GENERAL, ts=posted["ts"], text="Approval Required", blocks=CARD))
    carded = data(await sdk.conversations_history(channel=GENERAL, limit=1))["messages"][0]
    cleared = data(await sdk.chat_update(channel=GENERAL, ts=posted["ts"], text="Accepted", blocks=[]))

    assert [b["type"] for b in carded["blocks"]] == ["section", "actions"] and "edited" in carded
    assert cleared["message"]["text"] == "Accepted" and cleared["message"]["blocks"] == []


async def test_threads_replies_ephemeral_delete_and_reactions_through_the_blocking_client(slack: Intercepted) -> None:
    sdk = slack.sync()
    root = data(await off_loop(lambda: sdk.chat_postMessage(channel=GENERAL, text="root", mrkdwn=False)))
    data(await off_loop(lambda: sdk.chat_postMessage(channel=GENERAL, text="reply", thread_ts=root["ts"])))
    thread = data(
        await off_loop(lambda: sdk.conversations_replies(channel=GENERAL, ts=root["ts"], limit=200, inclusive=False))
    )
    shown = data(
        await off_loop(lambda: sdk.chat_postEphemeral(channel=GENERAL, user=state.user_id("iris"), text="psst"))
    )
    data(await off_loop(lambda: sdk.reactions_add(channel=GENERAL, timestamp=root["ts"], name="eyes")))
    gone = data(await off_loop(lambda: sdk.chat_delete(channel=GENERAL, ts=root["ts"])))

    assert [m["text"] for m in thread["messages"]] == ["root", "reply"]
    assert thread["messages"][0]["reply_count"] == 1
    assert set(shown) == {"ok", "message_ts"}
    assert gone["ts"] == root["ts"]


async def test_a_deactivated_person_and_a_bot_cannot_be_dmed(
    workspace: Workspace, slack: Intercepted, tmp_path: Any
) -> None:
    from minutehand.domain.scenario import Account, Person

    scenario = SCENARIO.model_copy(
        update={
            "people": [
                *SCENARIO.people,
                Person(key="ward", name="Ward Old", email="ward@example.com", account=Account.DEACTIVATED),
                Person(key="robo", name="Deploy Bot", email="robo@example.com", account=Account.BOT),
            ]
        }
    )
    store = SqliteStore(tmp_path / "other.db", "other", RunClock(START))
    provider = build()
    provider.seed(scenario, store)
    slack.proxy.mount(store, workspace.clock, {"slack": provider.app(store, workspace.clock)})
    sdk = slack.asynchronous()
    with pytest.raises(SlackApiError) as disabled:
        await sdk.conversations_open(users=[state.user_id("ward")])
    with pytest.raises(SlackApiError) as bot:
        await sdk.conversations_open(users=[state.user_id("robo")])
    users = {u["id"]: u for u in data(await sdk.users_list())["members"]}

    assert disabled.value.response["error"] == "user_disabled" and bot.value.response["error"] == "cannot_dm_bot"
    assert users[state.user_id("ward")]["deleted"] is True
    assert users[state.user_id("robo")]["is_bot"] is True and "email" not in users[state.user_id("robo")]["profile"]


# ------------------------------------------------------------------ oauth


async def test_the_install_code_is_exchanged_for_the_bot_token_once(slack: Intercepted) -> None:
    sdk = slack.sync(token="")
    granted = data(await off_loop(lambda: sdk.oauth_v2_access(client_id="111.222", client_secret="shh", code="c0de")))
    with pytest.raises(SlackApiError) as reused:
        await off_loop(lambda: sdk.oauth_v2_access(client_id="111.222", client_secret="shh", code="c0de"))

    assert granted["team"] == {"id": state.TEAM_ID, "name": state.TEAM_NAME}
    assert granted["access_token"].startswith("xoxb-") and granted["bot_user_id"] == state.BOT_USER_ID
    assert granted["authed_user"]["id"] == state.user_id("iris"), "the scenario's owner installed the app"
    assert reused.value.response["error"] == "invalid_code"
    who = data(await off_loop(slack.sync(token=granted["access_token"]).auth_test))
    assert who["team_id"] == state.TEAM_ID


async def test_an_exchange_without_the_apps_secret_is_refused(slack: Intercepted) -> None:
    with pytest.raises(SlackApiError) as refused:
        await off_loop(lambda: slack.sync(token="").oauth_v2_access(client_id="111.222", client_secret="", code="c"))
    assert refused.value.response["error"] == "bad_client_secret"


# ------------------------------------------------------------------ views


async def test_a_home_tab_is_published_and_replaced_and_a_stale_hash_is_refused(
    slack: Intercepted, workspace: Workspace
) -> None:
    sdk = slack.asynchronous()
    home = {"type": "home", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "2 approvals waiting"}}]}
    first = data(await sdk.views_publish(user_id=state.user_id("iris"), view=home))["view"]
    second = data(await sdk.views_publish(user_id=state.user_id("iris"), view=home, hash=first["hash"]))["view"]
    with pytest.raises(SlackApiError) as stale:
        await sdk.views_publish(user_id=state.user_id("iris"), view=home, hash=first["hash"])

    assert first["id"] == second["id"] and first["hash"] != second["hash"]
    assert stale.value.response["error"] == "hash_conflict"
    written = [e for e in workspace.store.events() if e.entity == state.view_ref(first["id"])]
    assert all(isinstance(e.after, RecordSnapshot) and "2 approvals waiting" in e.after.text for e in written)


async def test_a_modal_needs_a_trigger_the_person_gave(slack: Intercepted) -> None:
    modal = {"type": "modal", "title": {"type": "plain_text", "text": "Reject"}, "blocks": []}
    with pytest.raises(SlackApiError) as refused:
        await slack.asynchronous().views_open(trigger_id="12345.98765.abcd", view=modal)
    assert refused.value.response["error"] == "invalid_trigger_id"


# ------------------------------------------------------------------ files


async def test_a_file_downloads_with_a_bot_token_and_without_one_lands_on_the_sign_in_page(
    slack: Intercepted, workspace: Workspace
) -> None:
    from minutehand.adapters.providers.slack.seed import write_file
    from minutehand.domain.scenario import SeededFile

    file = write_file(
        workspace.slack,
        SeededFile(name="brief.md", mime_type="text/markdown", text="# The brief"),
        state.user_id("iris"),
        int(START.timestamp()),
        seed="brief",
        actor=Actor.SCENARIO,
    )
    async with slack.http() as http:
        got = await http.get(file.url_private_download, headers={"Authorization": "Bearer xoxb-1"})
        anonymous = await http.get(file.url_private, follow_redirects=True)

    assert (
        got.status_code == 200 and got.text == "# The brief" and got.headers["content-type"].startswith("text/markdown")
    )
    assert anonymous.status_code == 200 and anonymous.text.startswith("<!DOCTYPE html>")


# ------------------------------------------------------------------ faults


def _faulted(workspace: Workspace, tmp_path: Any, *faults: FaultSeed) -> tuple[SqliteStore, Any]:
    seed = ProviderSeed(provider="slack", body=SlackSeed(faults=list(faults)).model_dump_json())
    scenario = SCENARIO.model_copy(update={"provider_seeds": [seed]})
    store = SqliteStore(tmp_path / "faulted.db", "faulted", RunClock(START))
    provider = build()
    provider.seed(scenario, store)
    return store, provider.app(store, workspace.clock)


async def test_a_rate_limit_answers_429_with_retry_after_and_then_passes(
    slack: Intercepted, workspace: Workspace, tmp_path: Any
) -> None:
    store, app = _faulted(
        workspace,
        tmp_path,
        FaultSeed(call="chat.postMessage", answer=RateLimited(retry_after=timedelta(seconds=7))),
    )
    slack.proxy.mount(store, workspace.clock, {"slack": app})
    sdk = slack.sync()
    with pytest.raises(SlackApiError) as limited:
        await off_loop(lambda: sdk.chat_postMessage(channel=GENERAL, text="hello"))
    passed = data(await off_loop(lambda: sdk.chat_postMessage(channel=GENERAL, text="hello")))

    assert limited.value.response.status_code == 429
    assert limited.value.response.headers["Retry-After"] == "7"
    assert limited.value.response["error"] == "ratelimited"
    assert passed["ok"] is True
    [failed] = [e for e in store.events() if isinstance(e.after, RecordSnapshot) and e.after.resource == "faults"]
    assert isinstance(failed.after, RecordSnapshot)
    assert failed.actor is Actor.SCENARIO and "chat.postMessage" in failed.after.text


async def test_blocks_rejected_on_purpose_pass_when_the_caller_retries_without_them(
    slack: Intercepted, workspace: Workspace, tmp_path: Any
) -> None:
    store, app = _faulted(
        workspace, tmp_path, FaultSeed(answer=Refused(error="invalid_blocks"), times=None, only_rich=True)
    )
    slack.proxy.mount(store, workspace.clock, {"slack": app})
    sdk = slack.asynchronous()
    with pytest.raises(SlackApiError) as rejected:
        await sdk.chat_postMessage(channel=GENERAL, text="Approval Required", blocks=CARD)
    plain = data(await sdk.chat_postMessage(channel=GENERAL, text="Approval Required"))
    with pytest.raises(SlackApiError):
        await sdk.chat_update(channel=GENERAL, ts=plain["ts"], text="x", blocks=CARD)

    assert rejected.value.response["error"] == "invalid_blocks" and plain["ok"] is True


@pytest.mark.parametrize(
    "error",
    [
        "invalid_auth",
        "token_revoked",
        "account_inactive",
        "channel_not_found",
        "not_in_channel",
        "message_not_found",
        "cant_update_message",
    ],
)
async def test_a_declared_refusal_reaches_the_sdk_as_slack_sends_it_once(
    slack: Intercepted, workspace: Workspace, tmp_path: Any, error: str
) -> None:
    store, app = _faulted(workspace, tmp_path, FaultSeed(answer=Refused(error=error)))
    slack.proxy.mount(store, workspace.clock, {"slack": app})
    sdk = slack.asynchronous()
    with pytest.raises(SlackApiError) as refused:
        await sdk.auth_test()
    assert refused.value.response["error"] == error and refused.value.response.status_code == 200
    assert data(await sdk.auth_test())["ok"] is True


async def test_a_fault_waits_for_its_moment(slack: Intercepted, workspace: Workspace, tmp_path: Any) -> None:
    store, app = _faulted(
        workspace, tmp_path, FaultSeed(answer=Refused(error="invalid_auth"), after=timedelta(hours=1))
    )
    slack.proxy.mount(store, workspace.clock, {"slack": app})
    sdk = slack.asynchronous()
    assert data(await sdk.auth_test())["ok"] is True
    workspace.clock.jump(START + timedelta(hours=2))
    with pytest.raises(SlackApiError):
        await sdk.auth_test()
    assert [e.operation for e in store.events() if e.entity == state.fault_ref(0)] == [
        Operation.CREATE,
        Operation.UPDATE,
    ]
