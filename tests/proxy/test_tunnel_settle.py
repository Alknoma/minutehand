"""The first request on a new tunnel to a model API is awaited until its answer, past the server's session tickets:
what a sandbox whose clock Minutehand owns waits on before it is released (`application.sandbox`).

The model API is a local TLS server that sends its session tickets as soon as the handshake is done (TLS 1.3, two
tickets: what OpenSSL does by default) and answers each request two seconds after reading it. The client sends its
request straight after its `Finished`, so the tickets reach the proxy after the request, where by direction alone
they read as its answer.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from tests.proxy.support import client
from tests.proxy.upstream import Authority, make_authority

MODEL_HOST = "localhost"
ANSWER_AFTER = 2.0
REPLY = b'{"choices": []}'


@asynccontextmanager
async def slow_model_api(authority: Authority, version: ssl.TLSVersion) -> AsyncIterator[int]:
    """A model API on `version` that answers every request `ANSWER_AFTER` seconds after reading it."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(authority.server_cert, authority.server_key)
    context.minimum_version = context.maximum_version = version
    if version is ssl.TLSVersion.TLSv1_3:
        context.num_tickets = 2

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    (
                        int(line.split(b":", 1)[1])
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    ),
                    0,
                )
                await reader.readexactly(length)
                await asyncio.sleep(ANSWER_AFTER)
                writer.write(
                    b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                    + f"content-length: {len(REPLY)}\r\n\r\n".encode()
                    + REPLY
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("version", [ssl.TLSVersion.TLSv1_3, ssl.TLSVersion.TLSv1_2], ids=["tls1.3", "tls1.2"])
async def test_the_first_request_on_a_new_tunnel_is_awaited_past_the_server_s_session_tickets(
    version: ssl.TLSVersion, registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    authority = make_authority(tmp_path / "upstream-ca")
    routing = Routing(registry, model_hosts=[MODEL_HOST])
    async with (
        slow_model_api(authority, version) as port,
        Proxy(routing, store, clock, confdir=tmp_path / "ca") as proxy,
        client(proxy, authority.ca_cert) as http,
    ):
        asked = time.monotonic()
        call = asyncio.create_task(http.post(f"https://{MODEL_HOST}:{port}/v1/chat/completions", content=b"{}"))
        await asyncio.sleep(0.3)  # the handshake, the request and the tickets have all moved by now
        assert not call.done()
        waiting = proxy.waiting()
        while proxy.waiting():
            await asyncio.sleep(0.05)
        quiet_after = time.monotonic() - asked
        response = await call

    assert response.status_code == 200 and response.content == REPLY
    assert quiet_after >= ANSWER_AFTER, f"quiet {quiet_after:.2f} s after the request, before its answer"
    assert waiting == [
        f"a request on the new TLS 1.3 tunnel to {MODEL_HOST}, unanswered since the server's session tickets"
        if version is ssl.TLSVersion.TLSv1_3
        else f"a request on the open tunnel to {MODEL_HOST}"
    ]
