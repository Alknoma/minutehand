"""What the agent's environment sends around the proxy, and what the proxy does with what it no longer does.

`NO_PROXY` names this machine by address only (`127.0.0.1`): every client reads an IPv4 address exactly, while a
name is read by requests, urllib, aiohttp and curl as covering every name under it, so `localhost` sent
`search.localhost` direct. `localhost` itself now reaches the proxy, which forwards it to this machine unrecorded.
A bare IPv6 literal in httpx's `CONNECT` is answered naming the cause.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
import subprocess
import threading
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import aiohttp.helpers
import httpx
import pytest
import requests.utils
import yarl

from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.session import Listen, agent_environment
from tests.proxy.support import client, exchanges
from tests.proxy.upstream import make_authority, model_api

BEYOND = ["search.localhost", "model.localhost", "api.mail.example", "fe80::1", "2001:db8::1", "10.0.0.1"]
"""Hosts that are not this machine's own name or loopback address, among them the two shapes that went direct:
a name under `localhost` (every name-suffix client) and an IPv6 address ending `::1` (requests' suffix match)."""


class _Here(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"here": self.path}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _Server(HTTPServer):
    def server_bind(self) -> None:
        """Skip the reverse DNS lookup of this machine's name `HTTPServer.server_bind` makes, slow on macOS."""
        self.socket.bind(self.server_address)
        self.server_name, self.server_port = "127.0.0.1", self.socket.getsockname()[1]


@pytest.fixture
def own_service() -> Iterator[int]:
    """A service of the agent's own on this machine."""
    server = _Server(("127.0.0.1", 0), _Here)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _handed(monkeypatch: pytest.MonkeyPatch, listen: Listen) -> str:
    environment = agent_environment(listen, 8080, "/ca.pem", {}, telemetry_port=4318)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, environment[name])
    return environment["NO_PROXY"]


def _direct(host: str) -> dict[str, bool]:
    """Whether each client library sends a call to `host` around the proxy, read with that library's own code."""
    target = f"https://[{host}]/x" if ":" in host else f"https://{host}/x"
    with httpx.Client() as http:
        by_httpx = http._transport_for_url(httpx.URL(target)) is http._transport  # pyright: ignore[reportPrivateUsage]
    try:
        aiohttp.helpers.get_env_proxy_for_url(yarl.URL(target))
        by_aiohttp = False
    except LookupError:
        by_aiohttp = True
    return {
        "requests": bool(requests.utils.should_bypass_proxies(target, None)),
        # Undocumented in the stubs, and what urllib.request.proxy_bypass reads when proxies are in the environment.
        "urllib": bool(urllib.request.proxy_bypass_environment(host)),  # pyright: ignore[reportAttributeAccessIssue]
        "httpx": by_httpx,
        "aiohttp": by_aiohttp,
    }


def test_no_host_beyond_this_machine_is_sent_direct_by_any_client_library(monkeypatch: pytest.MonkeyPatch) -> None:
    no_proxy = _handed(monkeypatch, Listen())

    assert no_proxy == "127.0.0.1"
    assert {host: _direct(host) for host in BEYOND} == {host: dict.fromkeys(_direct(host), False) for host in BEYOND}
    assert _direct("127.0.0.1") == dict.fromkeys(_direct("127.0.0.1"), True)


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
async def test_curl_sends_a_name_under_localhost_to_the_proxy(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    """curl reads NO_PROXY as covering every name under each entry; nothing is named any more."""
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        environment = {
            **os.environ,
            **agent_environment(Listen(), proxy.port, proxy.ca_bundle, {}, telemetry_port=None),
        }
        done = await asyncio.to_thread(
            subprocess.run,
            ["curl", "-s", "--max-time", "10", "http://search.localhost:9/ask"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert json.loads(done.stdout) == {"error": "no provider claims this host", "host": "search.localhost"}


def test_an_agent_elsewhere_is_handed_localhost_since_the_proxy_cannot_reach_its_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In a container, `localhost` is the container: the proxy cannot forward it, so it is named, and the doctor
    reports each declared name under it (`tests/test_doctor.py`)."""
    no_proxy = _handed(monkeypatch, Listen(agent_host="host.docker.internal"))
    assert no_proxy.split(",") == ["127.0.0.1", "host.docker.internal", "localhost"]


async def test_localhost_itself_is_forwarded_to_this_machine_unrecorded_and_unseen(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path, own_service: int
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            own = await http.get(f"http://localhost:{own_service}/jobs")
            under = await http.get(f"http://search.localhost:{own_service}/jobs")
        seen = proxy.last_call()

    assert (own.status_code, own.json()) == (200, {"here": "/jobs"})
    assert under.status_code == 502
    assert [(e.host, e.status) for _, _, e in exchanges(world_path)] == [("search.localhost", 502)]
    assert seen is not None and seen.what == "GET search.localhost/jobs"


async def test_a_tls_service_on_localhost_is_tunnelled_untouched(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    """The client trusts only the service's own CA: had the proxy opened the tunnel, it would fail."""
    authority = make_authority(tmp_path / "own-ca")
    async with (
        model_api(authority) as service,
        Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy,
        client(proxy, authority.ca_cert) as http,
    ):
        answered = await http.post(f"https://localhost:{service.port}/v1/own", content=b"{}")
        seen = proxy.last_call()
    assert answered.status_code == 200 and service.received[0].path == "/v1/own"
    assert exchanges(world_path) == [] and seen is None


async def test_a_connect_naming_an_ipv6_literal_without_brackets_is_refused_naming_the_cause(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"CONNECT 2001:db8::1:8443 HTTP/1.1\r\nHost: 2001:db8::1:8443\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 10)
        writer.close()
        async with httpx.AsyncClient(proxy=proxy.url, verify=ssl.create_default_context(), trust_env=False) as http:
            with pytest.raises(httpx.ProxyError, match="400"):
                await http.get("https://[2001:db8::1]:8443/x")
        seen = proxy.last_call()

    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400")
    refusal = json.loads(body)
    assert refusal["host"] == "2001:db8::1"
    assert "without brackets" in refusal["error"] and "httpx" in refusal["error"]
    assert seen is not None and seen.what == "CONNECT 2001:db8::1 without brackets, refused"
