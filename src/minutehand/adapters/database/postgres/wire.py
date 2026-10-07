"""PostgreSQL's frontend/backend protocol, version 3, as far as the relay and Minutehand's own connections need it:
framing, the startup packet, and the few messages read or written. Nothing here does I/O but the two readers.

Every message after the startup packet is one type byte, a big-endian int32 length that counts itself, and the
payload. The startup packet has no type byte; its first int32 after the length is a protocol version or a request
code (SSL, GSS encryption, cancel).
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Sequence

PROTOCOL_3 = 196608
SSL_REQUEST = 80877103
GSSENC_REQUEST = 80877104
CANCEL_REQUEST = 80877102
NO_ENCRYPTION = b"N"
"""The one-byte answer to an SSL or GSS encryption request that says this server does not speak it."""

MAX_MESSAGE = 1 << 30
"""PostgreSQL's own limit on a message; a longer length is a stream out of step, not a message."""


class ProtocolError(Exception):
    """Bytes that are not PostgreSQL's protocol, or not where they should be."""


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack(">I", len(payload) + 4) + payload


async def read_message(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """One message: its type byte and its payload. Raises `asyncio.IncompleteReadError` at the end of the stream."""
    header = await reader.readexactly(5)
    length = struct.unpack(">I", header[1:])[0]
    if length < 4 or length > MAX_MESSAGE:
        raise ProtocolError(f"a message of type {header[:1]!r} claims a length of {length}")
    return header[:1], await reader.readexactly(length - 4)


async def read_startup(reader: asyncio.StreamReader) -> bytes:
    """The whole startup packet, its length included, as it is forwarded."""
    head = await reader.readexactly(4)
    length = struct.unpack(">I", head)[0]
    if length < 8 or length > 10_000:
        raise ProtocolError(f"a startup packet claims a length of {length}")
    return head + await reader.readexactly(length - 4)


def startup_code(packet: bytes) -> int:
    return struct.unpack(">I", packet[4:8])[0]


def startup_params(packet: bytes) -> list[tuple[str, str]]:
    """A protocol-3 startup packet's parameters, in the order the client sent them: `user`, `database`, `options`
    and any run-time setting it asks for (`TimeZone`, `client_encoding`, `search_path`, ...)."""
    parts = packet[8:].split(b"\0")
    names, values = parts[0:-2:2], parts[1:-1:2]
    return [(n.decode(errors="replace"), v.decode(errors="replace")) for n, v in zip(names, values, strict=False) if n]


def startup_message(params: Sequence[tuple[str, str]]) -> bytes:
    body = struct.pack(">I", PROTOCOL_3) + b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in params) + b"\0"
    return struct.pack(">I", len(body) + 4) + body


class Fields:
    """A cursor over one message's payload."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._at = 0

    def cstr(self) -> str:
        end = self._payload.index(b"\0", self._at)
        text = self._payload[self._at : end].decode()
        self._at = end + 1
        return text

    def int16(self) -> int:
        (value,) = struct.unpack_from(">h", self._payload, self._at)
        self._at += 2
        return value

    def int32(self) -> int:
        (value,) = struct.unpack_from(">i", self._payload, self._at)
        self._at += 4
        return value

    def uint32(self) -> int:
        (value,) = struct.unpack_from(">I", self._payload, self._at)
        self._at += 4
        return value

    def take(self, n: int) -> bytes:
        if self._at + n > len(self._payload):
            raise ProtocolError("a message is shorter than its fields say")
        value = self._payload[self._at : self._at + n]
        self._at += n
        return value

    def byte(self) -> bytes:
        return self.take(1)

    def rest(self) -> bytes:
        value = self._payload[self._at :]
        self._at = len(self._payload)
        return value


def query(sql: str) -> bytes:
    return frame(b"Q", sql.encode() + b"\0")


def parse(name: str, sql: str, types: Sequence[int]) -> bytes:
    payload = name.encode() + b"\0" + sql.encode() + b"\0" + struct.pack(">h", len(types))
    payload += b"".join(struct.pack(">I", t) for t in types)
    return frame(b"P", payload)


def bind(
    portal: str, statement: str, formats: Sequence[int], values: Sequence[bytes | None], result_formats: Sequence[int]
) -> bytes:
    payload = portal.encode() + b"\0" + statement.encode() + b"\0"
    payload += struct.pack(">h", len(formats)) + b"".join(struct.pack(">h", f) for f in formats)
    payload += struct.pack(">h", len(values))
    for value in values:
        payload += struct.pack(">i", -1) if value is None else struct.pack(">i", len(value)) + value
    payload += struct.pack(">h", len(result_formats)) + b"".join(struct.pack(">h", f) for f in result_formats)
    return frame(b"B", payload)


def execute(portal: str, max_rows: int = 0) -> bytes:
    return frame(b"E", portal.encode() + b"\0" + struct.pack(">i", max_rows))


def sync() -> bytes:
    return frame(b"S")


def terminate() -> bytes:
    return frame(b"X")


def password(text: bytes) -> bytes:
    return frame(b"p", text)


def tag_of(payload: bytes) -> str:
    """A CommandComplete's tag: `INSERT 0 1`, `UPDATE 3`, `CREATE TABLE`."""
    return payload.rstrip(b"\0").decode()


def error_text(payload: bytes) -> str:
    """An ErrorResponse or NoticeResponse as one line: severity, message, SQLSTATE."""
    found: dict[str, str] = {}
    fields = Fields(payload)
    while True:
        code = fields.byte()
        if code == b"\0":
            break
        found[code.decode()] = fields.cstr()
    severity = found["S"] if "S" in found else "ERROR"
    message = found["M"] if "M" in found else "(no message)"
    state = f" ({found['C']})" if "C" in found else ""
    return f"{severity}: {message}{state}"
