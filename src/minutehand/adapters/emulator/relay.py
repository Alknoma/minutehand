"""A loopback relay in front of one external emulator: the proxy forwards a call to `127.0.0.1:<relay>` as plain
HTTP, and the relay carries the bytes, unread and unchanged, to the emulator's upstream wherever it is (a TCP port,
a Unix socket, an HTTPS server verified against its own CA).

The relay is why one mechanism serves every upstream: mitmproxy streams the answer back as it arrives, and the
relay alone knows two things the proxy cannot: that the upstream refused the connection, and that it accepted a
request and sent no byte of answer within `answer_within`. Either way it closes the connection and keeps why,
under the port the proxy's side of the connection came from, so the proxy can answer the agent 502 or 504 naming
the emulator, rather than hanging or saying "server disconnected".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from minutehand.domain.emulator import Upstream

WATCH_EVERY = 0.05
"""Seconds between two looks at whether a connection's request has gone unanswered too long."""


@dataclass(frozen=True)
class RelayFailure:
    """Why the relay broke off one connection."""

    reason: str
    timed_out: bool


@dataclass(frozen=True)
class Where:
    """An upstream as a socket: a TCP host and port, or a Unix socket's path; with TLS, its context and name."""

    host: str | None
    port: int | None
    path: str | None
    tls: ssl.SSLContext | None
    server_name: str | None
    prefix: str
    authority: str

    @classmethod
    def of(cls, upstream: Upstream, url: str) -> Where:
        """`url` is the upstream's URL with its `{port}` filled."""
        parts = urlsplit(url)
        if parts.scheme == "unix":
            path = unquote(parts.path or parts.netloc)
            return cls(None, None, path, None, None, "", "localhost")
        tls: ssl.SSLContext | None = None
        if parts.scheme == "https":
            tls = ssl.create_default_context(cafile=upstream.ca) if upstream.ca else ssl.create_default_context()
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return cls(host, port, None, tls, host, parts.path.rstrip("/"), parts.netloc)

    async def open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.path is not None:
            return await asyncio.open_unix_connection(self.path)
        assert self.host is not None and self.port is not None
        return await asyncio.open_connection(
            self.host, self.port, ssl=self.tls, server_hostname=self.server_name if self.tls is not None else None
        )

    def describe(self) -> str:
        if self.path is not None:
            return f"unix:{self.path}"
        return f"{'https' if self.tls is not None else 'http'}://{self.authority}"


class Relay:
    def __init__(self, name: str, where: Where, *, answer_within: float) -> None:
        self.name = name
        self.where = where
        self._limit = answer_within
        self._server: asyncio.Server | None = None
        self._failures: dict[int, RelayFailure] = {}
        self._open: set[asyncio.Task[None]] = set()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._carry, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for task in list(self._open):
            task.cancel()
        for task in list(self._open):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if server is not None:
            await server.wait_closed()

    def failure_for(self, peer_port: int) -> RelayFailure | None:
        """Why the relay broke off the connection that came from `peer_port`, if it did; asked once."""
        return self._failures.pop(peer_port, None)

    async def _carry(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._open.add(task)
        peer = int(client_writer.get_extra_info("peername")[1])
        try:
            try:
                up_reader, up_writer = await self.where.open()
            except (OSError, ssl.SSLError) as e:
                with contextlib.suppress(TimeoutError, OSError):
                    await asyncio.wait_for(client_reader.read(65536), 1.0)  # the request, before it is answered
                self._fail(
                    client_writer,
                    peer,
                    RelayFailure(
                        f"emulator {self.name} could not be reached at {self.where.describe()}: {e}", timed_out=False
                    ),
                )
                return
            await self._pipe(client_reader, client_writer, up_reader, up_writer, peer)
            up_writer.close()
            with contextlib.suppress(Exception):
                await up_writer.wait_closed()
        finally:
            client_writer.close()
            with contextlib.suppress(Exception):
                await client_writer.wait_closed()
            self._open.discard(task)

    def _fail(self, writer: asyncio.StreamWriter, peer: int, failure: RelayFailure) -> None:
        """Keep why, and answer the request waiting on this connection in the emulator's place."""
        self._failures[peer] = failure
        body = json.dumps(
            {"error": f"external emulator {self.name} is unavailable: {failure.reason}", "emulator": self.name}
        ).encode()
        status = b"504 Gateway Timeout" if failure.timed_out else b"502 Bad Gateway"
        head = b"HTTP/1.1 " + status + b"\r\ncontent-type: application/json\r\nconnection: close\r\n"
        with contextlib.suppress(OSError, RuntimeError):
            writer.write(head + b"content-length: " + str(len(body)).encode() + b"\r\n\r\n" + body)

    async def _pipe(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
        peer: int,
    ) -> None:
        loop = asyncio.get_running_loop()
        asked_at: list[float | None] = [None]

        async def out() -> None:
            while data := await client_reader.read(65536):
                if asked_at[0] is None:
                    asked_at[0] = loop.time()
                up_writer.write(data)
                await up_writer.drain()
            with contextlib.suppress(OSError):
                if up_writer.can_write_eof():
                    up_writer.write_eof()

        answered = [False]

        async def back() -> None:
            while data := await up_reader.read(65536):
                asked_at[0] = None
                answered[0] = True
                client_writer.write(data)
                await client_writer.drain()

        async def watch() -> None:
            while True:
                await asyncio.sleep(WATCH_EVERY)
                began = asked_at[0]
                if began is not None and loop.time() - began > self._limit:
                    failure = RelayFailure(
                        f"emulator {self.name} did not answer within {self._limit:g} s", timed_out=True
                    )
                    self._fail(client_writer, peer, failure)
                    return

        sending = asyncio.ensure_future(out())
        answering = asyncio.ensure_future(back())
        watching = asyncio.ensure_future(watch())
        try:
            done, _ = await asyncio.wait({answering, watching}, return_when=asyncio.FIRST_COMPLETED)
            if answering in done and answering.exception() is not None and peer not in self._failures:
                failure = RelayFailure(f"emulator {self.name} broke off: {answering.exception()}", timed_out=False)
                if answered[0]:
                    self._failures[peer] = failure
                else:
                    self._fail(client_writer, peer, failure)
            elif answering in done and asked_at[0] is not None and peer not in self._failures:
                failure = RelayFailure(f"emulator {self.name} closed the connection without answering", timed_out=False)
                if answered[0]:
                    self._failures[peer] = failure  # mid-answer: the proxy hears the connection break
                else:
                    self._fail(client_writer, peer, failure)
        finally:
            for task in (sending, answering, watching):
                task.cancel()
            for task in (sending, answering, watching):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
