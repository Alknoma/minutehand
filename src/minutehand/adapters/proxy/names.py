"""Every name the sandbox looks up resolves to the proxy.

A sealed sandbox (`sandbox/seal.sh`) has one route out, its gateway, where every TCP connection is redirected to the
proxy's transparent listener (`adapters/proxy/redirected.py`) and every DNS query to this answerer. A client that
ignores `HTTPS_PROXY` resolves `slack.com` here, gets the proxy's address, connects to it, and names the host in its
TLS ClientHello or `Host` header, which is what the transparent listener routes on; the address it was given never
mattered. So the answer to every A query is the proxy's address, and to every other query nothing (an empty
NOERROR answer: a client that asked AAAA first then asks A). Nothing here is a call of the agent's and nothing is
recorded: a lookup is how a connection starts, and the connection is recorded by the proxy.

The wire format is DNS's own (RFC 1035 §4.1): a 12-byte header, the question (labels, type, class), and in the
answer one resource record with the name compressed to a pointer at the question (0xC00C), type A, class IN, a TTL of
0 so nothing caches it past the run, and the four bytes of the address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import struct

A = 1
IN = 1
HEADER = struct.Struct("!HHHHHH")
FLAGS_RESPONSE = 0x8180  # QR, RD, RA: a response to a recursive query, as a resolver answers
FLAGS_FORMAT_ERROR = 0x8181
POINTER_TO_QUESTION = 0xC00C


def answer(query: bytes, address: str) -> bytes | None:
    """The answer to one DNS query: `address` for an A question, an empty answer for any other question, a FORMERR
    for bytes that are not a query, None for a response (never answered, so two answerers cannot loop)."""
    if len(query) < HEADER.size:
        return None
    ident, flags, questions, _, _, _ = HEADER.unpack_from(query)
    if flags & 0x8000:
        return None
    if questions != 1:
        return HEADER.pack(ident, FLAGS_FORMAT_ERROR, 0, 0, 0, 0)
    at = HEADER.size
    while True:
        if at >= len(query):
            return HEADER.pack(ident, FLAGS_FORMAT_ERROR, 0, 0, 0, 0)
        length = query[at]
        at += 1
        if length == 0:
            break
        if length & 0xC0:  # a pointer in a question is not something a client sends
            return HEADER.pack(ident, FLAGS_FORMAT_ERROR, 0, 0, 0, 0)
        at += length
    if at + 4 > len(query):
        return HEADER.pack(ident, FLAGS_FORMAT_ERROR, 0, 0, 0, 0)
    qtype, qclass = struct.unpack_from("!HH", query, at)
    question = query[HEADER.size : at + 4]
    if qtype != A or qclass != IN:
        return HEADER.pack(ident, FLAGS_RESPONSE, 1, 0, 0, 0) + question
    record = struct.pack("!HHHIH", POINTER_TO_QUESTION, A, IN, 0, 4) + ipaddress.IPv4Address(address).packed
    return HEADER.pack(ident, FLAGS_RESPONSE, 1, 1, 0, 0) + question + record


class _Answering(asyncio.DatagramProtocol):
    def __init__(self, address: str) -> None:
        self._address = address
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int] | tuple[str, int, int, int]) -> None:
        reply = answer(data, self._address)
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)


class NameAnswerer:
    """A UDP listener on `host`:`port` (0 for any free port) answering every lookup with `address`, the proxy's."""

    def __init__(self, host: str, port: int, *, address: str) -> None:
        self._host = host
        self._port = port
        self._address = address
        self._transport: asyncio.DatagramTransport | None = None
        self.port: int = port

    async def __aenter__(self) -> NameAnswerer:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Answering(self._address), local_addr=(self._host, self._port)
        )
        self._transport = transport
        self.port = transport.get_extra_info("sockname")[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
