"""The OTLP/HTTP endpoint an agent exports its own telemetry to for the length of a run.

    async with Receiver(store, clock, host="127.0.0.1", port=0, forwarding=...) as receiver:
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{receiver.port}"}

    POST /v1/traces    every span is kept with the run (`Store.receive`), stamped as it arrives
    POST /v1/logs      acknowledged and dropped
    POST /v1/metrics   acknowledged and dropped

Each takes `application/x-protobuf` or `application/json`, gzip or deflate or neither, and answers in the
format it was sent. A body that is not an OTLP request is answered 400 and nothing is kept; the run goes on.

Every payload, read or not, is also passed on unchanged to the endpoint the agent's environment named before
Minutehand stood in front of it (`forward.Forwarding`), in the background, so a slow or dead collector never
holds up the agent's exporter. A failure is recorded in the run (`Store.forward_failed`), never raised.

Like the proxy, one receiver serves every run a process plays; `mount` moves it to the next run's store.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from types import TracebackType

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from minutehand.adapters.telemetry import otlp
from minutehand.adapters.telemetry.forward import ENDPOINT, Forwarding, forward, signal_variable
from minutehand.domain.telemetry import ReceivedSpan, Signal, SpanSource
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

logger = logging.getLogger(__name__)

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
SHUTDOWN_SECONDS = 2.0
PROTOCOL = "OTEL_EXPORTER_OTLP_PROTOCOL"
HTTP_PROTOBUF = "http/protobuf"


def exporter_environment(url: str) -> dict[str, str]:
    """What points an agent's OTLP exporter at the receiver at `url`: the base endpoint every SDK reads, the
    traces endpoint for an SDK that reads only that, and OTLP over HTTP with protobuf, in both spellings, so a
    protocol the agent's own environment names does not win over it."""
    return {
        ENDPOINT: url,
        PROTOCOL: HTTP_PROTOBUF,
        signal_variable(Signal.TRACES, "ENDPOINT"): f"{url}/v1/{Signal.TRACES.value}",
        signal_variable(Signal.TRACES, "PROTOCOL"): HTTP_PROTOBUF,
    }


class Receiver:
    def __init__(
        self,
        store: Store,
        clock: Clock,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        forwarding: Forwarding | None = None,
        agent_host: str | None = None,
    ) -> None:
        """`agent_host` is the name the agent reaches this machine by; a forward to it on this port is dropped."""
        self.store = store
        self.clock = clock
        self.host = host
        self.port = port
        self._agent_host = agent_host
        self._forwarding = forwarding or Forwarding(())
        self._server: uvicorn.Server | None = None
        self._socket: socket.socket | None = None
        self._client: httpx.AsyncClient | None = None
        self._forwards: set[asyncio.Task[None]] = set()
        self._by_trace: Callable[[str], Store | None] | None = None

    def mount(self, world: Store, clock: Clock) -> None:
        """From now on spans are kept in `world`, stamped from `clock`."""
        self.store = world
        self.clock = clock

    def route(self, by_trace: Callable[[str], Store | None]) -> None:
        """`minutehand serve`: a span is kept in the world `by_trace` names for its trace id, and in `store` when
        it names none."""
        self._by_trace = by_trace

    @property
    def forwarding(self) -> Forwarding:
        return self._forwarding

    def app(self) -> Starlette:
        def route(signal: Signal) -> Route:
            async def endpoint(request: Request) -> Response:
                return await self._take(signal, request)

            return Route(f"/v1/{signal.value}", endpoint, methods=["POST"])

        return Starlette(routes=[route(signal) for signal in Signal])

    async def _take(self, signal: Signal, request: Request) -> Response:
        body = await request.body()
        content_type = request.headers["content-type"] if "content-type" in request.headers else ""
        encoding = request.headers["content-encoding"] if "content-encoding" in request.headers else ""
        self._pass_on(signal, body, content_type, encoding)
        try:
            payload = otlp.Payload.read(body, content_type, encoding)
            message = otlp.decode(signal, payload)
            received = otlp.spans(message) if signal is Signal.TRACES else []
        except otlp.UnsupportedMedia as e:
            return PlainTextResponse(str(e), status_code=415)
        except otlp.NotOtlp as e:
            logger.warning("refused an OTLP %s payload: %s", signal.value, e)
            return PlainTextResponse(str(e), status_code=400)
        by_store: dict[int, tuple[Store, list[ReceivedSpan]]] = {}
        for span in received:
            found = self._by_trace(span.trace_id) if self._by_trace is not None else None
            store = found or self.store
            by_store.setdefault(id(store), (store, []))[1].append(span)
        for store, spans in by_store.values():
            store.receive(spans, source=SpanSource.RECEIVED)
        content, media = otlp.answer(signal, payload.format)
        return Response(content, media_type=media)

    def _pass_on(self, signal: Signal, body: bytes, content_type: str, encoding: str) -> None:
        destination = self._forwarding.to(signal)
        if destination is None or self._client is None:
            return
        content = {"content-type": content_type} if content_type else {}
        if encoding:
            content["content-encoding"] = encoding
        store, client = self.store, self._client

        async def send() -> None:
            reason = await forward(client, destination, body, content)
            if reason is not None:
                logger.warning("could not pass the agent's %s on to %s: %s", signal.value, destination.url, reason)
                store.forward_failed(signal, destination.url, reason)

        task = asyncio.create_task(send())
        self._forwards.add(task)
        task.add_done_callback(self._forwards.discard)

    async def __aenter__(self) -> Receiver:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
        except OSError as e:
            listener.close()
            raise OSError(f"the telemetry receiver could not listen on {self.host}:{self.port}: {e}") from e
        listener.listen(128)
        listener.setblocking(False)
        self.port = listener.getsockname()[1]
        own = LOOPBACK | {self.host.lower()} | ({self._agent_host.lower()} if self._agent_host else set())
        self._forwarding = self._forwarding.without(frozenset(own), self.port)
        config = uvicorn.Config(
            self.app(),
            lifespan="off",
            http="h11",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=int(SHUTDOWN_SECONDS),
        )
        config.load()
        server = uvicorn.Server(config)
        server.lifespan = config.lifespan_class(config)
        await server.startup(sockets=[listener])
        self._server, self._socket = server, listener
        self._client = httpx.AsyncClient()
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        server, listener, client = self._server, self._socket, self._client
        self._server = self._socket = self._client = None
        if server is not None and listener is not None:
            await server.shutdown(sockets=[listener])
        if self._forwards:
            # What was received before the end is passed on, or its failure recorded, before the run's file closes.
            await asyncio.wait(list(self._forwards), timeout=SHUTDOWN_SECONDS + 10)
        if client is not None:
            await client.aclose()
