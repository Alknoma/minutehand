"""A person's reply enters the workspace and reaches the agent as a signed Events API request."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from slack_sdk.signature import Clock as VerifierClock
from slack_sdk.signature import SignatureVerifier

from minutehand.adapters.providers.slack import state
from minutehand.adapters.providers.slack.inbound import DeliveryRefused
from minutehand.domain.people import InboundTarget, PersonReply
from minutehand.domain.world import Actor, EntityKind, EntityRef, MessageSnapshot, Operation
from tests.providers.slack.slack_workspace import GENERAL, START, Workspace, body, form, messages_of, text_of


@dataclass
class Received:
    body: bytes
    headers: dict[str, str]


@dataclass
class Agent:
    """A real HTTP endpoint standing where the agent's Slack events URL would."""

    url: str
    received: list[Received] = field(default_factory=list)
    status: int = 200


@pytest.fixture
def agent() -> Iterator[Agent]:
    handle = Agent(url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            handle.received.append(Received(self.rfile.read(length), {k: v for k, v in self.headers.items()}))
            self.send_response(handle.status)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    handle.url = f"http://127.0.0.1:{server.server_address[1]}/slack/events"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield handle
    server.shutdown()
    server.server_close()


class SimulatedTime(VerifierClock):
    """The verifier's replay window, read on the run's clock as an agent under the run's time would."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def now(self) -> float:
        return self._workspace.clock.now().timestamp()


def _message(ts: object) -> EntityRef:
    return EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id=str(ts))


async def _asked(client: httpx.AsyncClient, workspace: Workspace, person: str) -> dict[str, object]:
    return await body(client, "chat.postMessage", channel=workspace.dm(person), text="are you in for Thursday?")


async def test_a_reply_arrives_signed_the_way_slack_signs(
    workspace: Workspace, client: httpx.AsyncClient, agent: Agent
) -> None:
    asked = await _asked(client, workspace, "tomas")
    workspace.clock.jump(START + timedelta(hours=7))
    reply = PersonReply(person="tomas", in_reply_to=_message(asked["ts"]), text="yes, count me in",
                        at=START + timedelta(hours=7))

    await workspace.provider.deliver(reply, InboundTarget(provider="slack", url=agent.url), workspace.store,
                                     workspace.clock)

    [got] = agent.received
    verifier = SignatureVerifier(workspace.provider.signing_secret, clock=SimulatedTime(workspace))
    assert verifier.is_valid_request(got.body, got.headers)
    assert not SignatureVerifier("another-secret", clock=SimulatedTime(workspace)).is_valid_request(got.body, got.headers)
    assert got.headers["X-Slack-Request-Timestamp"] == str(int((START + timedelta(hours=7)).timestamp()))
    callback = json.loads(got.body)
    assert callback["type"] == "event_callback" and callback["team_id"] == state.TEAM_ID
    event = callback["event"]
    assert (event["type"], event["channel"], event["user"], event["text"], event["channel_type"]) == (
        "message", workspace.dm("tomas"), state.user_id("tomas"), "yes, count me in", "im",
    )
    assert "thread_ts" not in event


async def test_the_reply_is_in_the_world_as_the_person(
    workspace: Workspace, client: httpx.AsyncClient, agent: Agent
) -> None:
    asked = await _asked(client, workspace, "iris")
    reply = PersonReply(person="iris", in_reply_to=_message(asked["ts"]), text="I am", at=START)
    await workspace.provider.deliver(reply, InboundTarget(provider="slack", url=agent.url), workspace.store,
                                     workspace.clock)

    written = workspace.store.events()[-1]
    assert (written.actor, written.operation, written.entity.kind) == (Actor.PERSON, Operation.CREATE, EntityKind.MESSAGE)
    assert written.after == MessageSnapshot(text="I am", channel=workspace.dm("iris"), recipient_emails=[])
    history = await form(client, "conversations.history", channel=workspace.dm("iris"))
    assert text_of(history) == ["I am", "are you in for Thursday?"]
    assert messages_of(history)[0]["user"] == state.user_id("iris") and "bot_id" not in messages_of(history)[0]
    assert json.loads(agent.received[0].body)["event"]["ts"] == messages_of(history)[0]["ts"]


async def test_a_reply_to_a_channel_message_goes_in_its_thread(
    workspace: Workspace, client: httpx.AsyncClient, agent: Agent
) -> None:
    asked = await body(client, "chat.postMessage", channel=GENERAL, text="who owns the rollback plan?")
    reply = PersonReply(person="noor", in_reply_to=_message(asked["ts"]), text="I do", at=START)
    await workspace.provider.deliver(reply, InboundTarget(provider="slack", url=agent.url), workspace.store,
                                     workspace.clock)

    event = json.loads(agent.received[0].body)["event"]
    assert (event["thread_ts"], event["channel_type"]) == (asked["ts"], "channel")
    thread = await form(client, "conversations.replies", channel=GENERAL, ts=str(asked["ts"]))
    assert text_of(thread) == ["who owns the rollback plan?", "I do"]


async def test_an_agent_that_answers_an_error_is_raised_not_swallowed(
    workspace: Workspace, client: httpx.AsyncClient, agent: Agent
) -> None:
    agent.status = 500
    asked = await _asked(client, workspace, "tomas")
    reply = PersonReply(person="tomas", in_reply_to=_message(asked["ts"]), text="yes", at=START)
    with pytest.raises(DeliveryRefused) as refused:
        await workspace.provider.deliver(reply, InboundTarget(provider="slack", url=agent.url), workspace.store,
                                         workspace.clock)
    assert refused.value.status == 500


async def test_the_secret_comes_from_the_variable_the_target_names(
    workspace: Workspace, client: httpx.AsyncClient, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_SLACK_SIGNING_SECRET", "a-secret-the-runner-chose")
    asked = await _asked(client, workspace, "tomas")
    reply = PersonReply(person="tomas", in_reply_to=_message(asked["ts"]), text="yes", at=START)
    await workspace.provider.deliver(
        reply, InboundTarget(provider="slack", url=agent.url, secret_env="AGENT_SLACK_SIGNING_SECRET"),
        workspace.store, workspace.clock,
    )
    got = agent.received[0]
    assert SignatureVerifier("a-secret-the-runner-chose", clock=SimulatedTime(workspace)).is_valid_request(
        got.body, got.headers
    )


async def test_a_secret_variable_that_is_not_set_is_refused(
    workspace: Workspace, client: httpx.AsyncClient, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_SLACK_SIGNING_SECRET", raising=False)
    asked = await _asked(client, workspace, "tomas")
    reply = PersonReply(person="tomas", in_reply_to=_message(asked["ts"]), text="yes", at=START)
    with pytest.raises(LookupError):
        await workspace.provider.deliver(
            reply, InboundTarget(provider="slack", url=agent.url, secret_env="AGENT_SLACK_SIGNING_SECRET"),
            workspace.store, workspace.clock,
        )
    assert agent.received == []


async def test_a_reply_to_a_message_that_does_not_exist_is_refused(workspace: Workspace, agent: Agent) -> None:
    reply = PersonReply(person="tomas", in_reply_to=_message("1.000001"), text="yes", at=START)
    with pytest.raises(LookupError):
        await workspace.provider.deliver(reply, InboundTarget(provider="slack", url=agent.url), workspace.store,
                                         workspace.clock)
