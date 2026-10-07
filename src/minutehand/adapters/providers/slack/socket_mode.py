"""Slack's Socket Mode: the app opens a WebSocket to Slack and takes its events there instead of at a request URL.

The app calls `apps.connections.open` with its app-level token (`xapp-`) and is answered a `wss://` URL on
`wss-primary.slack.com`, carrying a ticket the world records. It opens that URL through the proxy, which sends the
upgrade to this provider's socket server (`SlackProvider.sockets`, served by `adapters.proxy.local`): a ticket the
world handed out and nobody used is let in, the connection is told `hello`, and the ticket is spent. From then on
each event the scenario pushes to an agent whose Slack target is `socket_mode` goes on its newest connection as an
`events_api` envelope, and the push waits for the app's acknowledgement (`{"envelope_id": ...}`): unacknowledged
for `ACK_WITHIN` seconds, it is sent again with `retry_attempt` counted up and `retry_reason` `timeout`, up to
`inbound.RETRIES` more times, and then fails the agent, as a refused request does at a request URL. Every message
either way crosses the proxy and is recorded there (`Exchange.frame`).

The open connections of a world are held by its `Hub`, found by the world's store (`hub`): connections, never
state. The tickets handed out and spent are the state, in the world's log.
"""

from __future__ import annotations

import asyncio
import uuid
import weakref
from collections.abc import Awaitable, Callable

from pydantic import ValidationError
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
"""Real seconds Slack waits for an app to acknowledge an envelope before it sends it again."""
CONNECT_WITHIN = 30.0
"""Real seconds a push waits for the agent to have a connection open, as it starts up, before failing it."""
RECONNECT_AFTER = 3600
"""What `hello` says of when Slack will ask the app to reconnect; nothing here asks."""


class SocketTicket(Model):
    """One `apps.connections.open` answer: the ticket its URL carries, and whether a connection has used it."""

    ticket: str
    app_id: str
    used: bool = False


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


class SocketNotAcknowledged(AgentFailed):
    """The agent took no event over Socket Mode: it had no connection open, or never acknowledged the envelope."""


class Hub:
    """The Socket Mode connections open in one world, and the envelopes awaiting their acknowledgement."""

    def __init__(self) -> None:
        self._open: list[WebSocket] = []
        self._changed = asyncio.Condition()
        self._acks: dict[str, asyncio.Future[None]] = {}

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
            waiting.set_result(None)

    async def push(self, callback: wire.EventCallback) -> None:
        """Send `callback` as an `events_api` envelope on the newest connection, again until it is acknowledged."""
        envelope_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{HOST}/{callback.event_id}"))
        for attempt in range(PUSH_ATTEMPTS):
            socket = await self._newest()
            acked = asyncio.get_running_loop().create_future()
            self._acks[envelope_id] = acked
            envelope = wire.SocketEnvelope(
                envelope_id=envelope_id,
                payload=callback,
                retry_attempt=attempt,
                retry_reason="timeout" if attempt else "",
            )
            try:
                await socket.send_text(wire.envelope_body(envelope))
                await asyncio.wait_for(asyncio.shield(acked), timeout=ACK_WITHIN)
                return
            except (TimeoutError, WebSocketDisconnect, RuntimeError):
                continue
            finally:
                self._acks.pop(envelope_id, None)
        raise SocketNotAcknowledged(
            f"the agent did not acknowledge Socket Mode envelope {envelope_id} ({callback.event.type}) in "
            f"{PUSH_ATTEMPTS} sends {ACK_WITHIN:g} seconds apart"
        )

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
    """The socket server's app: a connection at `/link/` carrying a ticket the world handed out and nobody used is
    accepted and told `hello`; any other is refused before the handshake completes, as Slack refuses a stale URL.
    Nothing it does is stamped: `clock` is the port's, and a ticket is spent at whatever moment it is used."""
    connections = hub(world)

    async def app(
        scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]
    ) -> None:
        if scope["type"] != "websocket":
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        socket = WebSocket(scope, receive, send)
        slack = SlackWorld(world)
        ticket = socket.query_params["ticket"] if "ticket" in socket.query_params else ""
        held = slack.body(ticket_ref(ticket), SocketTicket) if ticket else None
        if socket.url.path != PATH or held is None or held.used:
            await socket.close(code=1008)
            return
        slack.write(
            ticket_ref(ticket),
            held.model_copy(update={"used": True}),
            operation=Operation.UPDATE,
            actor=Actor.AGENT,
            parent=TICKETS,
            after=RecordSnapshot(resource="socket_mode", text="the agent opened its Socket Mode connection"),
        )
        await socket.accept()
        await connections.opened(socket)
        try:
            hello = wire.SocketHello(
                num_connections=connections.count(),
                debug_info=wire.SocketDebugInfo(host=HOST, approximate_connection_time=RECONNECT_AFTER),
                connection_info=wire.SocketConnectionInfo(app_id=held.app_id),
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
