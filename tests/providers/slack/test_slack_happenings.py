"""What people do unprompted, at the moments a scenario sets, reaches the agent as Slack's Events API sends it:
signed (checked by `slack_sdk`'s own `SignatureVerifier` at the agent's endpoint), in Slack's envelope, and only
from conversations the agent's bot is in. And the workspace a scenario seeds: channels, history, threads, files,
guests and deactivated accounts."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.responses import JSONResponse, Response

from minutehand.adapters.providers.slack import inbound, state
from minutehand.adapters.providers.slack.inbound import DeliveryRefused
from minutehand.adapters.providers.slack.provider import SlackProvider, build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import (
    Account,
    Happening,
    Person,
    PersonCommands,
    PersonDeletes,
    PersonEdits,
    PersonJoins,
    PersonOpensAgent,
    PersonPosts,
    PersonReacts,
    Scenario,
    SeededChannel,
    SeededFile,
    SeededPost,
)
from minutehand.domain.world import Actor, MessageSnapshot, RecordSnapshot
from tests.providers.slack.intercepted import SECRET, AgentEndpoint, Intercepted, Received, data
from tests.providers.slack.slack_workspace import SCENARIO, START

LAUNCH = state.named_channel_id("launch")
QUIET = state.named_channel_id("quiet")

SEEDED = SCENARIO.model_copy(
    update={
        "people": [
            *SCENARIO.people,
            Person(key="gus", name="Gus Outside", email="gus@partner.example", account=Account.GUEST),
            Person(key="ward", name="Ward Gone", email="ward@example.com", account=Account.DEACTIVATED),
        ],
        "channels": [
            SeededChannel(
                provider="slack",
                name="launch",
                private=True,
                topic="Launch on the 30th",
                members=["iris", "tomas", "gus"],
                history=[
                    SeededPost(
                        by="iris",
                        text="Checklist attached",
                        ago=timedelta(days=2),
                        key="checklist",
                        files=[SeededFile(name="checklist.md", mime_type="text/markdown", text="- [ ] legal")],
                        replies=[SeededPost(by="tomas", text="Legal is mine", ago=timedelta(days=1))],
                    ),
                ],
            ),
            SeededChannel(provider="slack", name="quiet", members=["iris"], agent_member=False),
            SeededChannel(
                provider="slack",
                members=["tomas"],
                history=[SeededPost(by="tomas", text="hi bot", ago=timedelta(hours=3))],
            ),
        ],
    }
)


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[SlackProvider, SqliteStore, RunClock]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "seeded.db", "seeded", clock)
    provider = build()
    provider.seed(SEEDED, store)
    return provider, store, clock


@pytest.fixture
async def through(seeded: tuple[SlackProvider, SqliteStore, RunClock], slack: Intercepted) -> Intercepted:
    provider, store, clock = seeded
    slack.proxy.mount(store, clock, {"slack": provider.app(store, clock)})
    return slack


async def happen(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint, happening: Happening
) -> None:
    provider, store, clock = seeded
    await provider.happen(happening, agent.target(), store, clock, secret=SECRET)


def events(agent: AgentEndpoint) -> list[dict[str, Any]]:
    return [r.json["event"] for r in agent.received]


# ------------------------------------------------------------------ seeding


async def test_the_seeded_channel_its_thread_and_its_file_read_as_slack_serves_them(through: Intercepted) -> None:
    sdk = through.asynchronous()
    listed = data(await sdk.conversations_list(types="public_channel,private_channel"))["channels"]
    history = data(await sdk.conversations_history(channel=LAUNCH))["messages"]
    thread = data(await sdk.conversations_replies(channel=LAUNCH, ts=history[0]["ts"]))["messages"]
    users = {u["name"]: u for u in data(await sdk.users_list())["members"]}
    async with through.http() as http:
        content = await http.get(history[0]["files"][0]["url_private"], headers={"Authorization": "Bearer xoxb-agent"})

    assert sorted(c["name"] for c in listed) == ["general", "launch", "quiet"]
    launch = next(c for c in listed if c["name"] == "launch")
    assert launch["is_private"] is True and launch["topic"]["value"] == "Launch on the 30th"
    assert history[0]["text"] == "Checklist attached" and history[0]["reply_count"] == 1
    assert history[0]["subtype"] == "file_share" and int(float(history[0]["ts"])) == int(
        (START - timedelta(days=2)).timestamp()
    )
    assert [m["text"] for m in thread] == ["Checklist attached", "Legal is mine"]
    assert content.text == "- [ ] legal"
    assert users["gus"]["is_restricted"] is True and users["ward"]["deleted"] is True
    assert (
        state.user_id("gus")
        not in data(await sdk.conversations_members(channel=state.named_channel_id("general")))["members"]
    )
    dm = data(
        await sdk.conversations_history(channel=state.conversation_id([state.BOT_USER_ID, state.user_id("tomas")]))
    )
    assert [m["text"] for m in dm["messages"]] == ["hi bot"]


async def test_a_channel_the_agent_was_never_invited_to_is_not_its_to_read(through: Intercepted) -> None:
    from slack_sdk.errors import SlackApiError

    with pytest.raises(SlackApiError) as refused:
        await through.asynchronous().conversations_history(channel=QUIET)
    assert refused.value.response["error"] == "not_in_channel"


def test_a_happening_naming_a_post_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ValueError, match="no post 'ghost'"):
        Scenario.model_validate(
            {
                **SEEDED.model_dump(),
                "happenings": [PersonEdits(provider="slack", person="iris", post="ghost", text="x").model_dump()],
            }
        )


def test_a_happening_in_a_channel_nobody_seeded_is_refused() -> None:
    with pytest.raises(ValueError, match="no seeded channel #nowhere"):
        Scenario.model_validate(
            {
                **SEEDED.model_dump(),
                "happenings": [PersonJoins(provider="slack", person="iris", channel="nowhere").model_dump()],
            }
        )


# ------------------------------------------------------------------ events


async def test_a_mention_in_a_channel_is_a_message_and_an_app_mention_with_its_file(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint, through: Intercepted
) -> None:
    await happen(
        seeded,
        agent,
        PersonPosts(
            provider="slack",
            person="tomas",
            channel="launch",
            text="can you check the attached?",
            mentions_agent=True,
            in_thread_of="checklist",
            files=[SeededFile(name="notes.txt", text="sign by Friday")],
            key="mention",
        ),
    )

    assert agent.forged == []
    message, mention = events(agent)
    assert message["type"] == "message" and message["subtype"] == "file_share" and message["channel_type"] == "group"
    assert mention["type"] == "app_mention" and mention["text"].startswith(f"<@{state.BOT_USER_ID}>")
    assert message["thread_ts"] == mention["thread_ts"] and message["ts"] == mention["ts"]
    assert message["client_msg_id"] and message["files"][0]["name"] == "notes.txt"
    envelopes = [r.json for r in agent.received]
    assert envelopes[0]["event_id"] != envelopes[1]["event_id"]
    assert (
        envelopes[0]["team_id"] == state.TEAM_ID and envelopes[0]["authorizations"][0]["user_id"] == state.BOT_USER_ID
    )
    async with through.http() as http:
        got = await http.get(
            message["files"][0]["url_private_download"], headers={"Authorization": "Bearer xoxb-agent"}
        )
    assert got.text == "sign by Friday"


async def test_a_post_where_the_agent_is_not_reaches_the_world_and_not_the_agent(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint
) -> None:
    await happen(seeded, agent, PersonPosts(provider="slack", person="iris", channel="quiet", text="just us"))
    _, store, _ = seeded
    assert agent.received == []
    assert [
        e.after.text for e in store.events() if e.actor is Actor.PERSON and isinstance(e.after, MessageSnapshot)
    ] == ["just us"]


async def test_an_edit_and_a_delete_are_message_changed_and_message_deleted(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint
) -> None:
    await happen(seeded, agent, PersonEdits(provider="slack", person="iris", post="checklist", text="Checklist v2"))
    await happen(seeded, agent, PersonDeletes(provider="slack", person="iris", post="checklist"))

    changed, deleted = events(agent)
    assert (changed["subtype"], changed["message"]["text"], changed["previous_message"]["text"]) == (
        "message_changed",
        "Checklist v2",
        "Checklist attached",
    )
    assert changed["message"]["edited"]["user"] == state.user_id("iris")
    assert deleted["subtype"] == "message_deleted" and deleted["deleted_ts"] == changed["message"]["ts"]


async def test_editing_someone_elses_post_is_refused(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint
) -> None:
    with pytest.raises(LookupError, match="not theirs"):
        await happen(seeded, agent, PersonEdits(provider="slack", person="tomas", post="checklist", text="mine now"))


async def test_a_reaction_on_the_agents_latest_message_and_a_join_where_already_in_is_refused(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint, through: Intercepted
) -> None:
    posted = await through.asynchronous().chat_postMessage(channel=LAUNCH, text="Who has legal?")
    await happen(
        seeded, agent, PersonReacts(provider="slack", person="tomas", channel="launch", reaction="white_check_mark")
    )
    with pytest.raises(LookupError, match="already in #general"):
        await happen(seeded, agent, PersonJoins(provider="slack", person="noor", channel="general"))

    reaction = events(agent)[0]
    assert reaction == {
        "type": "reaction_added",
        "user": state.user_id("tomas"),
        "reaction": "white_check_mark",
        "item_user": state.BOT_USER_ID,
        "item": {"type": "message", "channel": LAUNCH, "ts": posted["ts"]},
        "event_ts": reaction["event_ts"],
    }
    assert len(agent.received) == 1, "noor is already in #general, so the join is refused before anything is sent"


async def test_joining_a_public_channel_is_member_joined_channel(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint, tmp_path: Path
) -> None:
    provider, _, clock = seeded
    channels = [*SEEDED.channels, SeededChannel(provider="slack", name="open", members=["iris"])]
    other = SqliteStore(tmp_path / "open.db", "open", RunClock(START))
    provider.seed(SEEDED.model_copy(update={"channels": channels}), other)
    await provider.happen(
        PersonJoins(provider="slack", person="tomas", channel="open"), agent.target(), other, clock, secret=SECRET
    )
    [joined] = events(agent)
    assert (joined["type"], joined["user"], joined["channel_type"]) == (
        "member_joined_channel",
        state.user_id("tomas"),
        "C",
    )


async def test_opening_the_home_tab_carries_the_view_the_agent_published(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint, through: Intercepted
) -> None:
    home = {"type": "home", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Nothing waiting"}}]}
    published = data(await through.asynchronous().views_publish(user_id=state.user_id("tomas"), view=home))
    await happen(seeded, agent, PersonOpensAgent(provider="slack", person="tomas"))
    [opened] = events(agent)
    assert opened["type"] == "app_home_opened" and opened["tab"] == "home"
    assert opened["view"]["id"] == published["view"]["id"]
    assert opened["channel"] == state.conversation_id([state.BOT_USER_ID, state.user_id("tomas")])


async def test_a_slash_command_is_form_fields_and_its_answers_show_where_slack_shows_them(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint, through: Intercepted
) -> None:
    async def answer(got: Received) -> Response:
        return JSONResponse({"text": "Setting up Google Drive…"})

    agent.answer = answer
    await happen(
        seeded,
        agent,
        PersonCommands(provider="slack", person="iris", command="/setup", text="google", channel="launch"),
    )
    fields = agent.received[0].form
    async with through.http() as http:
        later = await http.post(
            fields["response_url"], json={"response_type": "in_channel", "text": "Drive connected."}
        )

    assert (fields["command"], fields["text"], fields["user_id"], fields["channel_id"]) == (
        "/setup",
        "google",
        state.user_id("iris"),
        LAUNCH,
    )
    assert fields["trigger_id"] and fields["response_url"].startswith("https://hooks.slack.com/commands/")
    assert later.status_code == 200
    _, store, _ = seeded
    shown = [
        (e.after.text, e.after.recipient_emails)
        for e in store.events()
        if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
    ]
    assert shown[0] == ("Setting up Google Drive…", ["iris@example.com"]), "a command's answer is shown to its runner"
    assert shown[1][0] == "Drive connected." and set(shown[1][1]) == {
        "iris@example.com",
        "tomas@example.com",
        "gus@partner.example",
    }
    [ran] = [
        e.after for e in store.events() if isinstance(e.after, RecordSnapshot) and e.after.resource == "slash_commands"
    ]
    assert ran.text == "/setup google"


# ------------------------------------------------------------------ delivery


async def test_an_event_the_agent_refuses_is_sent_again_with_slacks_retry_headers_then_fails_it(
    seeded: tuple[SlackProvider, SqliteStore, RunClock], agent: AgentEndpoint
) -> None:
    agent.status = 500
    with pytest.raises(DeliveryRefused) as refused:
        await happen(seeded, agent, PersonPosts(provider="slack", person="tomas", text="are you there?"))
    assert refused.value.status == 500
    retries = [(r.headers.get("x-slack-retry-num"), r.headers.get("x-slack-retry-reason")) for r in agent.received]
    assert retries == [(None, None), ("1", "http_error"), ("2", "http_error"), ("3", "http_error")]
    assert len({r.body for r in agent.received}) == 1, "a retry is the same event"


async def test_url_verification_wants_the_challenge_back(agent: AgentEndpoint) -> None:
    async def echo(got: Received) -> Response:
        return JSONResponse({"challenge": got.json["challenge"]})

    agent.answer = echo
    await inbound.verify_url(agent.target(), SECRET, "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P")
    assert agent.received[0].json["type"] == "url_verification"


async def test_url_verification_answered_without_the_challenge_is_refused(agent: AgentEndpoint) -> None:
    with pytest.raises(DeliveryRefused, match="challenge"):
        await inbound.verify_url(agent.target(), SECRET, "abc")
