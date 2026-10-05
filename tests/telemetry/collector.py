"""A stand-in for the collector an agent already exported to: plain HTTP on 127.0.0.1, keeping each request."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Collected:
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        return next((v for k, v in self.headers if k == name), None)


@dataclass
class Collector:
    port: int = 0
    collected: list[Collected] = field(default_factory=list)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@asynccontextmanager
async def collector() -> AsyncIterator[Collector]:
    found = Collector()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
                _, path, _ = head[0].split(" ", 2)
                headers = tuple(
                    (name.strip().lower(), value.strip())
                    for name, _, value in (line.partition(":") for line in head[1:] if line)
                )
                length = int(next((v for k, v in headers if k == "content-length"), "0"))
                found.collected.append(Collected(path, headers, await reader.readexactly(length)))
                found.arrived.set()
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    found.port = server.sockets[0].getsockname()[1]
    try:
        yield found
    finally:
        server.close()
        await server.wait_closed()
