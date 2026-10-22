"""The loopback servers that answer what a provider's ASGI app cannot: gRPC, whose answers end in HTTP/2 trailers,
and WebSocket connections, which outlive a request. mitmproxy hosts an app by buffering its whole answer and has no
way to send trailers or to keep a connection open, so the proxy forwards those calls here instead (`addon`).

One server per world and provider, started on the first call that needs it and stopped when the proxy moves to
another run, when its world is closed or reset (`close`), or when the proxy stops:

- gRPC: a `grpc.aio` server on `127.0.0.1`, plaintext HTTP/2, serving the provider's `ServesGrpc.grpc` methods over
  the world's store and clock. Each method answers under the world's lock, as the proxy answers a REST call, so the
  events between two reads of the world's head are the call's; what it read and answered, as proto3 JSON, and that
  range of events are left in `answered` under the id the proxy gave the call (`CALL_HEADER`), for the proxy to
  record when the answer has passed back through it. Needs `grpcio` (`minutehand[grpc]`).
- WebSocket: uvicorn on `127.0.0.1`, its WebSocket protocol wsproto's, serving the provider's `ServesSockets.sockets`
  app. Every message on a connection crosses the proxy, which records it.

What lives here lives only while the process does: open connections and listening ports, never the world's state.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import uvicorn
from google.protobuf import json_format
from google.protobuf.message import Message as ProtoMessage

from minutehand.adapters.answering import grpc_ended
from minutehand.adapters.proxy import redact
from minutehand.adapters.proxy.worlds import Mounted
from minutehand.domain.provider import Manifest
from minutehand.domain.world import CallFailure
from minutehand.ports.provider import GrpcMethod, ServesGrpc, ServesSockets

logger = logging.getLogger(__name__)

CALL_HEADER = "x-minutehand-call"
"""The id the proxy gives a gRPC call it forwards here, read back to find what the call's method left in
`answered`. It is added on the way here only; nothing the agent sees carries it."""

SHUTDOWN_SECONDS = 2.0

GRPC_NOT_INSTALLED = (
    "this install of Minutehand cannot answer gRPC: install `minutehand[grpc]` (it adds grpcio and the Google "
    "client libraries whose messages the fakes speak), or set the agent's client to its REST transport"
)


@dataclass(frozen=True)
class GrpcAnswered:
    """What one gRPC method was handed and answered, as proto3 JSON with credentials redacted, and the events it
    wrote (`first` to `last`, none when `last` < `first`); `failure` set when Minutehand answered in its place."""

    first: int
    last: int
    request: str | None
    answer: str | None
    failure: CallFailure | None


@dataclass
class _Served:
    world: Mounted
    port: int
    stop: Callable[[], Awaitable[None]]


def _serialized(message: ProtoMessage) -> bytes:
    return message.SerializeToString()


def _json(message: ProtoMessage) -> str | None:
    text = json_format.MessageToJson(message, indent=None)
    kept, _ = redact.kept(text.encode("utf-8"), "application/json")
    return kept


class LocalServers:
    def __init__(self) -> None:
        self._grpc: dict[tuple[int, str], _Served] = {}
        self._sockets: dict[tuple[int, str], _Served] = {}
        self._starting = asyncio.Lock()
        self.answered: dict[str, GrpcAnswered] = {}
        self.grpc_ports: set[int] = set()
        """Every port a gRPC server here listens on: the proxy speaks HTTP/2 without TLS to these."""

    async def grpc(self, world: Mounted, manifest: Manifest, provider: ServesGrpc) -> int:
        """The port `provider`'s gRPC methods are answered on in `world`, its server started on first use."""
        key = (id(world), manifest.key)
        async with self._starting:
            if key not in self._grpc:
                methods = provider.grpc(world.store, world.clock)
                self._grpc[key] = await self._grpc_server(world, manifest, methods)
                self.grpc_ports.add(self._grpc[key].port)
            return self._grpc[key].port

    async def sockets(self, world: Mounted, manifest: Manifest, provider: ServesSockets) -> int:
        """The port `provider`'s WebSocket app is served on in `world`, started on first use."""
        key = (id(world), manifest.key)
        async with self._starting:
            if key not in self._sockets:
                self._sockets[key] = await _socket_server(world, provider)
            return self._sockets[key].port

    async def close(self, world: Mounted | None = None) -> None:
        """Stop the servers of `world`, or of every world: its connections close, and a later call starts anew."""
        for table in (self._grpc, self._sockets):
            for key in [k for k, served in table.items() if world is None or served.world is world]:
                served = table.pop(key)
                self.grpc_ports.discard(served.port)
                await served.stop()

    async def _grpc_server(self, world: Mounted, manifest: Manifest, methods: Sequence[GrpcMethod]) -> _Served:
        # gRPC's fork handlers write to stderr at every fork once it is loaded, and block when nobody reads it:
        # this process forks for every hook and the agent's command, none of which uses gRPC.
        os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")
        import grpc

        answered = self.answered

        def behaviour(
            method: GrpcMethod,
        ) -> Callable[[ProtoMessage, grpc.aio.ServicerContext], Awaitable[ProtoMessage]]:
            async def answer(request: ProtoMessage, context: grpc.aio.ServicerContext) -> ProtoMessage:
                call = next((str(v) for k, v in context.invocation_metadata() or () if k == CALL_HEADER), None)
                found: ProtoMessage | None = None
                async with world.lock:
                    first = world.store.head() + 1
                    try:
                        found = await method.answer(request)
                        ended = None
                    except Exception as error:
                        ended = grpc_ended(error, provider=manifest.key, path=method.path)
                    last = world.store.head()
                if call is not None:
                    answered[call] = GrpcAnswered(
                        first=first,
                        last=last,
                        request=_json(request),
                        answer=_json(found) if found is not None else None,
                        failure=ended.failure if ended is not None else None,
                    )
                if ended is not None:
                    await context.abort(grpc.StatusCode[ended.code.value], ended.message)
                assert found is not None
                return found

            return answer

        services: dict[str, dict[str, grpc.RpcMethodHandler]] = {}
        for method in methods:
            service, _, name = method.path.lstrip("/").rpartition("/")
            if not service or not name:
                raise ValueError(f"{manifest.key} serves a gRPC method at {method.path!r}: not /package.Service/Method")
            services.setdefault(service, {})[name] = grpc.unary_unary_rpc_method_handler(
                behaviour(method),
                request_deserializer=method.request.FromString,
                response_serializer=_serialized,
            )
        server = grpc.aio.server()
        server.add_generic_rpc_handlers(
            [grpc.method_handlers_generic_handler(service, handlers) for service, handlers in services.items()]
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        async def stop() -> None:
            await server.stop(grace=SHUTDOWN_SECONDS)

        return _Served(world, port, stop)


async def _socket_server(world: Mounted, provider: ServesSockets) -> _Served:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    config = uvicorn.Config(
        provider.sockets(world.store, world.clock),
        lifespan="off",
        http="h11",
        ws="wsproto",
        ws_ping_interval=None,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=int(SHUTDOWN_SECONDS),
    )
    config.load()
    server = uvicorn.Server(config)
    server.lifespan = config.lifespan_class(config)
    await server.startup(sockets=[listener])

    async def stop() -> None:
        await server.shutdown(sockets=[listener])

    return _Served(world, listener.getsockname()[1], stop)
