"""Slack's Socket Mode, reached the way an agent reaches it: `slack_sdk`'s own `SocketModeClient`, in a process of its
own configured only by the environment Minutehand hands an agent, asks `apps.connections.open` for a URL with its
app-level token, opens the WebSocket through the proxy, and acknowledges each envelope; what people say is pushed to
it as `events_api` envelopes, and every message either way is recorded."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from minutehand.adapters.providers.slack import socket_mode, state, wire
from minutehand.adapters.providers.slack.socket_mode import SocketNotAcknowledged
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import AgentUnderTest, GoalByMessage
from minutehand.domain.errors import NotServed
from minutehand.domain.people import Delivery, InboundTarget, PersonMessage
from minutehand.domain.scenario import FormInput, PersonCommands
from minutehand.domain.world import Actor, CallOutcome, EntityKind, FrameSender, RecordedCall, RecordSnapshot
from minutehand.ports.provider import ServesSockets
from minutehand.session import _services
from tests.providers.google_workspace.proxied import Client, client_environment
from tests.providers.slack.slack_workspace import SCENARIO, START, TOKEN, Workspace, form
from tests.providers.slack.test_slack_interactions import CARD, REJECT_MODAL, tomas_presses

pytestmark = pytest.mark.timeout(90)

APP_TOKEN = "xapp-1-A0SIMULATED-1-app-level-secret"
SOCKET = InboundTarget(provider="slack", delivery=Delivery.SOCKET_MODE)

AGENT = f"""
import json, sys
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse

ACK = sys.argv[1] == "ack"
web = WebClient(token={TOKEN!r})
client = SocketModeClient(app_token={APP_TOKEN!r}, web_client=web, auto_reconnect_enabled=False)

def listener(c, request):
    if request.type != "events_api":
        return
    event = request.payload["event"]
    if ACK:
        c.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
    print(json.dumps({{"event": event["type"], "text": event.get("text"), "channel": event["channel"],
                      "retry": request.retry_attempt}}), flush=True)

client.socket_mode_request_listeners.append(listener)
client.connect()
print(json.dumps({{"connected": client.is_connected()}}), flush=True)
sys.stdin.readline()
client.close()
"""


@pytest.fixture
async def proxied(workspace: Workspace, tmp_path: Path) -> AsyncIterator[Proxy]:
    async with Proxy(Routing(Registry.installed()), workspace.store, workspace.clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(
            workspace.store, workspace.clock, {"slack": workspace.provider.app(workspace.store, workspace.clock)}
        )
        yield proxy


async def socket_agent(proxy: Proxy, *, ack: bool = True) -> Client:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        AGENT,
        "ack" if ack else "silent",
        env=client_environment(proxy),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return Client(child)


def frames(workspace: Workspace) -> list[RecordedCall]:
    return [c for c in workspace.store.calls() if c.exchange.frame is not None]


def test_it_serves_sockets(workspace: Workspace) -> None:
    assert isinstance(workspace.provider, ServesSockets)


async def test_what_a_person_says_reaches_a_socket_mode_agent_as_an_envelope_it_acknowledges(
    workspace: Workspace, proxied: Proxy
) -> None:
    agent = await socket_agent(proxied)
    assert (await agent.heard()) == {"connected": True}

    await workspace.provider.say(
        PersonMessage(person="iris", text="Please chase the pricing.", at=START),
        SOCKET,
        workspace.store,
        workspace.clock,
        secret="unused",
    )

    heard = await agent.heard()
    await agent.go()
    await agent.finished()
    assert heard == {
        "event": "message",
        "text": "Please chase the pricing.",
        "channel": workspace.dm("iris"),
        "retry": 0,
    }
    calls = [c.exchange for c in workspace.store.calls()]
    opened, upgraded = calls[0], calls[1]
    assert (opened.path, json.loads(opened.response_body or "{}")["url"].split("?")[0]) == (
        "/api/apps.connections.open",
        "wss://wss-primary.slack.com/link/",
    )
    assert (upgraded.host, upgraded.path.split("?")[0], upgraded.status) == ("wss-primary.slack.com", "/link/", 101)
    assert upgraded.outcome is CallOutcome.ANSWERED
    said = [
        (c.exchange.frame.sender, json.loads(c.exchange.request_body or c.exchange.response_body or "{}"))
        for c in frames(workspace)
        if c.exchange.frame
    ]
    assert [sender for sender, _ in said] == [FrameSender.SERVICE, FrameSender.SERVICE, FrameSender.AGENT]
    hello, envelope, ack = (message for _, message in said)
    assert hello["type"] == "hello" and hello["connection_info"]["app_id"] == workspace.slack.team.app_id
    assert (envelope["type"], envelope["retry_attempt"]) == ("events_api", 0)
    assert envelope["payload"]["event"]["text"] == "Please chase the pricing."
    assert ack == {"envelope_id": envelope["envelope_id"]}
    assert [c.exchange.frame.number for c in frames(workspace) if c.exchange.frame] == [1, 2, 3]
    tickets = [
        e for e in workspace.store.events() if isinstance(e.after, RecordSnapshot) and e.after.resource == "socket_mode"
    ]
    assert [(e.actor, e.after.text) for e in tickets if isinstance(e.after, RecordSnapshot)] == [
        (Actor.AGENT, "a Socket Mode connection was asked for"),
    ]


async def test_an_envelope_the_agent_never_acknowledges_is_sent_again_and_then_fails_the_agent_is_refused(
    workspace: Workspace, proxied: Proxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket_mode, "ACK_WITHIN", 0.3)
    agent = await socket_agent(proxied, ack=False)
    assert (await agent.heard()) == {"connected": True}

    with pytest.raises(SocketNotAcknowledged, match=r"did not acknowledge Socket Mode event .* \(message\) in 4 sends"):
        await workspace.provider.say(
            PersonMessage(person="iris", text="Anyone there?", at=START),
            SOCKET,
            workspace.store,
            workspace.clock,
            secret="unused",
        )

    retries = [(await agent.heard())["retry"] for _ in range(4)]
    await agent.go()
    await agent.finished()
    assert retries == [0, 1, 2, 3]
    sent = [json.loads(c.exchange.response_body or "{}") for c in frames(workspace)][1:]
    assert [e["retry_attempt"] for e in sent] == [0, 1, 2, 3]
    assert not any("retry_reason" in e for e in sent), "no page gives Socket Mode's retry_reason"
    assert len({e["envelope_id"] for e in sent}) == 4, "each send is an envelope with its own unique identifier"
    assert len({e["payload"]["event_id"] for e in sent}) == 1, "every send carries the one event"


async def test_an_event_due_with_no_connection_open_fails_the_agent_is_refused(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket_mode, "CONNECT_WITHIN", 0.2)
    with pytest.raises(SocketNotAcknowledged, match="had no connection open"):
        await workspace.provider.say(
            PersonMessage(person="iris", text="hello?", at=START), SOCKET, workspace.store, workspace.clock, secret="x"
        )


async def test_a_socket_url_is_let_in_whatever_ticket_it_carries(workspace: Workspace, proxied: Proxy) -> None:
    program = f"""
import json, os, sys
from slack_sdk import WebClient
from slack_sdk.socket_mode.builtin.connection import Connection
import logging
url = WebClient(token={APP_TOKEN!r}).apps_connections_open(app_token={APP_TOKEN!r})["url"]
outcomes = []
for target in (url, url, url.split("?")[0] + "?ticket=never-issued"):
    connection = Connection(url=target, logger=logging.getLogger("t"), proxy=os.environ["HTTPS_PROXY"])
    try:
        connection.connect()
        outcomes.append(connection.is_active())
    except Exception as error:
        outcomes.append(type(error).__name__)
    finally:
        connection.disconnect()
print(json.dumps({{"outcomes": outcomes}}), flush=True)
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        env=client_environment(proxied),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = Client(child)
    outcomes = (await client.heard())["outcomes"]
    await client.finished()
    assert outcomes == [True, True, True], "a used ticket and one never issued connect: no credential is refused"
    upgrades = [
        c.exchange.status
        for c in workspace.store.calls()
        if c.exchange.host == "wss-primary.slack.com" and c.exchange.frame is None
    ]
    assert upgrades == [101, 101, 101]


async def test_any_token_is_answered_on_apps_connections_open_and_an_app_level_token_on_any_method(
    workspace: Workspace, proxied: Proxy
) -> None:
    program = f"""
import json
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
def answer(call):
    try:
        return call().data["ok"]
    except SlackApiError as e:
        return e.response["error"]
say = {{"bot_on_open": answer(lambda: WebClient(token={TOKEN!r}).apps_connections_open(app_token={TOKEN!r})),
       "app_on_bot": answer(lambda: WebClient(token={APP_TOKEN!r}).auth_test())}}
print(json.dumps(say), flush=True)
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        env=client_environment(proxied),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = Client(child)
    heard = await client.heard()
    await client.finished()
    assert heard == {"bot_on_open": True, "app_on_bot": True}, "Minutehand does not enforce token types"


def test_an_inbound_target_in_socket_mode_that_names_a_url_is_refused() -> None:
    with pytest.raises(ValidationError, match="in socket mode takes events on the connection the agent opens"):
        InboundTarget(provider="slack", delivery=Delivery.SOCKET_MODE, url="http://127.0.0.1:9/events")


def test_an_inbound_target_at_a_request_url_that_names_none_is_refused() -> None:
    with pytest.raises(ValidationError, match="delivered to a request URL needs `url`"):
        InboundTarget(provider="slack")


def test_an_agent_in_socket_mode_on_a_provider_that_serves_no_sockets_is_refused() -> None:
    agent = AgentUnderTest(
        name="teams_bot",
        goal=GoalByMessage(provider="microsoft"),
        inbound=[InboundTarget(provider="microsoft", delivery=Delivery.SOCKET_MODE)],
    )
    with pytest.raises(RunRefused, match="takes microsoft's events in socket mode, and microsoft serves no sockets"):
        _services(SCENARIO, agent, Registry.installed())


# ---------------------------------------------------------------------- presses and submissions

INTERACTIVE_AGENT = f"""
import json, sys
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse

ANSWER = json.loads(sys.argv[1])
MODAL = json.loads(sys.argv[2])
web = WebClient(token={TOKEN!r})
client = SocketModeClient(app_token={APP_TOKEN!r}, web_client=web, auto_reconnect_enabled=False)

def listener(c, request):
    if request.type != "interactive":
        return
    payload = request.payload
    if payload["type"] == "block_actions":
        web.views_open(trigger_id=payload["trigger_id"], view=MODAL)
        c.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
    else:
        c.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id, payload=ANSWER))
    print(json.dumps({{"type": payload["type"], "accepts": request.accepts_response_payload,
                      "values": payload.get("view", {{}}).get("state", {{}}).get("values")}}), flush=True)

client.socket_mode_request_listeners.append(listener)
client.connect()
print(json.dumps({{"connected": client.is_connected()}}), flush=True)
sys.stdin.readline()
client.close()
"""


async def interactive_agent(proxy: Proxy, answer: dict[str, object]) -> Client:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        INTERACTIVE_AGENT,
        json.dumps(answer),
        json.dumps(REJECT_MODAL),
        env=client_environment(proxy),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return Client(child)


async def press_with_a_form(
    workspace: Workspace, client: httpx.AsyncClient, proxied: Proxy, answer: dict[str, object]
) -> Client:
    """A card to Tomas, whose Reject press fills the modal the agent opens and submits it, over Socket Mode; the agent
    (a process of its own) acknowledges each envelope and answers the submission with `answer`."""
    agent = await interactive_agent(proxied, answer)
    assert (await agent.heard()) == {"connected": True}
    posted = await form(
        client, "chat.postMessage", channel=workspace.dm("tomas"), text="Approval", blocks=json.dumps(CARD)
    )
    [asked] = [e for e in workspace.store.events() if e.entity == state.message_ref(str(posted["ts"]))]
    reply = tomas_presses(asked, "Reject", form=[FormInput(value="Too dear")])
    await workspace.provider.press(reply, SOCKET, workspace.store, workspace.clock, secret="unused")
    return agent


async def test_a_press_and_its_submission_reach_a_socket_mode_agent_as_interactive_envelopes_it_answers(
    workspace: Workspace, proxied: Proxy, client: httpx.AsyncClient
) -> None:
    """DOCUMENTED: an interaction arrives as `{payload, envelope_id, type: "interactive", accepts_response_payload}`;
    the app's acknowledgement may carry the `payload` that answers a `view_submission`, here `response_action`
    `errors`, which leaves the modal open with them.
    https://docs.slack.dev/apis/events-api/using-socket-mode ("Using interactive features")"""
    answer: dict[str, object] = {"response_action": "errors", "errors": {"rejection_reason_block": "Say more."}}
    agent = await press_with_a_form(workspace, client, proxied, answer)

    pressed, submitted = await agent.heard(), await agent.heard()
    await agent.go()
    await agent.finished()

    assert (pressed["type"], pressed["accepts"]) == ("block_actions", True)
    assert (submitted["type"], submitted["accepts"]) == ("view_submission", True)
    assert submitted["values"] == {
        "rejection_reason_block": {"rejection_reason_input": {"type": "plain_text_input", "value": "Too dear"}}
    }
    [view] = workspace.slack.bodies(EntityKind.RECORD, state.VIEWS, wire.OpenView)
    assert view.open is True and view.errors == {"rejection_reason_block": "Say more."}
    envelopes = [
        json.loads(c.exchange.response_body or "{}")
        for c in frames(workspace)
        if c.exchange.frame is not None and c.exchange.frame.sender is FrameSender.SERVICE
    ][1:]
    assert [(e["type"], e["accepts_response_payload"]) for e in envelopes] == [("interactive", True)] * 2
    assert [e["payload"]["type"] for e in envelopes] == ["block_actions", "view_submission"]
    assert len({e["envelope_id"] for e in envelopes}) == 2
    acks = [
        json.loads(c.exchange.request_body or "{}")
        for c in frames(workspace)
        if c.exchange.frame is not None and c.exchange.frame.sender is FrameSender.AGENT
    ]
    assert [a["envelope_id"] for a in acks] == [e["envelope_id"] for e in envelopes]
    assert acks[1]["payload"] == answer


async def test_an_acknowledgement_with_no_answer_closes_the_submitted_view(
    workspace: Workspace, proxied: Proxy, client: httpx.AsyncClient
) -> None:
    """DOCUMENTED: an empty acknowledgement of a `view_submission` closes the submitted view.
    https://docs.slack.dev/surfaces/modals ("Closing a view")"""
    agent = await press_with_a_form(workspace, client, proxied, {})

    await agent.heard()
    await agent.heard()
    await agent.go()
    await agent.finished()

    [view] = workspace.slack.bodies(EntityKind.RECORD, state.VIEWS, wire.OpenView)
    assert view.open is False and view.errors == {}


async def test_a_slash_command_over_socket_mode_is_still_refused_by_name(workspace: Workspace) -> None:
    """DOCUMENTED GAP: a slash command is a `slash_commands` envelope, which is not served."""
    command = PersonCommands(provider="slack", person="iris", command="/status")

    with pytest.raises(NotServed, match="slash_commands"):
        await workspace.provider.happen(command, SOCKET, workspace.store, workspace.clock, secret="unused")
