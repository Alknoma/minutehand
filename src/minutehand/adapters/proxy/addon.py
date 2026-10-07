"""The mitmproxy addon: route each intercepted call by its host, answer or refuse it, and record it.

A claimed host is answered by its provider's ASGI app and the call is recorded as an
`Exchange` tied to the events the provider wrote while answering. An unclaimed host
is refused with 502 and recorded the same way. A model API is tunnelled without being
decrypted, unless the run edits its requests or records model calls. An edited call is
decrypted, edited and sent on. A recorded call (`record_model_calls`) is decrypted and sent
on unchanged, its answer streamed back to the agent as it arrives when it is a stream, and
kept as a span (`model_calls.span_of`); it is not an `Exchange`, and nothing from its
headers or query string is stored.

A gRPC call to a claimed host (HTTP/2, `content-type: application/grpc`) is sent on to the gRPC server Minutehand runs
for that provider and world (`adapters.proxy.local`), over HTTP/2 without TLS, and recorded once its trailers have
passed back: the method, the request and answer messages as proto3 JSON, its status (`Exchange.grpc`), and the events
its method wrote. A WebSocket upgrade to a claimed host whose provider serves sockets is sent on to that provider's
socket server the same way; the upgrade is recorded, and so is every message on the connection afterwards, either
way (`Exchange.frame`). A gRPC call to a provider that serves no gRPC is answered UNIMPLEMENTED, saying so.

A host no provider claims that the call's world declares outbound (`domain.outbound`) is captured
(`adapters.proxy.capture`): acknowledged with the declared answer, passed through to the real host, or answered
from a recording, and kept as an `Exchange` carrying `Captured`. With `capture_unknown`, an undeclared one is
passed through and kept the same way rather than refused. A pass-through answer reaches the agent chunk by chunk
as it arrives, by the same tee a recorded model call's stream uses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote, unquote

from mitmproxy import http, tcp, tls
from mitmproxy.addons import asgiapp
from mitmproxy.net import encoding
from mitmproxy.proxy import layer, layers, server_hooks
from mitmproxy.proxy.layers import modes

from minutehand.adapters.answering import OUTCOME, PLAIN, Guarded, Outcome, grpc_outcome, kind_of
from minutehand.adapters.emulator import answers
from minutehand.adapters.proxy import capture, connect, credentials, mcp, modeled, redact
from minutehand.adapters.proxy.capture import Broke, Capturing, Declaration, EmulatorRoute
from minutehand.adapters.proxy.edit import apply_edits
from minutehand.adapters.proxy.hosts import loopback_name
from minutehand.adapters.proxy.local import CALL_HEADER, GRPC_NOT_INSTALLED, LocalServers
from minutehand.adapters.proxy.model_calls import EVENT_STREAM, Exchanged, span_of
from minutehand.adapters.proxy.policy import HostPolicy, Routing
from minutehand.adapters.proxy.redirected import Redirected
from minutehand.adapters.proxy.tunnel import Tunnel
from minutehand.adapters.proxy.worlds import Mounted, One, Worlds, one_run
from minutehand.adapters.telemetry.receiver import grpc_installed
from minutehand.application.traffic import SeenCall
from minutehand.domain.emulator import TIME_HEADER, WAKE_HEADER, WORLD_HEADER, ExternalEmulator
from minutehand.domain.outbound import (
    BODY_LIMIT,
    READ_METHODS,
    Acknowledge,
    Forward,
    HostHeader,
    OnMiss,
    PassThrough,
    UnknownHosts,
)
from minutehand.domain.provider import Manifest, world_keys
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.domain.telemetry import SpanSource
from minutehand.domain.world import (
    GRPC_NUMBERS,
    Actor,
    AnsweredBy,
    BodyKept,
    CallBegan,
    CallOutcome,
    Captured,
    CaptureMode,
    Change,
    EntityKind,
    EntityRef,
    Exchange,
    FrameSender,
    GrpcCode,
    GrpcStatus,
    MessageSnapshot,
    Operation,
    Recipient,
    SocketFrame,
    Tunnelled,
    TunnelRoute,
)
from minutehand.ports.clock import Clock
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.model import ModelFailed
from minutehand.ports.provider import ASGIApp, Message, RendersErrors, Scope, ServesGrpc, ServesSockets
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

logger = logging.getLogger(__name__)

TRACEPARENT = "traceparent"


def strip_prefix(path: str, prefix: str) -> str:
    """The path a provider's app sees: the real API's prefix removed, when the path is under it."""
    if not prefix:
        return path
    if path != prefix and not path.startswith((prefix + "/", prefix + "?")):
        return path
    rest = path[len(prefix) :]
    return rest if rest.startswith("/") else "/" + rest


def _json_response(status: int, message: str, host: str) -> http.Response:
    return http.Response.make(
        status, json.dumps({"error": message, "host": host}).encode(), {"content-type": "application/json"}
    )


def _first_header(message: http.Message, name: str) -> str | None:
    values = message.headers.get_all(name)
    return values[0] if values else None


TEE_LIMIT = 64 * 1024 * 1024
"""Bytes of a passed-through answer held to be kept; past this the answer still reaches the agent whole, and
is kept as its length only."""

REPLAYED_HEADER = "x-minutehand-replayed"


BURST_QUIET = 0.5
"""Seconds a tunnel the proxy never opens stays quiet, its last request answered, before what moved on it since
its previous record is written as one call. This only splits calls within a wake: the agent sending in a later
wake than its burst began in always ends that burst first, so a connection reused in a later wake is a record
in each, however the machine's timers ran."""


@dataclass
class _Burst:
    """What has moved on a relayed tunnel since its previous record was written."""

    world: Mounted
    route: TunnelRoute
    began: CallBegan
    started: float
    ended: float
    sent: int = 0
    received: int = 0
    carried: bool = False


@dataclass
class _Relayed:
    """One tunnel the proxy relays as bytes: what they say of a request awaiting its answer (`Tunnel`), and the
    burst in progress, written as a recorded call when it falls quiet, when the connection closes, or when the
    proxy is moved to another run or stopped."""

    tunnel: Tunnel
    connection: str
    port: int
    opened: float
    written: int = 0
    burst: _Burst | None = None
    quiet: asyncio.TimerHandle | None = None


@dataclass(frozen=True)
class _Forwarded:
    """What a forwarded call was before it was pointed at its emulator, to be kept as the agent sent it."""

    route: EmulatorRoute
    host: str
    path: str
    traceparent: str | None
    sent_traceparent: str


@dataclass(frozen=True)
class _GrpcCall:
    """A gRPC call sent on to its provider's gRPC server, as the agent sent it, until its answer has passed."""

    world: Mounted
    provider: ProviderKey
    host: str
    path: str
    traceparent: str | None


@dataclass
class _Socket:
    """A WebSocket connection to a claimed host, sent on to its provider's socket server: where its messages are
    recorded, and how many it has carried."""

    world: Mounted
    provider: ProviderKey
    host: str
    path: str
    carried: int = 0


GRPC = "application/grpc"


def is_grpc(request: http.Request) -> bool:
    """A gRPC call, by its content type: `application/grpc`, or `application/grpc+` and a codec. gRPC-Web, which is
    not gRPC on the wire, is not."""
    media = (_first_header(request, "content-type") or "").split(";", 1)[0].strip().lower()
    return media == GRPC or media.startswith(GRPC + "+")


def is_upgrade(request: http.Request) -> bool:
    """A WebSocket opening handshake (RFC 6455 §4.1)."""
    return (_first_header(request, "upgrade") or "").strip().lower() == "websocket"


def grpc_refusal(code: GrpcCode, message: str) -> http.Response:
    """An answer that is trailers only (the gRPC spec's Trailers-Only): its status in the headers, no message."""
    return http.Response.make(
        200,
        b"",
        {"content-type": GRPC, "grpc-status": str(GRPC_NUMBERS.index(code)), "grpc-message": quote(message, safe=" ")},
    )


def grpc_status(response: http.Response) -> GrpcStatus:
    """How a gRPC call ended, from its trailers or, for an answer that is trailers only, its headers. An answer that
    carries no status at all, or not a number gRPC has, ended UNKNOWN, as a gRPC client reads it."""
    fields = response.trailers if response.trailers is not None and "grpc-status" in response.trailers else None
    fields = fields or response.headers
    raw = fields["grpc-status"] if "grpc-status" in fields else ""
    code = GRPC_NUMBERS[int(raw)] if raw.isdigit() and int(raw) < len(GRPC_NUMBERS) else GrpcCode.UNKNOWN
    message = unquote(fields["grpc-message"]) if "grpc-message" in fields else ""
    return GrpcStatus(code=code, message=message or None)


@dataclass
class _Passing:
    """A captured call sent on to the real host, or to its external emulator, until its answer has passed."""

    world: Mounted
    declaration: Declaration | None
    mode: CaptureMode
    note: str | None
    chunks: list[bytes] = field(default_factory=lambda: list[bytes]())
    size: int = 0
    streamed: bool = False
    forwarded: _Forwarded | None = None


class ProxyAddon:
    def __init__(
        self,
        routing: Routing,
        store: Store,
        clock: Clock,
        telemetry: Telemetry | None = None,
        *,
        record_model_calls: bool = False,
        capturing: Capturing | None = None,
        capture_unknown: UnknownHosts = UnknownHosts.REFUSE,
        model: LanguageModel | None = None,
    ) -> None:
        self.routing = routing
        self.capturing = capturing or Capturing()
        self.capture_unknown = capture_unknown
        self._model = model
        self.worlds: Worlds = one_run(
            store, clock, {}, scenario=None, provider=routing.registry.provider, capturing=self.capturing
        )
        self.telemetry = telemetry
        self.record_model_calls = record_model_calls
        # Each captured call on its way to the real host, by flow id, until its answer has passed.
        self._passing: dict[str, _Passing] = {}
        # The streamed answer of each recorded call, chunk by chunk as it passed through, by flow id.
        self._streams: dict[str, list[bytes]] = {}
        self._recorded: set[str] = set()
        self.last_seen: SeenCall | None = None
        # Calls sent on to a real host and not answered yet, by flow id; tunnels by flow id, each with what its
        # bytes tell of whether the agent awaits an answer on it (`tunnel.Tunnel`).
        self._sent_on: dict[str, str] = {}
        self._tunnels: dict[str, _Relayed] = {}
        # Each call routed to a world and not yet answered, by flow id, with the world and what it is.
        self._in: dict[str, tuple[Mounted, str]] = {}
        # The gRPC and WebSocket servers of each provider and world, and what is on its way to them, by flow id.
        self.local = LocalServers()
        self._grpc: dict[str, _GrpcCall] = {}
        self._sockets: dict[str, _Socket] = {}
        self._closing: set[asyncio.Task[None]] = set()

    def _seen(self, what: str) -> None:
        """Every outbound call is seen as it starts and, when the proxy answers it, as it ends, so a checkpoint
        can wait until the agent has been quiet. A tunnel the proxy does not open is seen as its bytes move."""
        self.last_seen = SeenCall(at=time.monotonic(), what=what)

    def _routed(self, flow_id: str, world: Mounted, what: str) -> None:
        """A call routed to `world` begins: it is busy with it until it ends (`_ended`)."""
        self._in[flow_id] = (world, what)
        world.seen, world.last = time.monotonic(), what

    def _ended(self, flow_id: str) -> None:
        found = self._in.pop(flow_id, None)
        if found is not None:
            world, what = found
            world.seen, world.last = time.monotonic(), what

    def activity_in(self, world: Mounted) -> tuple[float | None, list[str]]:
        """When a call routed to `world` was last seen, and each one still in progress there: a call not yet
        answered, a tunnel whose bytes there say a request awaits its answer. What `serve` waits on to call a world
        quiet."""
        busy = [what for (found, what) in self._in.values() if found is world]
        busy += [
            awaiting
            for relayed in self._tunnels.values()
            if relayed.burst is not None
            and relayed.burst.world is world
            and (awaiting := relayed.tunnel.awaiting()) is not None
        ]
        return world.seen, busy

    def waiting(self) -> list[str]:
        """What the agent sent and has not had answered: a call sent on to a real host, and a tunnel on which a
        request went from the agent and nothing since reads as its answer (`tunnel.Tunnel`: a TLS 1.3 server's
        session tickets do not)."""
        sent = list(self._sent_on.values())
        tunnels = [
            awaiting for relayed in self._tunnels.values() if (awaiting := relayed.tunnel.awaiting()) is not None
        ]
        return sent + tunnels

    def forwarded(self, host: str) -> bool:
        """`localhost` itself, which nothing claims or declares: the agent's environment no longer sends it direct
        (`session.Listen.direct`), so the proxy sends it on to this machine untouched, unrecorded and unseen, as
        if it had gone direct. A name under `localhost` is a host like any other."""
        return (
            loopback_name(host)
            and self.routing.policy(host) is HostPolicy.REFUSE
            and self.worlds.lobby.capturing.find(host) is None
        )

    def http_connect(self, flow: http.HTTPFlow) -> None:
        if self.forwarded(flow.request.pretty_host):
            return
        self._seen(f"CONNECT {flow.request.pretty_host}:{flow.request.port}")

    def next_layer(self, nextlayer: layer.NextLayer) -> None:
        """A tunnel to a model API the run neither edits nor records is relayed as bytes, never decrypted, as a
        TCP flow rather than an ignored connection, so the bytes it carries are seen (`tcp_message`): a request
        on a tunnel that was already open is activity like any other call. mitmproxy's own NextLayer addon has
        chosen first; this replaces its choice for those hosts only."""
        context = nextlayer.context
        chosen = nextlayer.layer
        if (
            isinstance(chosen, layers.HttpLayer)
            and isinstance(context.layers[0], modes.HttpProxy)
            and context.layers[-2:] == [context.layers[0], chosen]
        ):
            # The client's own connection to the proxy, about to be read as HTTP: mitmproxy would refuse a CONNECT
            # it cannot parse with a bare 400, before any hook sees a flow.
            address6 = connect.unbracketed_ipv6(nextlayer.data_client())
            if address6 is not None:
                self._seen(f"CONNECT {address6} without brackets, refused")
                context.layers.remove(chosen)
                nextlayer.layer = connect.Refused(context, connect.refusal(address6))
            return
        address = context.server.address
        if context.client.transport_protocol != "tcp" or address is None:
            return
        if not any(isinstance(lay, layers.HttpLayer | Redirected) for lay in context.layers):
            return  # neither the inside of a CONNECT nor a connection redirected to the proxy
        if self.forwarded(str(address[0])):
            nextlayer.layer = layers.TCPLayer(context, ignore=True)
            return
        if isinstance(nextlayer.layer, layers.TCPLayer) or self.policy(str(address[0])) is not HostPolicy.TUNNEL:
            return
        nextlayer.layer = layers.TCPLayer(context)

    def tcp_start(self, flow: tcp.TCPFlow) -> None:
        host = str(flow.server_conn.address[0]) if flow.server_conn.address else "?"
        self._seen(f"a new tunnelled connection to {host}")
        self._tunnels[flow.id] = self._relayed(flow)

    def _relayed(self, flow: tcp.TCPFlow) -> _Relayed:
        address = flow.server_conn.address
        return _Relayed(
            Tunnel(str(address[0]) if address else "?"),
            connection=flow.id,
            port=int(address[1]) if address else 0,
            opened=flow.client_conn.timestamp_start,
        )

    def tcp_message(self, flow: tcp.TCPFlow) -> None:
        message = flow.messages[-1]
        relayed = self._tunnels[flow.id] if flow.id in self._tunnels else self._relayed(flow)
        self._tunnels[flow.id] = relayed
        tunnel = relayed.tunnel
        carried = tunnel.moved(message.content, from_client=message.from_client)
        self._seen(f"bytes {'to' if message.from_client else 'from'} {tunnel.host} on an open tunnel")
        earlier = relayed.burst
        if message.from_client and earlier is not None and earlier.world.clock.wake() != earlier.began.wake:
            # The agent sends again in a later wake: what moved before is the earlier wake's call, whatever the
            # bytes said of an answer. A wake edge ends a burst, never only a quiet period.
            self._write(relayed, closed=None)
        burst = relayed.burst or self._burst(tunnel.host, message.timestamp)
        relayed.burst = burst
        burst.ended = message.timestamp
        burst.carried = burst.carried or carried
        if message.from_client:
            burst.sent += len(message.content)
        else:
            burst.received += len(message.content)
        if relayed.quiet is not None:
            relayed.quiet.cancel()
        relayed.quiet = asyncio.get_running_loop().call_later(BURST_QUIET, self._fell_quiet, flow.id)
        del flow.messages[:-1]  # bytes are relayed, not kept: a long-lived tunnel would grow without end

    def _burst(self, host: str, at: float) -> _Burst:
        """A burst beginning now: kept in the world the host's model calls are kept in, stamped with that world's
        wake and simulated time now, whenever it is written."""
        world = self.worlds.keeping(host, None)
        if isinstance(self.worlds, One):
            route = TunnelRoute.RUN
        else:
            route = TunnelRoute.NONE if world is self.worlds.lobby else TunnelRoute.HOST
        began = CallBegan(wake=world.clock.wake(), sim_time=world.clock.now())
        return _Burst(world, route, began, started=at, ended=at)

    def _fell_quiet(self, flow_id: str) -> None:
        """No bytes for `BURST_QUIET`: the burst is over unless the agent is still awaiting its answer, which the
        next bytes, the connection's close or a flush end instead."""
        relayed = self._tunnels[flow_id] if flow_id in self._tunnels else None
        if relayed is None or relayed.tunnel.asked:
            return
        self._write(relayed, closed=None)

    def tcp_end(self, flow: tcp.TCPFlow) -> None:
        self._closed(flow)

    def tcp_error(self, flow: tcp.TCPFlow) -> None:
        self._closed(flow)

    def _closed(self, flow: tcp.TCPFlow) -> None:
        relayed = self._tunnels.pop(flow.id, None)
        if relayed is None:
            return
        ends = [t for t in (flow.client_conn.timestamp_end, flow.server_conn.timestamp_end) if t is not None]
        closed = max(ends) if ends else relayed.burst.ended if relayed.burst is not None else None
        self._write(relayed, closed=closed)

    def flush(self) -> None:
        """Write every burst still in progress on a relayed tunnel, as far as it has gone: the run is ending or
        the proxy is moving to another one. What moves on the tunnel afterwards is a burst of its own."""
        for relayed in self._tunnels.values():
            self._write(relayed, closed=None)

    def flush_in(self, world: Mounted) -> None:
        """`flush`, for the bursts kept in `world` only: it is about to be scored and closed, or reset. Its gRPC
        and WebSocket servers are stopped too: a reset world's are started again on the next call."""
        for relayed in self._tunnels.values():
            if relayed.burst is not None and relayed.burst.world is world:
                self._write(relayed, closed=None)
        self._close_local(world)

    async def done(self) -> None:
        """mitmproxy is stopping: what is in progress is written before the run's store is closed, and the gRPC and
        WebSocket servers are stopped."""
        self.flush()
        await self.local.close()
        if self._closing:
            await asyncio.gather(*self._closing)

    def _close_local(self, world: Mounted | None) -> None:
        """Stop the local servers of `world`, or of every world, in the background: called where the proxy moves
        on without waiting (`mount`, `flush_in`); `done` waits for what is still stopping."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop runs, so no server was ever started
        task = loop.create_task(self.local.close(world))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _write(self, relayed: _Relayed, *, closed: float | None) -> None:
        """The burst in progress as one recorded call, in the world it began in. A burst of nothing but TLS
        alerts or cipher changes (a connection's `close_notify`) carried no call and is dropped."""
        if relayed.quiet is not None:
            relayed.quiet.cancel()
            relayed.quiet = None
        burst, relayed.burst = relayed.burst, None
        if burst is None or not burst.carried:
            return
        relayed.written += 1
        host = relayed.tunnel.host
        exchange = Exchange(
            method="CONNECT",
            host=host,
            path=f"{host}:{relayed.port}",
            status=200,
            tunnelled=Tunnelled(
                port=relayed.port,
                connection=relayed.connection,
                burst=relayed.written,
                opened=datetime.fromtimestamp(relayed.opened, UTC),
                started=datetime.fromtimestamp(burst.started, UTC),
                ended=datetime.fromtimestamp(burst.ended, UTC),
                closed=datetime.fromtimestamp(closed, UTC) if closed is not None else None,
                bytes_sent=burst.sent,
                bytes_received=burst.received,
                route=burst.route,
            ),
        )
        head = burst.world.store.head()
        burst.world.store.attach(exchange, first_seq=head + 1, last_seq=head, began=burst.began)

    def mount(
        self, world: Store, clock: Clock, apps: Mapping[ProviderKey, ASGIApp], *, scenario: Scenario | None = None
    ) -> None:
        """`application.orchestrator.Mounts`: from now on calls are recorded in `world` and each of `apps` answers
        its provider's hosts. A provider claimed but not mounted is still built on its first call, over `world`,
        and seeded then with `scenario`'s people and things, unless `world` already holds anything of it. A burst
        in progress on a relayed tunnel is written to the run it began in first, and the gRPC and WebSocket
        servers of the run before are stopped."""
        self.flush()
        self._close_local(None)
        self.worlds = one_run(
            world, clock, apps, scenario=scenario, provider=self.routing.registry.provider, capturing=self.capturing
        )

    def route(self, worlds: Worlds) -> None:
        """`minutehand serve`: from now on each call is answered in the world `worlds` finds for it."""
        self.worlds = worlds

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        host = data.client_hello.sni
        if host is None and data.context.server.address is not None:
            host = data.context.server.address[0]
        if host is not None and self.policy(host) is HostPolicy.TUNNEL:
            self._seen(f"a new tunnelled connection to {host}")
            data.ignore_connection = True

    def policy(self, host: str) -> HostPolicy:
        """The routing's policy for `host`, with a model API this proxy records opened rather than tunnelled."""
        policy = self.routing.policy(host)
        return HostPolicy.RECORD if policy is HostPolicy.TUNNEL and self._records(host) else policy

    def _records(self, host: str) -> bool:
        """Whether a model call to `host` is kept as a span: every one, under `record_model_calls`, or one to a
        model host a world declared with `record`."""
        return self.record_model_calls or self.routing.records(host)

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            return  # answered as its headers arrived: a base-URL request that names no host (`base_url`)
        host = flow.request.pretty_host
        if self.forwarded(host):
            return
        self._seen(f"{flow.request.method} {host}{redact.path(flow.request.path)}")
        policy = self.policy(host)
        if policy in (HostPolicy.EDIT, HostPolicy.RECORD) and self._records(host):
            self._recorded.add(flow.id)
        if policy is HostPolicy.EDIT:
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            self._edit(flow, host)
            return
        if policy not in (HostPolicy.ANSWER, HostPolicy.REFUSE):
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            return
        request = flow.request
        manifest = self.routing.claimant(host)
        presented = credentials.presented(
            authorization=_first_header(request, "authorization"),
            path=request.path,
            content_type=_first_header(request, "content-type") or "",
            body=request.get_content(strict=False) or b"",
        )
        world = self.worlds.world_for(
            host, presented, world_keys(manifest, host, request.path) if manifest is not None else []
        )
        if world is not None and world is not self.worlds.lobby:
            self._routed(flow.id, world, f"{request.method} {host}{redact.path(request.path)}")
        if policy is HostPolicy.ANSWER and manifest is not None and world is not None:
            if is_grpc(request):
                await self._send_grpc(flow, host, manifest, world)
            elif not is_upgrade(request) or not await self._send_upgrade(flow, host, manifest, world):
                await self._answer(flow, host, manifest, world)
            return
        held = world or self.worlds.lobby
        declaration = held.capturing.find(host) if manifest is None else None
        if (
            declaration is None
            and manifest is None
            and self.capture_unknown is UnknownHosts.MODEL
            and self._model is not None
            and (request.method.upper() not in READ_METHODS or modeled.earlier(held.store.calls(), host))
        ):
            await self._modeled(flow, host, held, self._model)
            return
        if declaration is not None or (manifest is None and self.capture_unknown.captures(request.method)):
            await self._capture(flow, host, held, declaration)
            return
        refused = held
        late = None
        if world is None:
            late = self.worlds.late_for(host, presented, world_keys(manifest, host, request.path) if manifest else [])
        async with refused.lock:
            first = refused.store.head() + 1
            if manifest is None:
                flow.response = _json_response(502, "no provider claims this host", host)
            elif late is not None:
                flow.response = _json_response(
                    502, f"this call is for world {late}, which was closed before it came", host
                )
            else:
                flow.response = _json_response(
                    502, "no world claims this call: none holds its credentials or host", host
                )
            self._record(
                refused, flow, host, flow.request.path, first, manifest.key if manifest else None, late_for=late
            )

    def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        """A connection to one of the gRPC servers here speaks HTTP/2 without TLS: there is no TLS to agree a
        protocol by, and gRPC speaks nothing else."""
        address = data.server.address
        if address is not None and address[0] == "127.0.0.1" and int(address[1]) in self.local.grpc_ports:
            data.server.alpn = b"h2"

    async def _send_grpc(self, flow: http.HTTPFlow, host: str, manifest: Manifest, world: Mounted) -> None:
        """Send a gRPC call on to its provider's gRPC server in `world`; one that cannot be served is answered here,
        in gRPC, and recorded."""
        request = flow.request
        self._grpc[flow.id] = _GrpcCall(world, manifest.key, host, request.path, _first_header(request, TRACEPARENT))
        try:
            provider = world.provider_for(manifest)
        except Exception as e:
            logger.error("%s could not be built for a gRPC call", manifest.key, exc_info=e)
            flow.response = grpc_refusal(GrpcCode.INTERNAL, f"minutehand could not build {manifest.key}: {e}")
            self._grpc_passed(flow)
            return
        if not isinstance(provider, ServesGrpc) or not grpc_installed():
            why = (
                GRPC_NOT_INSTALLED
                if isinstance(provider, ServesGrpc)
                else f"minutehand's {manifest.key} fake does not serve gRPC: set the client to its REST transport"
            )
            flow.response = grpc_refusal(GrpcCode.UNIMPLEMENTED, why)
            self._grpc_passed(flow)
            return
        port = await self.local.grpc(world, manifest, provider)
        self._sent_on[flow.id] = f"gRPC {host}{request.path}"
        request.headers[CALL_HEADER] = flow.id
        request.scheme = "http"
        request.host = "127.0.0.1"
        request.port = port

    def _grpc_passed(self, flow: http.HTTPFlow) -> None:
        """A gRPC call whose answer, or failure, has come back: recorded with the events its method wrote, in the
        world it was routed to."""
        call = self._grpc.pop(flow.id)
        answered = self.local.answered.pop(flow.id, None)
        response = flow.response
        world = call.world
        if response is None:
            status = GrpcStatus(code=GrpcCode.UNAVAILABLE, message=flow.error.msg if flow.error is not None else None)
            http_status = 502
        else:
            status, http_status = grpc_status(response), response.status_code
        if answered is not None:
            asked, asked_bytes, said, said_bytes = answered.request, None, answered.answer, None
            first, last, failure = answered.first, answered.last, answered.failure
        else:
            asked, asked_bytes = redact.kept(flow.request.get_content(strict=False) or b"", GRPC)
            said, said_bytes = (
                redact.kept(response.get_content(strict=False) or b"", GRPC) if response is not None else (None, None)
            )
            first, last, failure = world.store.head() + 1, world.store.head(), None
        exchange = Exchange(
            method=flow.request.method,
            host=call.host,
            path=call.path,
            status=http_status,
            request_body=asked,
            response_body=said,
            request_bytes=asked_bytes,
            response_bytes=said_bytes,
            traceparent=call.traceparent,
            outcome=grpc_outcome(status.code, failure),
            failure=failure,
            grpc=status,
        )
        self._seen(f"gRPC {call.host}{call.path}")
        world.store.attach(exchange, first_seq=first, last_seq=last, provider=call.provider)
        if self.telemetry is not None and last >= first:
            for event in world.store.events(since=first - 1):
                if event.seq <= last:
                    self.telemetry.recorded(event)
        self.worlds.answered(world, exchange, [])

    async def _send_upgrade(self, flow: http.HTTPFlow, host: str, manifest: Manifest, world: Mounted) -> bool:
        """Send a WebSocket upgrade on to its provider's socket server in `world`; False when the provider serves
        no sockets, and its app answers the request as any other."""
        try:
            provider = world.provider_for(manifest)
        except Exception:
            return False  # its app, built the same way, fails the same and answers in the provider's error shape
        if not isinstance(provider, ServesSockets):
            return False
        request = flow.request
        port = await self.local.sockets(world, manifest, provider)
        self._sockets[flow.id] = _Socket(world, manifest.key, host, redact.path(request.path))
        request.scheme = "http"
        request.host = "127.0.0.1"
        request.port = port
        return True

    def _upgraded(self, flow: http.HTTPFlow, socket: _Socket) -> None:
        """The upgrade's answer has passed: recorded as a call. One the socket server refused ends the connection."""
        response = flow.response
        assert response is not None
        said, said_bytes = redact.kept(
            response.get_content(strict=False) or b"", _first_header(response, "content-type") or ""
        )
        exchange = Exchange(
            method=flow.request.method,
            host=socket.host,
            path=socket.path,
            status=response.status_code,
            response_body=said,
            response_bytes=said_bytes,
            traceparent=_first_header(flow.request, TRACEPARENT),
            outcome=CallOutcome.ANSWERED if response.status_code == 101 else CallOutcome.REFUSED,
        )
        self._seen(f"{flow.request.method} {socket.host}{socket.path}")
        head = socket.world.store.head()
        socket.world.store.attach(exchange, first_seq=head + 1, last_seq=head, provider=socket.provider)
        self.worlds.answered(socket.world, exchange, [])
        if response.status_code != 101:
            del self._sockets[flow.id]

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        """One message on a WebSocket connection to a claimed host, either way, recorded as it crosses."""
        socket = self._sockets[flow.id] if flow.id in self._sockets else None
        if socket is None or flow.websocket is None:
            return
        message = flow.websocket.messages[-1]
        socket.carried += 1
        sender = FrameSender.AGENT if message.from_client else FrameSender.SERVICE
        kept, kept_bytes = redact.kept(message.content, "application/json" if message.is_text else "")
        exchange = Exchange(
            method=flow.request.method,
            host=socket.host,
            path=socket.path,
            status=101,
            request_body=kept if sender is FrameSender.AGENT else None,
            request_bytes=kept_bytes if sender is FrameSender.AGENT else None,
            response_body=kept if sender is FrameSender.SERVICE else None,
            response_bytes=kept_bytes if sender is FrameSender.SERVICE else None,
            outcome=CallOutcome.ANSWERED,
            frame=SocketFrame(connection=flow.id, number=socket.carried, sender=sender, text=message.is_text),
        )
        self._seen(f"a message {'from' if message.from_client else 'to'} the agent on {socket.host}{socket.path}")
        head = socket.world.store.head()
        socket.world.store.attach(exchange, first_seq=head + 1, last_seq=head, provider=socket.provider)
        del flow.websocket.messages[:-1]  # messages are recorded, not kept: a long-lived connection would grow

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        self._sockets.pop(flow.id, None)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """A recorded call answered as a stream reaches the agent as one: each chunk is passed on as it arrives
        and kept beside, to be read when the stream ends. A captured call passed through is always streamed so."""
        response = flow.response
        if flow.id in self._passing and response is not None:
            self._tee_passing(flow.id, response)
            return
        if flow.id not in self._recorded or response is None:
            return
        if (_first_header(response, "content-type") or "").split(";", 1)[0].strip().lower() != EVENT_STREAM:
            return
        chunks = self._streams.setdefault(flow.id, [])

        def tee(chunk: bytes) -> bytes:
            chunks.append(chunk)
            return chunk

        response.stream = tee

    def response(self, flow: http.HTTPFlow) -> None:
        self._sent_on.pop(flow.id, None)
        self._ended(flow.id)
        if flow.id in self._grpc:
            self._grpc_passed(flow)
            return
        if flow.id in self._sockets:
            self._upgraded(flow, self._sockets[flow.id])
            return
        if flow.id in self._passing:
            self._passed(flow, self._passing.pop(flow.id))
            return
        if flow.id not in self._recorded:
            return
        self._recorded.discard(flow.id)
        streamed = self._streams.pop(flow.id, None)
        request, response = flow.request, flow.response
        assert response is not None
        if streamed is None:
            body = response.get_content(strict=False) or b""
        else:
            raw = b"".join(streamed)
            coding = _first_header(response, "content-encoding")
            decoded = encoding.decode(raw, coding) if coding else raw
            body = decoded if isinstance(decoded, bytes) else decoded.encode("utf-8")
        exchanged = Exchanged(
            host=request.pretty_host,
            path=request.path,
            status=response.status_code,
            request_body=request.get_content(strict=False) or b"",
            request_type=_first_header(request, "content-type") or "",
            response_body=body,
            response_type=_first_header(response, "content-type") or "",
            traceparent=_first_header(request, TRACEPARENT),
            started=datetime.fromtimestamp(request.timestamp_start, UTC),
            ended=datetime.fromtimestamp(response.timestamp_end or response.timestamp_start, UTC),
        )
        span = span_of(exchanged)
        kept_in = self.worlds.keeping(exchanged.host, span.trace_id if span.parent_span_id is not None else None)
        kept_in.store.receive([span], source=SpanSource.WIRE)

    async def _answer(self, flow: http.HTTPFlow, host: str, manifest: Manifest, world: Mounted) -> None:
        """Answer from the provider's app, guarded (`adapters.answering`): whatever building the app or answering
        lets out becomes the agent's answer, and how the call was answered is recorded on it."""
        async with world.lock:
            first = world.store.head() + 1
            original = flow.request.path
            outcome = Outcome()

            async def built(
                scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]
            ) -> None:
                nonlocal first
                app = world.app_for(manifest)
                first = world.store.head() + 1  # what seeding a provider on its first call wrote is not this call's
                await app(scope, receive, send)

            token = OUTCOME.set(outcome)
            try:
                flow.request.path = strip_prefix(original, manifest.path_prefix)
                guarded = Guarded(built, self._renders(manifest), provider=manifest.key, clock=world.clock)
                await asgiapp.serve(_path_decoded(guarded), flow)
            finally:
                OUTCOME.reset(token)
                flow.request.path = original
            exchange = self._record(world, flow, host, original, first, manifest.key, answered_by=outcome)
        response = flow.response
        minted = (
            credentials.minted(
                content_type=_first_header(response, "content-type") or "",
                body=response.get_content(strict=False) or b"",
            )
            if response is not None and response.status_code < 400
            else []
        )
        self.worlds.answered(world, exchange, minted)

    def _renders(self, manifest: Manifest) -> RendersErrors:
        """The provider's error shape; the plain one when it has none, or cannot be built (which the guard then
        answers as Minutehand's internal error, once building the app fails the same way)."""
        try:
            found = self.routing.registry.provider(manifest)
        except Exception:
            return PLAIN
        return found if isinstance(found, RendersErrors) else PLAIN

    def _edit(self, flow: http.HTTPFlow, host: str) -> None:
        try:
            edited = apply_edits(flow.request.content or b"", host, self.routing.edits_for(host))
        except Exception:
            # An edit that fails must not send the agent's request on unedited.
            flow.response = _json_response(502, "the run's model edits could not be applied", host)
            raise
        if edited is not None:
            flow.request.content = edited

    def _record(
        self,
        world: Mounted,
        flow: http.HTTPFlow,
        host: str,
        path: str,
        first: int,
        provider: str | None,
        *,
        late_for: str | None = None,
        answered_by: Outcome | None = None,
    ) -> Exchange:
        """`answered_by` is how a provider's app answered the call; None for a call no provider answered."""
        request, response = flow.request, flow.response
        assert response is not None
        asked, asked_bytes = redact.kept(
            request.get_content(strict=False) or b"", _first_header(request, "content-type") or ""
        )
        answered, answered_bytes = redact.kept(
            response.get_content(strict=False) or b"", _first_header(response, "content-type") or ""
        )
        exchange = Exchange(
            method=request.method,
            host=host,
            path=redact.path(path),
            status=response.status_code,
            request_body=asked,
            response_body=answered,
            request_bytes=asked_bytes,
            response_bytes=answered_bytes,
            traceparent=_first_header(request, TRACEPARENT),
            late_for=late_for,
            outcome=kind_of(answered_by, response.status_code) if answered_by is not None else None,
            failure=answered_by.failure if answered_by is not None else None,
        )
        self._seen(f"{request.method} {host}{exchange.path}")
        last = world.store.head()
        world.store.attach(exchange, first_seq=first, last_seq=last, provider=provider)
        if self.telemetry is not None and last >= first:
            for event in world.store.events(since=first - 1):
                self.telemetry.recorded(event)
        return exchange

    # -- hosts no provider claims, captured -------------------------------------------------------------------

    async def _capture(self, flow: http.HTTPFlow, host: str, world: Mounted, declaration: Declaration | None) -> None:
        """Answer a call to a host no provider claims as its world declares, or, undeclared, pass it through."""
        if declaration is None:
            self._passing[flow.id] = _Passing(world, None, CaptureMode.DISCOVERED, None)
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            return
        if isinstance(declaration, Acknowledge):
            await self._acknowledge(flow, host, world, declaration)
            return
        if isinstance(declaration, Forward):
            await self._forward(flow, host, world, declaration)
            return
        mode = CaptureMode.PASS_THROUGH if isinstance(declaration, PassThrough) else CaptureMode.REPLAY
        plan = world.capturing.replaying[declaration.host] if declaration.host in world.capturing.replaying else None
        if plan is None:
            self._passing[flow.id] = _Passing(world, declaration, mode, None)
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            return
        request = flow.request
        whole = capture.keep(
            request.get_content(strict=False) or b"",
            _first_header(request, "content-type"),
            limit=TEE_LIMIT,
            paths=declaration.redact,
        )
        asked = capture.Asked(
            method=request.method,
            host=host,
            path=redact.path(request.path, also=capture.query_keys(declaration)),
            body=whole.text,
            content_type=_first_header(request, "content-type"),
            raw_digest=whole.body.sha256,
        )
        found, why = plan.recordings.answer(asked, ignore_query=plan.ignore_query, ignore_body=plan.ignore_body)
        if found is not None:
            recorded = found.exchange
            assert recorded.captured is not None
            headers = {REPLAYED_HEADER: plan.recordings.source}
            if recorded.captured.response.content_type is not None:
                headers["content-type"] = recorded.captured.response.content_type
            answer = recorded.response_bytes or (recorded.response_body or "").encode("utf-8")
            flow.response = http.Response.make(recorded.status, answer, headers)
            await self._keep(
                flow, host, world, declaration, mode, AnsweredBy.RECORDING, replayed_from=plan.recordings.source
            )
            return
        if plan.on_miss is OnMiss.PASS_THROUGH:
            self._passing[flow.id] = _Passing(world, declaration, mode, f"not replayed: {why}")
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            return
        flow.response = _json_response(502, f"no recording answers this call: {why}", host)
        await self._keep(flow, host, world, declaration, mode, AnsweredBy.REFUSAL, note=f"not replayed: {why}")

    async def _modeled(self, flow: http.HTTPFlow, host: str, world: Mounted, model: LanguageModel) -> None:
        """Answer a write to a host nobody declared, and every call to it after, as a model standing in for the
        service says, from what it answered for that host before: never sent anywhere. A model that fails is
        answered 502, naming it."""
        request = flow.request
        content_type = _first_header(request, "content-type")
        shown = capture.keep(request.get_content(strict=False) or b"", content_type, limit=TEE_LIMIT, paths=[])
        try:
            found = await modeled.answer(
                model,
                host,
                request.method,
                redact.path(request.path),
                shown.text,
                modeled.earlier(world.store.calls(), host),
            )
        except ModelFailed as e:
            flow.response = _json_response(502, f"the model standing in for this host failed: {e}", host)
            await self._keep(flow, host, world, None, CaptureMode.MODELED, AnsweredBy.REFUSAL, note=str(e))
            return
        flow.response = http.Response.make(found.status, found.body.encode(), {"content-type": found.content_type})
        await self._keep(
            flow, host, world, None, CaptureMode.MODELED, AnsweredBy.MODEL, note=f"answered by {model.model_id}"
        )

    async def _forward(self, flow: http.HTTPFlow, host: str, world: Mounted, declaration: Forward) -> None:
        """Send the call to its external emulator through the emulator's relay, unchanged but for the headers
        `domain.emulator.ADDED_HEADERS` names, `traceparent` (the agent's trace, Minutehand's span of the call as the
        parent) and, as declared, `Host` and the path. An emulator not running or already failed is not sent
        anything: the agent is answered 502 at once, naming it."""
        request = flow.request
        named = declaration.emulator
        route = world.capturing.emulators[named] if named in world.capturing.emulators else None
        why = route.unavailable() if route is not None else f"emulator {named} is not running in this world"
        caller = _first_header(request, TRACEPARENT)
        sent = continued(caller)
        if route is None or why is not None:
            flow.response = _unavailable(502, named, why or "", host)
            await self._keep(
                flow,
                host,
                world,
                declaration,
                CaptureMode.FORWARD,
                AnsweredBy.REFUSAL,
                note=why,
                emulator=route.declaration if route is not None else None,
                emulator_name=named,
                outcome=CallOutcome.UNAVAILABLE,
                forwarded_traceparent=sent,
            )
            return
        original_path = request.path
        self._passing[flow.id] = _Passing(
            world,
            declaration,
            CaptureMode.FORWARD,
            None,
            forwarded=_Forwarded(route, host, original_path, caller, sent),
        )
        self._sent_on[flow.id] = f"{request.method} {host}{redact.path(original_path)}"
        kept_host = _first_header(request, "host") or host
        request.scheme = "http"
        request.host = "127.0.0.1"
        request.port = route.relay_port
        request.path = route.prefix + declaration.prefix + strip_prefix(original_path, declaration.strip)
        request.headers["host"] = kept_host if declaration.host_header is HostHeader.PRESERVE else route.authority
        request.headers[WORLD_HEADER] = world.store.run_id
        request.headers[WAKE_HEADER] = str(world.clock.wake())
        request.headers[TIME_HEADER] = world.clock.now().isoformat()
        request.headers[TRACEPARENT] = sent

    async def _acknowledge(self, flow: http.HTTPFlow, host: str, world: Mounted, declaration: Acknowledge) -> None:
        """Answer as declared, with an id made for this call in place of `{message_id}`; with a message reading,
        the send is also a message from the agent to a person."""
        request = flow.request
        reading = declaration.message
        async with world.lock:
            first = world.store.head() + 1
            answer = capture.canned(
                declaration, request.method, request.path, message_id=message_id(declaration, first)
            )
            flow.response = http.Response.make(answer.status, answer.body, answer.headers)
            if reading is None:
                await self._keep(
                    flow, host, world, declaration, CaptureMode.ACKNOWLEDGE, AnsweredBy.DECLARATION, locked=True
                )
                return
            content_type = _first_header(request, "content-type")
            whole = capture.keep(
                request.get_content(strict=False) or b"", content_type, limit=TEE_LIMIT, paths=declaration.redact
            )
            read = capture.read_message(reading, whole.text, content_type, world.capturing.people)
            if read.unread is None:
                self._message(world, declaration, read, first)
            await self._keep(
                flow,
                host,
                world,
                declaration,
                CaptureMode.ACKNOWLEDGE,
                AnsweredBy.DECLARATION,
                note=f"not read as a message: {read.unread}" if read.unread is not None else None,
                recipients=read.recipients,
                first=first,
                locked=True,
            )

    @staticmethod
    def _message(world: Mounted, declaration: Acknowledge, read: capture.Read, seq: int) -> None:
        """The send as a world event: a message from the agent to each person it reached, as their email, and to
        each address that reaches nobody, as written. Its people can answer it when the declaration says how an
        answer reaches the agent (`replies`)."""
        by_key = {p.key: p for p in world.capturing.people}
        emails = [by_key[r.person].email if r.person is not None else r.address for r in read.recipients]
        channel = "to:" + ",".join(sorted({r.address.lower() for r in read.recipients}))
        text = f"{read.subject}\n\n{read.text}" if read.subject else read.text
        body = json.dumps(
            {"to": [r.address for r in read.recipients], "subject": read.subject, "text": read.text},
            ensure_ascii=False,
        )
        world.store.apply(
            Change(
                entity=EntityRef(provider=declaration.key, kind=EntityKind.MESSAGE, external_id=str(seq)),
                operation=Operation.CREATE,
                actor=Actor.AGENT,
                body=body,
                parent=channel,
                after=MessageSnapshot(
                    text=text, channel=channel, recipient_emails=emails, answerable=declaration.replies is not None
                ),
            )
        )

    def _tee_passing(self, flow_id: str, response: http.Response) -> None:
        passing = self._passing[flow_id]
        kind = capture.media(_first_header(response, "content-type"))
        passing.streamed = kind == EVENT_STREAM or _first_header(response, "content-length") is None

        def tee(chunk: bytes) -> bytes:
            passing.size += len(chunk)
            if passing.size <= TEE_LIMIT:
                passing.chunks.append(chunk)
            return chunk

        response.stream = tee

    def _passed(self, flow: http.HTTPFlow, passing: _Passing) -> None:
        """A passed-through call whose answer has reached the agent: kept with what of the answer was held."""
        response = flow.response
        assert response is not None
        if passing.size > TEE_LIMIT:
            raw = b""
            note = f"its answer of {passing.size} bytes was longer than the proxy holds; kept as its length only"
        else:
            note = None
            coded = b"".join(passing.chunks)
            coding = _first_header(response, "content-encoding")
            try:
                decoded = encoding.decode(coded, coding) if coding else coded
            except ValueError:
                decoded = coded
            raw = decoded if isinstance(decoded, bytes) else (decoded or "").encode("utf-8")
        notes = "; ".join(n for n in (passing.note, note) if n) or None
        forwarded = passing.forwarded
        if forwarded is not None:
            broke = _broke(flow, forwarded)
            if broke is not None:
                # The relay answered in the emulator's place: it could not be reached, or did not answer in time.
                self._unreached(flow, passing, forwarded, broke.reason, broke=broke)
                return
            _as_sent(flow, forwarded)
        self._keep_now(
            flow,
            forwarded.host if forwarded is not None else flow.request.pretty_host,
            passing.world,
            passing.declaration,
            passing.mode,
            AnsweredBy.EMULATOR if forwarded is not None else AnsweredBy.REAL_HOST,
            note=notes,
            answer=raw,
            streamed=passing.streamed,
            whole_size=passing.size,
            emulator=forwarded.route.declaration if forwarded is not None else None,
            forwarded_traceparent=forwarded.sent_traceparent if forwarded is not None else None,
        )

    def error(self, flow: http.HTTPFlow) -> None:
        """A captured call whose real host could not be reached or broke off, or a gRPC call whose server did not
        answer: kept, saying so."""
        self._sent_on.pop(flow.id, None)
        self._ended(flow.id)
        if flow.id in self._grpc:
            self._grpc_passed(flow)
            return
        self._sockets.pop(flow.id, None)
        passing = self._passing.pop(flow.id, None)
        if passing is None:
            return
        reason = flow.error.msg if flow.error is not None else "the connection failed"
        forwarded = passing.forwarded
        if forwarded is not None:
            self._unreached(flow, passing, forwarded, reason)
            return
        if flow.response is None:
            flow.response = _json_response(
                502, f"the real host could not be reached: {reason}", flow.request.pretty_host
            )
        notes = "; ".join(n for n in (passing.note, f"the real host failed: {reason}") if n)
        self._keep_now(
            flow,
            flow.request.pretty_host,
            passing.world,
            passing.declaration,
            passing.mode,
            AnsweredBy.REAL_HOST,
            note=notes,
            answer=b"",
            streamed=passing.streamed,
            whole_size=passing.size,
        )

    def _unreached(
        self, flow: http.HTTPFlow, passing: _Passing, forwarded: _Forwarded, reason: str, *, broke: Broke | None = None
    ) -> None:
        """A forwarded call its emulator did not answer: 504 when it accepted the call and sent nothing back within
        its `answer_within`, 502 otherwise (refused the connection, broke off). Kept as unavailable, and the
        emulator failed: every later call to it is answered at once."""
        named = forwarded.route.declaration.name
        broke = broke or _broke(flow, forwarded)
        why = broke.reason if broke is not None else f"emulator {named} broke off: {reason}"
        timed_out = broke is not None and broke.timed_out
        if flow.response is None:
            flow.response = _unavailable(504 if timed_out else 502, named, why, forwarded.host)
        forwarded.route.failed(why)
        _as_sent(flow, forwarded)
        self._keep_now(
            flow,
            forwarded.host,
            passing.world,
            passing.declaration,
            passing.mode,
            AnsweredBy.REFUSAL,
            note=why,
            answer=flow.response.get_content(strict=False) or b"",
            streamed=passing.streamed,
            whole_size=None,
            emulator=forwarded.route.declaration,
            outcome=CallOutcome.UNAVAILABLE,
            forwarded_traceparent=forwarded.sent_traceparent,
        )

    async def _keep(
        self,
        flow: http.HTTPFlow,
        host: str,
        world: Mounted,
        declaration: Declaration | None,
        mode: CaptureMode,
        answered_by: AnsweredBy,
        *,
        replayed_from: str | None = None,
        note: str | None = None,
        recipients: list[Recipient] | None = None,
        first: int | None = None,
        locked: bool = False,
        emulator: ExternalEmulator | None = None,
        emulator_name: str | None = None,
        outcome: CallOutcome | None = None,
        forwarded_traceparent: str | None = None,
    ) -> Exchange:
        """Keep a captured call the proxy answered itself, tied to the events written for it since `first`."""
        response = flow.response
        assert response is not None
        if locked:
            return self._keep_now(
                flow,
                host,
                world,
                declaration,
                mode,
                answered_by,
                replayed_from=replayed_from,
                note=note,
                recipients=recipients,
                first=first,
                answer=response.get_content(strict=False) or b"",
                streamed=False,
                whole_size=None,
                emulator=emulator,
                emulator_name=emulator_name,
                outcome=outcome,
                forwarded_traceparent=forwarded_traceparent,
            )
        async with world.lock:
            return self._keep_now(
                flow,
                host,
                world,
                declaration,
                mode,
                answered_by,
                replayed_from=replayed_from,
                note=note,
                recipients=recipients,
                first=first,
                answer=response.get_content(strict=False) or b"",
                streamed=False,
                whole_size=None,
                emulator=emulator,
                emulator_name=emulator_name,
                outcome=outcome,
                forwarded_traceparent=forwarded_traceparent,
            )

    def _keep_now(
        self,
        flow: http.HTTPFlow,
        host: str,
        world: Mounted,
        declaration: Declaration | None,
        mode: CaptureMode,
        answered_by: AnsweredBy,
        *,
        answer: bytes,
        streamed: bool,
        whole_size: int | None,
        replayed_from: str | None = None,
        note: str | None = None,
        recipients: list[Recipient] | None = None,
        first: int | None = None,
        emulator: ExternalEmulator | None = None,
        emulator_name: str | None = None,
        outcome: CallOutcome | None = None,
        forwarded_traceparent: str | None = None,
    ) -> Exchange:
        request, response = flow.request, flow.response
        assert response is not None
        limit = declaration.body_limit if declaration is not None else BODY_LIMIT
        paths = declaration.redact if declaration is not None else []
        asked = capture.keep(
            request.get_content(strict=False) or b"", _first_header(request, "content-type"), limit=limit, paths=paths
        )
        answered = capture.keep(answer, _first_header(response, "content-type"), limit=limit, paths=paths)
        if whole_size is not None and whole_size > len(answer):
            answered = capture.KeptBody(
                None, answered.body.model_copy(update={"size": whole_size, "kept": BodyKept.BINARY}), None
            )
        keys = capture.query_keys(declaration) if declaration is not None else redact.CAPTURED_QUERY_KEYS
        operation: str | None = None
        if mode is CaptureMode.FORWARD:
            request_type = _first_header(request, "content-type")
            operation = answers.operation(request.method, request.path, asked.text, request_type)
            if outcome is None and emulator is not None:
                outcome = answers.outcome(
                    emulator, response.status_code, answered.text, _first_header(response, "content-type")
                )
        started = datetime.fromtimestamp(request.timestamp_start, UTC)
        ended = datetime.fromtimestamp(response.timestamp_end or response.timestamp_start, UTC)
        exchange = Exchange(
            method=request.method,
            host=host,
            path=redact.path(request.path, also=keys),
            status=response.status_code,
            request_body=asked.text,
            response_body=answered.text,
            request_bytes=asked.raw,
            response_bytes=answered.raw,
            traceparent=_first_header(request, TRACEPARENT),
            outcome=outcome,
            captured=Captured(
                mode=mode,
                declared_as=declaration.host if declaration is not None else None,
                answered_by=answered_by,
                replayed_from=replayed_from,
                note=note,
                started=started,
                ended=max(started, ended),
                request=asked.body,
                response=answered.body,
                streamed=streamed,
                recipients=recipients or [],
                emulator=emulator.name if emulator is not None else emulator_name,
                operation=operation,
                forwarded_traceparent=forwarded_traceparent,
            ),
        )
        self._seen(f"{request.method} {host}{exchange.path}")
        before = world.store.head()
        for n, call in enumerate(
            mcp.tool_calls(
                host,
                asked.text,
                _first_header(request, "content-type"),
                answered.text,
                _first_header(response, "content-type"),
            )
        ):
            world.store.apply(
                Change(
                    entity=EntityRef(provider="mcp", kind=EntityKind.TOOL_CALL, external_id=f"{host}/{before + 1}/{n}"),
                    operation=Operation.CREATE,
                    actor=Actor.AGENT,
                    body=call.model_dump_json(),
                    after=call,
                )
            )
        head = world.store.head()
        world.store.attach(exchange, first_seq=first if first is not None else before + 1, last_seq=head)
        self.worlds.answered(world, exchange, [])
        return exchange


def continued(traceparent: str | None) -> str:
    """The `traceparent` a forwarded copy carries: the agent's trace and flags with a new span id, Minutehand's
    span of the call, as the parent; a trace begun here when the agent sent none it could continue."""
    parts = traceparent.strip().split("-") if traceparent is not None else []
    if len(parts) == 4 and len(parts[1]) == 32 and len(parts[3]) == 2 and parts[1] != "0" * 32:
        return f"00-{parts[1]}-{secrets.token_hex(8)}-{parts[3]}"
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def _broke(flow: http.HTTPFlow, forwarded: _Forwarded) -> Broke | None:
    """Why the relay broke off the connection this call went out on, if it did."""
    address = flow.server_conn.sockname
    return forwarded.route.failure_for(int(address[1])) if address is not None else None


def _as_sent(flow: http.HTTPFlow, forwarded: _Forwarded) -> None:
    """The request as the agent sent it, for keeping: its own path and `traceparent`. What else was added for the
    emulator is in headers, which are never kept."""
    flow.request.path = forwarded.path
    if forwarded.traceparent is None:
        del flow.request.headers[TRACEPARENT]
    else:
        flow.request.headers[TRACEPARENT] = forwarded.traceparent


def _unavailable(status: int, emulator: str, why: str, host: str) -> http.Response:
    return http.Response.make(
        status,
        json.dumps(
            {"error": f"external emulator {emulator} is unavailable: {why}", "emulator": emulator, "host": host}
        ).encode(),
        {"content-type": "application/json"},
    )


def message_id(declaration: Acknowledge, seq: int) -> str:
    """The id an acknowledged send is answered with: its declaration's name and the seq its message takes."""
    return f"{declaration.key}-{seq}"


def _path_decoded(app: ASGIApp) -> ASGIApp:
    """The app, handed `path` decoded as ASGI says it arrives: mitmproxy percent-encodes the request target into
    it, so Docs' `/v1/documents/{id}:batchUpdate` would reach a router as `{id}%3AbatchUpdate`."""

    async def decoded(
        scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]
    ) -> None:
        raw = scope["raw_path"] if "raw_path" in scope else None
        if isinstance(raw, str):
            scope = {**scope, "path": unquote(raw.split("?", 1)[0])}
        await app(scope, receive, send)

    return decoded
