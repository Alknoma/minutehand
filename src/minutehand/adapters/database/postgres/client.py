"""Minutehand's own connection to a PostgreSQL server: to take a base, put a database back and replay what the
agent committed, byte for byte as the agent sent it. It speaks the protocol directly, so a replayed statement goes
back with the very parameter bytes, formats and types the agent's driver chose, and its answer is read the way the
relay read the original: the CommandComplete tags and a digest of the rows.

Authentication: trust, cleartext password, MD5 and SCRAM-SHA-256, in the clear only (`domain.database`).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import secrets
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self
from urllib.parse import unquote, urlsplit

from minutehand.adapters.database.postgres import wire
from minutehand.adapters.database.postgres.conversation import param_bytes
from minutehand.domain.database import Executed, StatementProtocol


class ServerRefused(Exception):
    """The server answered an error where Minutehand needed it to succeed."""


@dataclass(frozen=True)
class Answer:
    tags: list[str]
    rows: str | None
    error: str | None
    values: list[list[str | None]]
    suspended: bool = False
    """The Execute stopped at its row limit (PortalSuspended)."""


@dataclass(frozen=True)
class Address:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def of(cls, url: str) -> Address:
        parts = urlsplit(url)
        return cls(
            host=parts.hostname or "127.0.0.1",
            port=parts.port or 5432,
            user=unquote(parts.username or "postgres"),
            password=unquote(parts.password or ""),
            database=parts.path.strip("/"),
        )

    def on(self, database: str) -> Address:
        return Address(self.host, self.port, self.user, self.password, database)


class Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def open(cls, address: Address, *, timeout: float = 10.0) -> Self:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(address.host, address.port), timeout)
        connection = cls(reader, writer)
        try:
            await asyncio.wait_for(connection._start(address), timeout)
        except BaseException:
            writer.close()
            raise
        return connection

    async def _start(self, address: Address) -> None:
        self._writer.write(
            wire.startup_message(
                [("user", address.user), ("database", address.database), ("application_name", "minutehand")]
            )
        )
        await self._writer.drain()
        scram: _Scram | None = None
        while True:
            kind, payload = await wire.read_message(self._reader)
            if kind == b"E":
                raise ServerRefused(wire.error_text(payload))
            if kind == b"Z":
                return
            if kind != b"R":
                continue
            fields = wire.Fields(payload)
            code = fields.int32()
            if code == 0:
                continue
            if code == 3:
                self._writer.write(wire.password(address.password.encode() + b"\0"))
            elif code == 5:
                salt = fields.take(4)
                inner = hashlib.md5((address.password + address.user).encode()).hexdigest().encode()
                self._writer.write(wire.password(b"md5" + hashlib.md5(inner + salt).hexdigest().encode() + b"\0"))
            elif code == 10:
                mechanisms = fields.rest().split(b"\0")
                if b"SCRAM-SHA-256" not in mechanisms:
                    raise ServerRefused(f"the server offers no SASL mechanism Minutehand speaks: {mechanisms!r}")
                scram = _Scram(address.password)
                first = scram.first().encode()
                self._writer.write(wire.password(b"SCRAM-SHA-256\0" + struct.pack(">i", len(first)) + first))
            elif code == 11 and scram is not None:
                self._writer.write(wire.password(scram.final(fields.rest().decode()).encode()))
            elif code == 12 and scram is not None:
                scram.verify(fields.rest().decode())
            else:
                raise ServerRefused(f"the server asks for an authentication Minutehand does not speak (code {code})")
            await self._writer.drain()

    async def close(self) -> None:
        with contextlib.suppress(ConnectionError):
            self._writer.write(wire.terminate())
            await self._writer.drain()
        self._writer.close()
        with contextlib.suppress(ConnectionError):
            await self._writer.wait_closed()

    async def run(self, sql: str) -> Answer:
        """A simple query; an error is in the answer, not raised."""
        self._writer.write(wire.query(sql))
        await self._writer.drain()
        return await self._answer()

    async def must(self, sql: str) -> Answer:
        """A simple query that has to succeed."""
        answer = await self.run(sql)
        if answer.error is not None:
            raise ServerRefused(f"{sql}: {answer.error}")
        return answer

    async def replay(self, executed: Executed) -> Answer:
        """One recorded statement, sent as the agent sent it."""
        [answer] = await self.pipeline([sent(executed)])
        return answer

    async def pipeline(self, units: Sequence[bytes]) -> list[Answer]:
        """Several units sent at once, each a simple query or an extended statement ending in Sync, and each one's
        answer up to its ReadyForQuery, in order: one round trip for all of them. The answers are read while the
        units are written, so a large answer cannot hold the writing up."""
        reading = asyncio.ensure_future(self._answers(len(units)))
        try:
            self._writer.write(b"".join(units))
            await self._writer.drain()
        except BaseException:
            reading.cancel()
            raise
        return await reading

    async def _answers(self, count: int) -> list[Answer]:
        return [await self._answer() for _ in range(count)]

    async def _answer(self) -> Answer:
        tags: list[str] = []
        error: str | None = None
        digest = hashlib.sha256()
        any_rows = False
        values: list[list[str | None]] = []
        suspended = False
        while True:
            kind, payload = await wire.read_message(self._reader)
            if kind == b"D":
                digest.update(payload)
                any_rows = True
                values.append(_row(payload))
            elif kind == b"C":
                tags.append(wire.tag_of(payload))
            elif kind == b"s":
                suspended = True
            elif kind == b"E" and error is None:
                error = wire.error_text(payload)
            elif kind == b"Z":
                return Answer(
                    tags=tags,
                    rows=digest.hexdigest() if any_rows else None,
                    error=error,
                    values=values,
                    suspended=suspended,
                )


def sent(executed: Executed) -> bytes:
    """A recorded statement's messages as the agent sent them: its text, or Parse, Bind, Execute and Sync with the
    very parameter bytes, formats, types and row limit."""
    if executed.protocol is StatementProtocol.SIMPLE:
        return wire.query(executed.sql)
    return (
        wire.parse("", executed.sql, executed.param_types)
        + wire.bind(
            "",
            "",
            executed.param_formats,
            [param_bytes(v, f) for v, f in zip(executed.params, executed.param_formats, strict=True)],
            executed.result_formats,
        )
        + wire.execute("", executed.max_rows)
        + wire.sync()
    )


def _row(payload: bytes) -> list[str | None]:
    fields = wire.Fields(payload)
    row: list[str | None] = []
    for _ in range(fields.int16()):
        length = fields.int32()
        row.append(None if length < 0 else fields.take(length).decode(errors="replace"))
    return row


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


class _Scram:
    """SCRAM-SHA-256 without channel binding (RFC 5802, RFC 7677), as PostgreSQL speaks it in the clear."""

    def __init__(self, password: str) -> None:
        self._password = password
        self._nonce = base64.b64encode(secrets.token_bytes(18)).decode()
        self._first_bare = f"n=,r={self._nonce}"
        self._server_signature = b""

    def first(self) -> str:
        return "n,," + self._first_bare

    def final(self, server_first: str) -> str:
        parts = dict(item.split("=", 1) for item in server_first.split(","))
        nonce, salt, rounds = parts["r"], base64.b64decode(parts["s"]), int(parts["i"])
        if not nonce.startswith(self._nonce):
            raise ServerRefused("the server's SCRAM nonce does not extend Minutehand's")
        salted = hashlib.pbkdf2_hmac("sha256", self._password.encode(), salt, rounds)
        client_key = hmac.digest(salted, b"Client Key", "sha256")
        stored = hashlib.sha256(client_key).digest()
        without_proof = f"c=biws,r={nonce}"
        message = f"{self._first_bare},{server_first},{without_proof}".encode()
        signature = hmac.digest(stored, message, "sha256")
        proof = bytes(a ^ b for a, b in zip(client_key, signature, strict=True))
        self._server_signature = hmac.digest(hmac.digest(salted, b"Server Key", "sha256"), message, "sha256")
        return f"{without_proof},p={base64.b64encode(proof).decode()}"

    def verify(self, server_final: str) -> None:
        if not server_final.startswith("v=") or base64.b64decode(server_final[2:]) != self._server_signature:
            raise ServerRefused("the server's SCRAM signature is not the one its password gives")
