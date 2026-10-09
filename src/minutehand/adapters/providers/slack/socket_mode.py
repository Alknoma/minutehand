"""Slack's Socket Mode: the app opens a WebSocket to Slack and takes its events there instead of at a request URL.

The app calls `apps.connections.open` with its app-level token (`xapp-`) and is answered a `wss://` URL on
`wss-primary.slack.com`, carrying a ticket the world records. It opens that URL through the proxy, which sends the
upgrade to this provider's socket server (`SlackProvider.sockets`, served by `adapters.proxy.local`): every
connection at the URL's path is let in, whatever ticket it carries (Minutehand never refuses a credential), and told
`hello`. From then on each event the scenario pushes to an agent whose Slack target is `socket_mode` goes on its
newest connection as an `events_api` envelope, and the push waits for the app's acknowledgement
(`{"envelope_id": ...}`): unacknowledged for `ACK_WITHIN` seconds, it is sent again as a new envelope with
`retry_attempt` counted up, up to `inbound.RETRIES` more times, and then fails the agent, as a refused request does at
a request URL. Every message either way crosses the proxy and is recorded there (`Exchange.frame`).

Sources: the envelope, the acknowledgement and `hello` (https://docs.slack.dev/apis/events-api/using-socket-mode);
three seconds to acknowledge (https://docs.slack.dev/tools/bolt-python/concepts/acknowledge); three retries
(https://docs.slack.dev/apis/events-api); `retry_attempt` on an `events_api` envelope (read by `slack_sdk`'s
`SocketModeRequest`). No page gives a retry's `retry_reason` over Socket Mode, so none is sent.

The open connections of a world are held by its `Hub`, found by the world's store (`hub`): connections, never
state. The tickets handed out and spent are the state, in the world's log.
"""

from __future__ import annotations

import asyncio
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue, ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from minutehand.adapters.providers.slack import wire
from minutehand.adapters.providers.slack.manifest import MANIFEST
from minutehand.adapters.providers.slack.state import SlackWorld
from minutehand.application.refusals import AgentFailed
from minutehand.domain.scenario import Model
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, Message, Scope
from minutehand.ports.store import Store

HOST = "wss-primary.slack.com"
PATH = "/link/"
TICKETS = "socket_mode_tickets"
ACK_WITHIN = 3.0
"""Real seconds Slack waits for an app to acknowledge an envelope before it sends it again: "you only have 3 seconds
to respond" (https://docs.slack.dev/tools/bolt-python/concepts/acknowledge)."""
CONNECT_WITHIN = 30.0
"""Real seconds a push waits for the agent to have a connection open, as it starts up, before failing it."""
RECONNECT_AFTER = 3600
"""What `hello` says of when Slack will ask the app to reconnect, as the page's example gives it; nothing here asks."""


class SocketTicket(Model):
    """One `apps.connections.open` answer: the ticket its URL carries, recorded as the agent asking for a
    connection."""

    ticket: str
    app_id: str


def ticket_ref(ticket: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=f"socket_mode/{ticket}")


def open_connection(slack: SlackWorld) -> wire.ConnectionsOpen:
    """`apps.connections.open`: a ticket for one connection, recorded as the agent's."""
    ticket = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{HOST}/{slack.team.app_id}/{slack.next_seq()}"))
    slack.write(
        ticket_ref(ticket),
        SocketTicket(ticket=ticket, app_id=slack.team.app_id),
        operation=Operation.CREATE,
        actor=Actor.AGENT,
        parent=TICKETS,
        after=RecordSnapshot(resource="socket_mode", text="a Socket Mode connection was asked for"),
    )
    return wire.ConnectionsOpen(url=f"wss://{HOST}{PATH}?ticket={ticket}&app_id={slack.team.app_id}")


class Recorder(Protocol):
    """Where each send of an event is told as it ends (`inbound.sent`)."""

    def __call__(
        self, *, url: str, attempt: int, reason: str | None, status: int | None, failure: str | None, seconds: float
    ) -> object: ...


class SocketNotAcknowledged(AgentFailed):
    """The agent took no event over Socket Mode: it had no connection open, or never acknowledged the envelope."""


@dataclass(frozen=True)
class Acknowledged:
    """The app took an envelope; `answer` is the `payload` its acknowledgement carried, None for none."""

    answer: JsonValue


class Hub:
    """The Socket Mode connections open in one world, and the envelopes awaiting their acknowledgement."""

    def __init__(self) -> None:
        self._open: list[WebSocket] = []
        self._changed = asyncio.Condition()
        self._acks: dict[str, asyncio.Future[JsonValue]] = {}

    async def opened(self, socket: WebSocket) -> None:
        async with self._changed:
            self._open.append(socket)
            self._changed.notify_all()

    async def closed(self, socket: WebSocket) -> None:
        async with self._changed:
            self._open.remove(socket)

    def count(self) -> int:
        return len(self._open)

    def heard(self, text: str) -> None:
        """A message the app sent: an acknowledgement settles its envelope; anything else is only recorded."""
        try:
            ack = wire.SocketAck.model_validate_json(text)
        except ValidationError:
            return
        waiting = self._acks.pop(ack.envelope_id, None)
        if waiting is not None and not waiting.done():
            waiting.set_result(ack.payload)

    async def push(self, callback: wire.EventCallback, *, record: Recorder | None = None) -> None:
        """Send `callback` as an `events_api` envelope on the newest connection, again until it is acknowledged;
        each send told to `record` as it ends."""
        for attempt in range(PUSH_ATTEMPTS):
            envelope_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{HOST}/{callback.event_id}/{attempt}"))
            envelope = wire.SocketEnvelope(envelope_id=envelope_id, payload=callback, retry_attempt=attempt)
            began = time.monotonic()
            acked = await self._send(envelope_id, wire.envelope_body(envelope))
            if record is not None:
                record(
                    url=f"wss://{HOST}{PATH}",
                    attempt=attempt,
                    reason=None,  # Socket Mode gives an envelope's retry no reason
                    status=None,
                    failure=None if acked is not None else f"not acknowledged within {ACK_WITHIN:g}s",
                    seconds=time.monotonic() - began,
                )
            if acked is not None:
                return
        raise SocketNotAcknowledged(
            f"the agent did not acknowledge Socket Mode event {callback.event_id} ({callback.event.type}) in "
            f"{PUSH_ATTEMPTS} sends {ACK_WITHIN:g} seconds apart"
        )

    async def interact(self, payload: wire.BlockActions | wire.ViewSubmission) -> JsonValue:
        """Send an interaction as an `interactive` envelope on the newest connection and wait for the app's
        acknowledgement, whose `payload` is its answer (a `view_submission`'s `response_action`), None when it holds
        none. An interaction is sent once, as it is at a request URL, and the app that does not acknowledge it in
        time fails."""
        envelope_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{HOST}/{payload.trigger_id}"))
        envelope = wire.InteractiveEnvelope(envelope_id=envelope_id, payload=payload)
        acked = await self._send(envelope_id, wire.envelope_body(envelope))
        if acked is None:
            raise SocketNotAcknowledged(
                f"the agent did not acknowledge the Socket Mode {payload.type} {payload.trigger_id} in "
                f"{ACK_WITHIN:g} seconds"
            )
        return acked.answer

    async def _send(self, envelope_id: str, text: str) -> Acknowledged | None:
        """One envelope on the newest connection, and the app's acknowledgement; None when it came in no time."""
        socket = await self._newest()
        waiting: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self._acks[envelope_id] = waiting
        try:
            await socket.send_text(text)
            return Acknowledged(answer=await asyncio.wait_for(asyncio.shield(waiting), timeout=ACK_WITHIN))
        except (TimeoutError, WebSocketDisconnect, RuntimeError):
            return None
        finally:
            self._acks.pop(envelope_id, None)

    async def _newest(self) -> WebSocket:
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: bool(self._open)), timeout=CONNECT_WITHIN)
            except TimeoutError as e:
                raise SocketNotAcknowledged(
                    f"an event was due over Socket Mode and the agent had no connection open after {CONNECT_WITHIN:g} "
                    "seconds: it never called apps.connections.open and connected to the URL it was given"
                ) from e
            return self._open[-1]


PUSH_ATTEMPTS = 4
"""The first send and Slack's three retries (`inbound.RETRIES`)."""

_hubs: weakref.WeakKeyDictionary[Store, Hub] = weakref.WeakKeyDictionary()


def hub(world: Store) -> Hub:
    """The Socket Mode connections of the world `world` holds: one hub per world, for as long as the world is open."""
    if world not in _hubs:
        _hubs[world] = Hub()
    return _hubs[world]


def socket_app(world: Store, clock: Clock) -> ASGIApp:
    """The socket server's app: a connection at `/link/` is accepted and told `hello`, whatever ticket it carries;
    one at any other path is refused before the handshake completes. `clock` is the port's; nothing here is
    stamped."""
    connections = hub(world)

    async def app(
        scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]
    ) -> None:
        if scope["type"] != "websocket":
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        socket = WebSocket(scope, receive, send)
        if socket.url.path != PATH:
            await socket.close(code=1008)
            return
        slack = SlackWorld(world)
        app_id = socket.query_params["app_id"] if "app_id" in socket.query_params else slack.team.app_id
        await socket.accept()
        await connections.opened(socket)
        try:
            hello = wire.SocketHello(
                num_connections=connections.count(),
                debug_info=wire.SocketDebugInfo(host=HOST, approximate_connection_time=RECONNECT_AFTER),
                connection_info=wire.SocketConnectionInfo(app_id=app_id),
            )
            await socket.send_text(hello.model_dump_json())
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message["text"] if "text" in message else None
                if isinstance(text, str):
                    connections.heard(text)
        except WebSocketDisconnect:
            pass
        finally:
            await connections.closed(socket)

    return app
