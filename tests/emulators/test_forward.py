"""What goes in is what comes out, through an external emulator: a forwarded call reaches the emulator as the agent
sent it but for the headers `domain.emulator.ADDED_HEADERS` names and `traceparent`, and its answer reaches the
agent untouched, streamed, from a TCP port, a Unix socket or an HTTPS server with a CA of its own. The copy kept
with the run is the agent's own call, with no secret in it."""

from __future__ import annotations

import shutil
import ssl
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.emulator import ADDED_HEADERS, ExternalEmulator, Upstream
from minutehand.domain.outbound import Forward, HostHeader
from minutehand.domain.world import AnsweredBy, CallOutcome, CaptureMode
from tests.emulators.support import Reply, forwarding, raw_upstream
from tests.proxy.support import client, stored_bytes
from tests.proxy.upstream import Authority, make_authority

HOST = "api.tracker.test"
ODD_JSON = b'{ "query" :"mutation IssueCreate { x }",\n\t"title":  "caf\\u00e9 \xc3\xa9",   "n": 1.0e2 }\r\n'
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
SECRET = "Bearer emulator-secret-7c21"
# What a proxy may rightly change: headers that describe one hop of the connection, not the message.
ONE_HOP = {"connection", "proxy-connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}


def _head(raw: bytes) -> tuple[str, dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers, body


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A Unix socket's path must fit in about a hundred bytes, which a test's own directory does not."""
    made = Path(tempfile.mkdtemp(prefix="mh", dir="/tmp"))
    yield made
    shutil.rmtree(made, ignore_errors=True)


async def test_a_forwarded_call_reaches_the_emulator_unchanged_but_for_the_documented_headers(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    clock.begin_wake()
    answered = b'{"data":{"issueCreate":{"id":"ISS-1"}}, "spacing" :  true}'
    reply = Reply(
        head=b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nx-emulator: kept\r\ncontent-length: "
        + str(len(answered)).encode()
        + b"\r\n\r\n",
        chunks=[answered],
    )
    async with raw_upstream(reply) as raw:
        emulator = ExternalEmulator(name="tracker", upstream=Upstream(url=f"http://127.0.0.1:{raw.port}"))
        declared = Forward(host=HOST, emulator="tracker", strip="/api", prefix="/tracker")
        async with forwarding(registry, store, clock, tmp_path, [emulator], [declared]) as running:
            proxy = running.proxy
            async with client(proxy, proxy.ca_cert) as http:
                sent_headers = {
                    "content-type": "application/json",
                    "authorization": SECRET,
                    "x-mixed-case": "VaLuE  with  two spaces",
                    "traceparent": TRACEPARENT,
                    "accept-encoding": "identity",
                }
                got = await http.post(
                    f"https://{HOST}/api/graphql?q=a%20b&Empty=", content=ODD_JSON, headers=sent_headers
                )

    [received] = raw.requests
    line, headers, body = _head(received)
    assert line == "POST /tracker/graphql?q=a%20b&Empty= HTTP/1.1"
    assert body == ODD_JSON
    agent_sent = {k.lower(): v for k, v in got.request.headers.items() if k.lower() not in ONE_HOP}
    at_emulator = {k: v for k, v in headers.items() if k not in ONE_HOP}
    # Every header differs in exactly these, and in nothing else.
    differing = {k for k in agent_sent.keys() | at_emulator.keys() if agent_sent.get(k) != at_emulator.get(k)}
    assert differing == {*ADDED_HEADERS, "traceparent"}
    assert headers["host"] == HOST
    assert headers["x-minutehand-world"] == store.run_id and headers["x-minutehand-wake"] == "1"
    assert headers["x-minutehand-time"] == clock.now().isoformat()
    # The emulator's spans join the agent's trace, under a span of Minutehand's.
    version, trace, parent, flags = headers["traceparent"].split("-")
    assert (version, trace, flags) == ("00", "4bf92f3577b34da6a3ce929d0e0e4736", "01") and parent != "00f067aa0ba902b7"

    assert got.content == answered and got.headers["x-emulator"] == "kept"
    assert not any(h in {k.lower() for k in got.headers} for h in ADDED_HEADERS)

    [kept] = [c for c in store.calls() if c.exchange.captured is not None]
    exchange, captured = kept.exchange, kept.exchange.captured
    assert captured is not None
    assert exchange.path == "/api/graphql?q=a%20b&Empty=" and exchange.host == HOST
    assert exchange.traceparent == TRACEPARENT and captured.forwarded_traceparent == headers["traceparent"]
    assert captured.mode is CaptureMode.FORWARD and captured.answered_by is AnsweredBy.EMULATOR
    assert captured.emulator == "tracker" and captured.operation == "IssueCreate"
    assert exchange.outcome is CallOutcome.ANSWERED
    assert exchange.response_body is not None and exchange.response_body.encode() == answered
    kept_bytes = stored_bytes(world_path)
    assert b"IssueCreate" in kept_bytes and b"emulator-secret-7c21" not in kept_bytes


async def test_the_upstream_host_is_sent_when_declared(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with raw_upstream(Reply()) as raw:
        emulator = ExternalEmulator(name="tracker", upstream=Upstream(url=f"http://127.0.0.1:{raw.port}/base"))
        declared = Forward(host=HOST, emulator="tracker", host_header=HostHeader.UPSTREAM)
        async with forwarding(registry, store, clock, tmp_path, [emulator], [declared]) as running:
            async with client(running.proxy, running.proxy.ca_cert) as http:
                await http.get(f"https://{HOST}/issues")
    line, headers, _ = _head(raw.requests[0])
    assert line == "GET /base/issues HTTP/1.1" and headers["host"] == f"127.0.0.1:{raw.port}"


async def test_an_answer_reaches_the_agent_chunk_by_chunk_as_the_emulator_writes_it(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    chunks = [b"5\r\nfirst\r\n", b"6\r\nsecond\r\n", b"5\r\nthird\r\n", b"0\r\n\r\n"]
    reply = Reply(
        head=b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\ntransfer-encoding: chunked\r\n\r\n",
        chunks=chunks,
        pause=0.3,
    )
    arrived: list[float] = []
    async with raw_upstream(reply) as raw:
        emulator = ExternalEmulator(name="tracker", upstream=Upstream(url=f"http://127.0.0.1:{raw.port}"))
        async with forwarding(
            registry, store, clock, tmp_path, [emulator], [Forward(host=HOST, emulator="tracker")]
        ) as running:
            async with (
                client(running.proxy, running.proxy.ca_cert) as http,
                http.stream("GET", f"https://{HOST}/events") as got,
            ):
                body = b""
                async for piece in got.aiter_raw():
                    arrived.append(time.monotonic())
                    body += piece
    assert body == b"firstsecondthird"
    # The first piece was in the agent's hands before the emulator had written the last.
    assert arrived[0] < raw.wrote_at[-2]
    [kept] = [c for c in store.calls() if c.exchange.captured is not None]
    assert kept.exchange.captured is not None and kept.exchange.captured.streamed
    assert kept.exchange.response_body == "firstsecondthird"


async def test_an_emulator_on_a_unix_socket_is_forwarded_to(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, short_dir: Path
) -> None:
    socket_path = short_dir / "e.sock"
    async with raw_upstream(Reply(), unix=socket_path) as raw:
        emulator = ExternalEmulator(name="tracker", upstream=Upstream(url=f"unix://{socket_path}"))
        async with forwarding(
            registry, store, clock, tmp_path, [emulator], [Forward(host=HOST, emulator="tracker")]
        ) as running:
            async with client(running.proxy, running.proxy.ca_cert) as http:
                got = await http.get(f"https://{HOST}/issues/1")
    assert got.status_code == 200 and got.content == b"{}"
    assert raw.requests[0].startswith(b"GET /issues/1 HTTP/1.1\r\n")


async def test_an_https_emulator_is_verified_against_its_own_ca(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    authority: Authority = make_authority(tmp_path / "emulator-ca")
    tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    tls.load_cert_chain(authority.server_cert, authority.server_key)
    async with raw_upstream(Reply(), tls=tls) as raw:
        emulator = ExternalEmulator(
            name="tracker", upstream=Upstream(url=f"https://localhost:{raw.port}", ca=str(authority.ca_cert))
        )
        async with forwarding(
            registry, store, clock, tmp_path, [emulator], [Forward(host=HOST, emulator="tracker")]
        ) as running:
            async with client(running.proxy, running.proxy.ca_cert) as http:
                got = await http.get(f"https://{HOST}/issues")
    assert got.status_code == 200
    assert raw.requests[0].startswith(b"GET /issues HTTP/1.1\r\n")
