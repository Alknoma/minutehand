"""A service's push (`adapters.agent.delivery.Door`) to an agent's HTTPS receiver on a certificate no trusted CA
signed: delivered, since Minutehand checks no receiver's certificate, and recorded whole."""

from __future__ import annotations

import asyncio
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.agent.delivery import Door
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.world import EntityKind, PushSnapshot
from minutehand.ports.services import Delivered
from tests.certificates import untrusted_certificate
from tests.ports import free_port
from tests.support.scenarios import T0


async def test_a_push_to_a_receiver_on_a_certificate_no_trusted_ca_signed_is_delivered(tmp_path: Path) -> None:
    heard: list[bytes] = []

    async def hear(request: Request) -> Response:
        heard.append(await request.body())
        return Response(status_code=200)

    cert, key = untrusted_certificate(tmp_path / "receiver")
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[Route("/pushed", hear, methods=["POST"])]),
            host="127.0.0.1",
            port=port,
            log_config=None,
            lifespan="off",
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        store = SqliteStore(tmp_path / "world.db", "r1", RunClock(T0))
        delivered = await Door().push(
            store, service="hooks", item="T-1", url=f"https://127.0.0.1:{port}/pushed", body='{"ticket": "T-1"}'
        )
    finally:
        server.should_exit = True
        await serving
    assert delivered == Delivered(status=200, failure=None)
    assert heard == [b'{"ticket": "T-1"}']
    [recorded] = [e.after for e in store.events(kinds=[EntityKind.PUSH])]
    assert isinstance(recorded, PushSnapshot)
    assert (recorded.method, recorded.url, recorded.body, recorded.status) == (
        "POST",
        f"https://127.0.0.1:{port}/pushed",
        '{"ticket": "T-1"}',
        200,
    )
    assert [h.name for h in recorded.headers] == ["content-type"]
