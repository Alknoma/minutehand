"""Test plumbing for forwarding: an upstream that keeps the exact bytes it was sent and answers as told, a proxy
whose world forwards a host to it, and a stand-in emulator process for the lifecycle tests.

None of this is an emulator of any service: the upstream answers whatever the test says, and the process below
answers every path the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import sys
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from minutehand.adapters.emulator.fleet import Emulators
from minutehand.adapters.emulator.process import Running
from minutehand.adapters.proxy.capture import Capturing
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.emulators import record_health
from minutehand.application.run_clock import RunClock
from minutehand.domain.emulator import EmulatorChange, ExternalEmulator
from minutehand.domain.outbound import Forward


@dataclass
class Reply:
    """What the upstream answers: a status line and headers as bytes, then each chunk after `pause` seconds."""

    head: bytes = b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 2\r\n\r\n"
    chunks: Sequence[bytes] = (b"{}",)
    pause: float = 0.0
    silent: bool = False


@dataclass
class Raw:
    """Every request the upstream read, exactly as the bytes arrived."""

    requests: list[bytes] = field(default_factory=lambda: list[bytes]())
    port: int = 0
    wrote_at: list[float] = field(default_factory=lambda: list[float]())
    """`time.monotonic()` as each chunk of the answer was written."""


async def _read_request(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    chunked = False
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
        if name.strip().lower() == b"transfer-encoding" and b"chunked" in value.lower():
            chunked = True
    if chunked:
        body = b""
        while True:
            size_line = await reader.readuntil(b"\r\n")
            size = int(size_line.strip().split(b";")[0], 16)
            chunk = await reader.readexactly(size + 2)
            body += size_line + chunk
            if size == 0:
                break
        return head + body
    return head + (await reader.readexactly(length) if length else b"")


@asynccontextmanager
async def raw_upstream(
    reply: Reply, *, unix: Path | None = None, tls: ssl.SSLContext | None = None
) -> AsyncIterator[Raw]:
    """An HTTP/1.1 server on 127.0.0.1 (or a Unix socket) that keeps each request's bytes and answers `reply`."""
    raw = Raw()
    writers: set[asyncio.StreamWriter] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.add(writer)
        try:
            while True:
                try:
                    raw.requests.append(await _read_request(reader))
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                if reply.silent:
                    await asyncio.sleep(3600)
                writer.write(reply.head)
                await writer.drain()
                for chunk in reply.chunks:
                    await asyncio.sleep(reply.pause)
                    writer.write(chunk)
                    await writer.drain()
                    raw.wrote_at.append(time.monotonic())
        finally:
            writers.discard(writer)
            writer.close()

    if unix is not None:
        server = await asyncio.start_unix_server(handle, str(unix), ssl=tls)
    else:
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls)
        raw.port = int(server.sockets[0].getsockname()[1])
    try:
        yield raw
    finally:
        server.close()
        for writer in list(writers):
            writer.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()


@dataclass
class Forwarding:
    proxy: Proxy
    emulators: Emulators
    changes: list[EmulatorChange]


@asynccontextmanager
async def forwarding(
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    declared: Sequence[ExternalEmulator],
    hosts: Sequence[Forward],
) -> AsyncIterator[Forwarding]:
    """A proxy whose one world forwards `hosts`, with `declared` started and watched, each health change
    recorded in `store` as a run records it."""
    routes: dict[str, Running] = {}
    changes: list[EmulatorChange] = []

    def changed(change: EmulatorChange) -> None:
        changes.append(change)
        record_health(store, change)

    emulators = Emulators(tmp_path / "logs", {}, changed, running=routes)
    proxy = Proxy(
        Routing(registry), store, clock, confdir=tmp_path / "ca", capturing=Capturing(hosts, emulators=routes)
    )
    async with proxy:
        await emulators.start(declared)
        try:
            yield Forwarding(proxy, emulators, changes)
        finally:
            await emulators.stop()


STAND_IN = r"""
import http.server, os, sys
dies_after = int(os.environ.get("DIES_AFTER", "0"))
answered = 0
def export(traceparent):
    # Its own span, under the call it was handed, sent as OTLP/JSON to wherever its environment says.
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint or not traceparent:
        return
    import json, secrets, time, urllib.request
    _, trace, parent, _ = traceparent.split("-")
    now = time.time_ns()
    span = {"traceId": trace, "spanId": secrets.token_hex(8), "parentSpanId": parent, "name": "stand-in handles",
            "kind": 2, "startTimeUnixNano": str(now - 1000), "endTimeUnixNano": str(now)}
    payload = {"resourceSpans": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": os.environ.get("OTEL_SERVICE_NAME", "?")}}]},
        "scopeSpans": [{"spans": [span]}]}]}
    request = urllib.request.Request(endpoint + "/v1/traces", data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5).read()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global answered
        answered += 1
        if dies_after and answered > dies_after:
            print("dying on request", answered, flush=True)
            os._exit(3)
        export(self.headers.get("traceparent"))
        import json
        body = json.dumps({"ok": True, "world": self.headers.get("x-minutehand-world")}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_POST = do_GET
    def log_message(self, *args):
        pass
class Server(http.server.HTTPServer):
    def server_bind(self):
        # No reverse DNS lookup of this machine's name, which stalls on some Macs.
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
print("stand-in starting", flush=True)
if os.environ.get("NEVER_READY"):
    print("stand-in will never listen", flush=True)
    import time; time.sleep(3600)
server = Server(("127.0.0.1", int(sys.argv[1])), Handler)
print("listening on", sys.argv[1], flush=True)
server.serve_forever()
"""


def stand_in(tmp_path: Path) -> list[str]:
    """The command of a stand-in emulator process: answers 200 `{"ok": true, "world": <its world header>}` to
    anything, on `{port}`."""
    script = tmp_path / "stand_in.py"
    script.write_text(STAND_IN, encoding="utf-8")
    return [sys.executable, str(script), "{port}"]
