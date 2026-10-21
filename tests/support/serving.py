"""An ASGI app served on 127.0.0.1 at an ephemeral port inside the test's own event loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn


@asynccontextmanager
async def serving(app: object) -> AsyncIterator[str]:
    """Serve an ASGI app on 127.0.0.1 at an ephemeral port inside the test's own event loop; yields its base URL."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))  # type: ignore[arg-type]
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.005)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
