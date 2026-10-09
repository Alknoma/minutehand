"""The OTLP endpoint an agent exports its own telemetry to for the length of a run, over HTTP and over gRPC on the
same port.

    async with Receiver(store, clock, host="127.0.0.1", port=0, forwarding=...) as receiver:
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{receiver.port}"}

    POST /v1/traces    every span is kept with the run (`Store.receive`), stamped as it arrives
    POST /v1/logs      a log record carrying GenAI content is kept as a child span of the span it names
                       (`otlp.genai_events`); any other is acknowledged and dropped
    POST /v1/metrics   acknowledged and dropped
    gRPC               the same three services (`opentelemetry.proto.collector.*.v1`), kept the same way

    POST /minutehand/agent/store   the agent's memory (`minutehand.agent.store`): a get, a list or a write,
                                   answered from the run and recorded in it (`application.memory`)
    POST /minutehand/agent/wake    the agent's next wake (`minutehand.agent.wake`), recorded in the run
    POST /minutehand/mcp           a tool call `minutehand mcp-relay` saw, recorded in the run

The agent's own calls are held while `hold` is on: a fork's agent may start before the fork's run exists, and what it
reads then must be the fork's memory, not the parent's at its end.

Each HTTP route takes `application/x-protobuf` or `application/json`, gzip or deflate or neither, and answers in
the format it was sent. A body that is not an OTLP request is answered 400 and nothing is kept; the run goes on.

Many SDKs build a gRPC exporter in code, whatever OTEL_EXPORTER_OTLP_PROTOCOL says, and point it at the endpoint
the environment names: the same port. So the port is a front that reads each connection's first bytes: the
HTTP/2 preface goes to a gRPC server, anything else to the HTTP one, both on loopback ports of their own. gRPC
needs `grpcio` (`minutehand[grpc]`); without it a gRPC exporter is answered by nothing, and the run says so once,
in `notices`, rather than "no telemetry was received".

Every payload, read or not, is also passed on unchanged to the endpoint the agent's environment named before
Minutehand stood in front of it (`forward.Forwarding`), in the background, so a slow or dead collector never
holds up the agent's exporter; one that came over gRPC is passed on as OTLP/HTTP protobuf. A failure is recorded
in the run (`Store.forward_failed`), never raised.

Like the proxy, one receiver serves every run a process plays; `mount` moves it to the next run's store.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import socket
from collections.abc import Callable
from types import TracebackType

import httpx
import uvicorn
from google.protobuf.message import Message
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceResponse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from pydantic import TypeAdapter, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from minutehand.adapters.proxy import mcp
from minutehand.adapters.telemetry import otlp
from minutehand.adapters.telemetry.forward import ENDPOINT, Forwarding, forward, signal_variable
from minutehand.application import memory
from minutehand.domain.memory import MemoryGet, MemoryList, StoreCall, WakeMark
from minutehand.domain.scenario import Model
from minutehand.domain.telemetry import ReceivedSpan, Signal, SpanSource
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation
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


MCP_PATH = "/minutehand/mcp"
"""Where `minutehand mcp-relay` reports each tool call; the agent is handed it as MINUTEHAND_MCP_URL."""
MCP_URL_ENV = "MINUTEHAND_MCP_URL"
JSON = "application/json"

AGENT_PATH = "/minutehand/agent"
"""Where `minutehand.agent` reaches the run; the agent is handed it as MINUTEHAND_AGENT_URL, with MINUTEHAND_ON."""

_STORE_CALL: TypeAdapter[StoreCall] = TypeAdapter(StoreCall)


class RelayedCall(Model):
    """One request line and the response line that answered it, as the relay saw them."""

    server: str
    request: str
    response: str


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
        grpc: bool | None = None,
    ) -> None:
        """`agent_host` is the name the agent reaches this machine by; a forward to it on this port is dropped.
        `grpc`: whether gRPC is received; None receives it when `grpcio` is installed."""
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
        self._by_span: Callable[[ReceivedSpan], Store | None] | None = None
        self._grpc = grpc_installed() if grpc is None else grpc
        self._notices: list[str] = []
        self._front: asyncio.Server | None = None
        self._http_port = 0
        self._grpc_port: int | None = None
        self._grpc_server: GrpcServer | None = None
        self._grpc_starting = asyncio.Lock()
        self._mounted = asyncio.Event()
        self._mounted.set()
        self._reads = memory.Reads()

    @property
    def notices(self) -> list[str]:
        """What a run's reader must be told about the telemetry it could not receive: each said once."""
        return list(self._notices)

    def mount(self, world: Store, clock: Clock) -> None:
        """From now on spans are kept in `world`, stamped from `clock`, and the agent's calls answered from it."""
        self.store = world
        self.clock = clock
        self._reads = memory.Reads()
        self._mounted.set()

    def memory_reads(self, wake: int) -> int:
        """The agent's gets and listings of its memory in `wake` of the run mounted (`memory.Reads`)."""
        return self._reads.count(wake)

    def hold(self) -> None:
        """Hold the agent's calls until the next `mount`: a fork's agent starts before the fork's run exists."""
        self._mounted.clear()

    def route(self, by_span: Callable[[ReceivedSpan], Store | None]) -> None:
        """`minutehand serve`: a span is kept where `by_span` says (the world whose calls carried its trace, or the
        case open while it ran), and in `store` when it names none."""
        self._by_span = by_span

    @property
    def forwarding(self) -> Forwarding:
        return self._forwarding

    def app(self) -> Starlette:
        def route(signal: Signal) -> Route:
            async def endpoint(request: Request) -> Response:
                return await self._take(signal, request)

            return Route(f"/v1/{signal.value}", endpoint, methods=["POST"])

        async def relayed(request: Request) -> Response:
            return self._relayed(await request.body())

        async def remembered(request: Request) -> Response:
            body = await request.body()
            await self._mounted.wait()
            return self._remembered(body)

        async def marked(request: Request) -> Response:
            body = await request.body()
            await self._mounted.wait()
            return self._marked(body)

        return Starlette(
            routes=[
                *(route(signal) for signal in Signal),
                Route(MCP_PATH, relayed, methods=["POST"]),
                Route(f"{AGENT_PATH}/store", remembered, methods=["POST"]),
                Route(f"{AGENT_PATH}/wake", marked, methods=["POST"]),
            ]
        )

    def _remembered(self, body: bytes) -> Response:
        """One call of the agent's store, answered from the run being played and recorded in it. Nothing awaits
        between reading the memory and writing it, so no other call of the agent's comes between."""
        try:
            call = _STORE_CALL.validate_json(body)
        except ValidationError as e:
            return PlainTextResponse(f"not a call of the agent's store: {e}", status_code=400)
        if isinstance(call, MemoryGet):
            answer: Model = self._reads.recall(self.store, self.clock.wake(), call)
        elif isinstance(call, MemoryList):
            answer = self._reads.listing(self.store, self.clock.wake(), call)
        else:
            answer = memory.remember(self.store, call)
        return Response(answer.model_dump_json(), media_type=JSON)

    def _marked(self, body: bytes) -> Response:
        try:
            said = WakeMark.model_validate_json(body)
        except ValidationError as e:
            return PlainTextResponse(f"not a next wake: {e}", status_code=400)
        memory.mark(self.store, said)
        return Response(status_code=204)

    def _relayed(self, body: bytes) -> Response:
        """A tool call `minutehand mcp-relay` saw pass between the agent and an MCP server on its standard input and
        output: recorded as the agent's, in the run being played."""
        try:
            said = RelayedCall.model_validate_json(body)
        except ValueError as e:
            return PlainTextResponse(f"not a relayed MCP call: {e}", status_code=400)
        for n, call in enumerate(mcp.tool_calls(said.server, said.request, JSON, said.response, JSON)):
            head = self.store.head()
            self.store.apply(
                Change(
                    entity=EntityRef(
                        provider="mcp", kind=EntityKind.TOOL_CALL, external_id=f"{said.server}/{head + 1}/{n}"
                    ),
                    operation=Operation.CREATE,
                    actor=Actor.AGENT,
                    body=call.model_dump_json(),
                    after=call,
                )
            )
        return Response(status_code=204)

    async def _take(self, signal: Signal, request: Request) -> Response:
        body = await request.body()
        content_type = request.headers["content-type"] if "content-type" in request.headers else ""
        encoding = request.headers["content-encoding"] if "content-encoding" in request.headers else ""
        self._pass_on(signal, body, content_type, encoding)
        try:
            payload = otlp.Payload.read(body, content_type, encoding)
            message = otlp.decode(signal, payload)
            self.keep(signal, message)
        except otlp.UnsupportedMedia as e:
            return PlainTextResponse(str(e), status_code=415)
        except otlp.NotOtlp as e:
            logger.warning("refused an OTLP %s payload: %s", signal.value, e)
            return PlainTextResponse(str(e), status_code=400)
        content, media = otlp.answer(signal, payload.format)
        return Response(content, media_type=media)

    def keep(self, signal: Signal, message: Message) -> None:
        """What a decoded export request holds that the run keeps: its spans, and its GenAI log events."""
        if signal is Signal.TRACES:
            received, source = otlp.spans(message), SpanSource.RECEIVED
        elif signal is Signal.LOGS:
            received, source = otlp.genai_events(message), SpanSource.LOG
        else:
            return
        by_store: dict[int, tuple[Store, list[ReceivedSpan]]] = {}
        for span in received:
            found = self._by_span(span) if self._by_span is not None else None
            store = found or self.store
            by_store.setdefault(id(store), (store, []))[1].append(span)
        for store, spans in by_store.values():
            store.receive(spans, source=source)

    def took_grpc(self, signal: Signal, message: Message) -> None:
        """An export request that came over gRPC: passed on as OTLP/HTTP protobuf, and kept."""
        self._pass_on(signal, message.SerializeToString(), otlp.PROTOBUF, "")
        self.keep(signal, message)

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
        inner = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        inner.bind(("127.0.0.1", 0))
        inner.listen(128)
        inner.setblocking(False)
        self._http_port = inner.getsockname()[1]
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
        await server.startup(sockets=[inner])
        self._server, self._socket = server, inner
        self._front = await asyncio.start_server(self._connection, sock=listener)
        self._client = httpx.AsyncClient()
        return self

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One connection to the public port: to the gRPC server when it opens with HTTP/2's preface, else to
        the HTTP one, byte for byte both ways."""
        try:
            head = await reader.readexactly(len(H2_PREFACE))
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        port = self._http_port
        if head == H2_PREFACE:
            if not self._grpc:
                self._say(GRPC_NOT_INSTALLED)
                writer.close()
                return
            port = await self._grpc_started()
        try:
            inner_reader, inner_writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            writer.close()
            return
        inner_writer.write(head)
        await asyncio.gather(_pump(reader, inner_writer), _pump(inner_reader, writer))

    async def _grpc_started(self) -> int:
        """The gRPC server, started on the first gRPC connection: most agents never open one, and a run that
        loads gRPC carries its fork handlers into every hook command it starts."""
        async with self._grpc_starting:
            if self._grpc_port is None:
                self._grpc_server = GrpcServer(self)
                self._grpc_port = await self._grpc_server.start()
            return self._grpc_port

    def _say(self, notice: str) -> None:
        if notice not in self._notices:
            logger.warning("%s", notice)
            self._notices.append(notice)

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        front, grpc_server = self._front, self._grpc_server
        self._front = self._grpc_server = None
        if front is not None:
            front.close()
        if grpc_server is not None:
            await grpc_server.stop()
        server, listener, client = self._server, self._socket, self._client
        self._server = self._socket = self._client = None
        if server is not None and listener is not None:
            await server.shutdown(sockets=[listener])
        if self._forwards:
            # What was received before the end is passed on, or its failure recorded, before the run's file closes.
            await asyncio.wait(list(self._forwards), timeout=SHUTDOWN_SECONDS + 10)
        if client is not None:
            await client.aclose()


H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
"""How every HTTP/2 connection with prior knowledge opens, and so every gRPC exporter's."""

GRPC_NOT_INSTALLED = (
    "the agent's OTLP exporter speaks gRPC, and this install of Minutehand cannot receive gRPC: install "
    "`minutehand[grpc]` (it adds grpcio), or point the agent's exporter at OTLP over HTTP; nothing it exported "
    "over gRPC was kept"
)


def grpc_installed() -> bool:
    return importlib.util.find_spec("grpc") is not None


async def _pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while chunk := await source.read(65536):
            sink.write(chunk)
            await sink.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        sink.close()


class GrpcServer:
    """OTLP's three gRPC services on a loopback port, handing each request to the receiver. Imported only when
    started, since `grpcio` is an extra."""

    def __init__(self, receiver: Receiver) -> None:
        self._receiver = receiver
        self._server: object | None = None

    async def start(self) -> int:
        # gRPC's fork handlers write to stderr at every fork once it is loaded, and block when nobody reads it:
        # this process forks for every hook and the agent's command, none of which uses gRPC.
        os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")
        import grpc
        from opentelemetry.proto.collector.logs.v1 import logs_service_pb2_grpc
        from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2_grpc
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

        receiver = self._receiver

        class Traces(trace_service_pb2_grpc.TraceServiceServicer):
            async def Export(self, request: Message, context: object) -> Message:
                receiver.took_grpc(Signal.TRACES, request)
                return ExportTraceServiceResponse()

        class Logs(logs_service_pb2_grpc.LogsServiceServicer):
            async def Export(self, request: Message, context: object) -> Message:
                receiver.took_grpc(Signal.LOGS, request)
                return ExportLogsServiceResponse()

        class Metrics(metrics_service_pb2_grpc.MetricsServiceServicer):
            async def Export(self, request: Message, context: object) -> Message:
                receiver.took_grpc(Signal.METRICS, request)
                return ExportMetricsServiceResponse()

        server = grpc.aio.server()
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Traces(), server)
        logs_service_pb2_grpc.add_LogsServiceServicer_to_server(Logs(), server)
        metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(Metrics(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self._server = server
        return port

    async def stop(self) -> None:
        import grpc

        server = self._server
        if isinstance(server, grpc.aio.Server):
            await server.stop(grace=SHUTDOWN_SECONDS)
