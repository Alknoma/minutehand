"""Connections redirected to the proxy by the network, from a client that asked for no proxy: the host is read from
the TLS server name or the `Host` header, and the call is answered, refused and recorded as a proxied one is.

The client is the standard library's `http.client`, which has no proxy support at all, with its socket opened to the
redirected listener instead of the host's address: what `iptables -t nat ... -j DNAT` in the agent's container does to
it (`adapters.proxy.redirected.redirect_script`; the same through a real container is
`tests/providers/slack/test_slack_redirected_container.py`)."""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import ssl
from pathlib import Path

import pytest

from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.redirected import NeedsMore, destination
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from tests.proxy.support import exchanges


class Redirected(http.client.HTTPSConnection):
    """`http.client` to `host` over TLS, its TCP connection sent to `port` on this machine, as a DNAT rule sends it."""

    def __init__(self, host: str, port: int, trust: ssl.SSLContext, *, server_name: str | None) -> None:
        super().__init__(host, context=trust, timeout=10)
        self.redirect_to = port
        self.trust = trust
        self.server_name = server_name

    def connect(self) -> None:
        raw = socket.create_connection(("127.0.0.1", self.redirect_to), timeout=10)
        self.sock = self.trust.wrap_socket(raw, server_hostname=self.server_name)


def _get(host: str, port: int, trust: ssl.SSLContext, path: str) -> tuple[int, bytes]:
    connection = Redirected(host, port, trust, server_name=host)
    try:
        connection.request("GET", path)
        answer = connection.getresponse()
        return answer.status, answer.read()
    finally:
        connection.close()


def _plain(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        received = b""
        while chunk := sock.recv(65536):
            received += chunk
        return received


async def test_a_tls_connection_is_answered_by_the_provider_its_server_name_names(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca", redirect_port=0) as proxy:
        assert proxy.redirect_port is not None and proxy.redirect_port not in (0, proxy.port)
        trust = ssl.create_default_context(cafile=str(proxy.ca_cert))
        status, body = await asyncio.to_thread(_get, "ledger.example", proxy.redirect_port, trust, "/api/v2/whoami")
    assert status == 200
    assert json.loads(body) == {**json.loads(body), "host": "ledger.example", "path": "/whoami"}
    [(_, _, exchange)] = exchanges(world_path)
    assert (exchange.host, exchange.path, exchange.status) == ("ledger.example", "/api/v2/whoami", 200)


async def test_a_plain_request_is_answered_by_the_provider_its_host_header_names(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca", redirect_port=0) as proxy:
        assert proxy.redirect_port is not None
        port = proxy.redirect_port
        request = b"GET /api/v2/whoami HTTP/1.1\r\nHost: ledger.example\r\nConnection: close\r\n\r\n"
        received = await asyncio.to_thread(_plain, port, request)
    assert received.startswith(b"HTTP/1.1 200")
    assert b'"host":"ledger.example"' in received.replace(b" ", b"")
    [(_, _, exchange)] = exchanges(world_path)
    assert exchange.host == "ledger.example"


async def test_a_host_nobody_claims_is_refused_and_recorded_as_through_the_proxy(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca", redirect_port=0) as proxy:
        assert proxy.redirect_port is not None
        trust = ssl.create_default_context(cafile=str(proxy.ca_cert))
        status, _ = await asyncio.to_thread(_get, "nobody.example", proxy.redirect_port, trust, "/x")
    assert status == 502
    [(_, _, exchange)] = exchanges(world_path)
    assert (exchange.host, exchange.status) == ("nobody.example", 502)


async def test_a_tls_connection_without_a_server_name_is_closed_unrecorded_and_refused(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca", redirect_port=0) as proxy:
        assert proxy.redirect_port is not None
        trust = ssl.create_default_context(cafile=str(proxy.ca_cert))
        trust.check_hostname = False
        nameless = Redirected("ledger.example", proxy.redirect_port, trust, server_name=None)
        with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
            await asyncio.to_thread(nameless.request, "GET", "/api/v2/whoami")
        nameless.close()
    assert exchanges(world_path) == []


async def test_a_model_host_is_relayed_unopened_and_its_burst_recorded_as_through_a_tunnel(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    upstream = await asyncio.start_server(_answer_once, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    async with (
        upstream,
        Proxy(
            Routing(registry, model_hosts=["localhost"]), store, clock, confdir=tmp_path / "ca", redirect_port=0
        ) as proxy,
    ):
        assert proxy.redirect_port is not None
        request = f"POST /v1/chat HTTP/1.1\r\nHost: localhost:{port}\r\nContent-Length: 2\r\n\r\n{{}}".encode()
        received = await asyncio.to_thread(_plain, proxy.redirect_port, request)
    assert received.endswith(b"model said hello")
    [call] = [c for c in store.calls() if c.exchange.tunnelled is not None]
    assert (call.exchange.method, call.exchange.path) == ("CONNECT", f"localhost:{port}")


async def test_a_tls_connection_to_a_model_host_is_relayed_as_a_tunnel_and_recorded(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    """Relayed to the model host's port 443 unopened: the client's ClientHello reaches the server there, which answers
    with a TLS alert, and the burst is recorded. Port 443 is what the redirect takes a TLS connection to have been for,
    so the test needs to bind it; skipped where that takes privileges (Linux, as a user)."""
    hellos: list[bytes] = []

    async def refuse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        hellos.append(await reader.read(65536))
        writer.write(b"\x15\x03\x03\x00\x02\x02\x28")  # alert: handshake_failure
        await writer.drain()
        writer.close()

    try:
        upstream = await asyncio.start_server(
            refuse, "0.0.0.0", 443
        )  # macOS lets a user bind 443 on every interface only
    except OSError as e:
        pytest.skip(f"port 443 cannot be bound here: {e}")
    async with (
        upstream,
        Proxy(
            Routing(registry, model_hosts=["localhost"]), store, clock, confdir=tmp_path / "ca", redirect_port=0
        ) as proxy,
    ):
        assert proxy.redirect_port is not None
        trust = ssl.create_default_context(cafile=str(proxy.ca_cert))
        with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
            await asyncio.to_thread(_get, "localhost", proxy.redirect_port, trust, "/v1/models")
    [call] = [c for c in store.calls() if c.exchange.tunnelled is not None]
    assert (call.exchange.method, call.exchange.path) == ("CONNECT", "localhost:443")


async def _answer_once(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readuntil(b"\r\n\r\n{}")
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\nConnection: close\r\n\r\nmodel said hello")
    await writer.drain()
    writer.close()


def test_the_destination_is_read_from_the_host_header_with_its_port() -> None:
    assert destination(b"GET / HTTP/1.1\r\nHost: Slack.com\r\n\r\n") == ("slack.com", 80)
    assert destination(b"POST /a HTTP/1.1\r\nhost: hooks.example:8080\r\nx: y\r\n\r\n") == ("hooks.example", 8080)
    assert destination(b"GET / HTTP/1.1\r\nHost: [2001:db8::1]:81\r\n\r\n") == ("2001:db8::1", 81)
    assert destination(b"GET / HTTP/1.1\r\nAccept: */*\r\n\r\n") is None
    assert destination(b"SSH-2.0-OpenSSH_9.6\r\n") is None
    with pytest.raises(NeedsMore):
        destination(b"GET / HTTP/1.1\r\nHost: slack.com\r\n")


def test_an_incomplete_client_hello_waits_for_the_rest() -> None:
    hello = _client_hello("slack.com")
    with pytest.raises(NeedsMore):
        destination(hello[:40])
    assert destination(hello) == ("slack.com", 443)


def _client_hello(server_name: str) -> bytes:
    """The first bytes a TLS client sends for `server_name`, made by the standard library's own TLS."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname=server_name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()
