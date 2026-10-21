"""The name answerer a sealed sandbox's lookups land on (`adapters/proxy/names.py`): asked over UDP, in DNS's own
wire format, it resolves every name to the proxy's address."""

from __future__ import annotations

import asyncio
import socket
import struct

from minutehand.adapters.proxy.names import NameAnswerer, answer

PROXY = "192.168.10.3"


def _query(name: str, qtype: int, ident: int = 0x1234) -> bytes:
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split("."))
    return struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack("!HH", qtype, 1)


def _answered_address(reply: bytes, query: bytes) -> str:
    ident, flags, questions, answers, _, _ = struct.unpack_from("!HHHHHH", reply)
    assert ident == 0x1234 and flags & 0x8000 and questions == 1 and answers == 1, reply
    assert reply[12 : len(query)] == query[12:]  # the question, echoed
    name, rtype, rclass, ttl, length = struct.unpack_from("!HHHIH", reply, len(query))
    assert (name, rtype, rclass, ttl, length) == (0xC00C, 1, 1, 0, 4)
    return socket.inet_ntoa(reply[len(query) + 12 :])


async def _ask(port: int, query: bytes) -> bytes:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_sendto(sock, query, ("127.0.0.1", port))
        return await asyncio.wait_for(loop.sock_recv(sock, 512), timeout=5)
    finally:
        sock.close()


async def test_every_name_resolves_to_the_proxy_over_udp() -> None:
    async with NameAnswerer("127.0.0.1", 0, address=PROXY) as names:
        for name in ("slack.com", "api.github.com", "anything.example"):
            query = _query(name, 1)
            assert _answered_address(await _ask(names.port, query), query) == PROXY


def test_a_question_that_is_not_for_an_address_gets_an_empty_answer_and_a_response_gets_none() -> None:
    aaaa = _query("slack.com", 28)
    reply = answer(aaaa, PROXY)
    assert reply is not None
    assert struct.unpack_from("!HHHHHH", reply)[3] == 0 and reply[12:] == aaaa[12:]
    assert answer(reply, PROXY) is None  # a response is never answered: two answerers cannot loop
    assert answer(b"\x12\x34\x01", PROXY) is None  # not even a header
    short = struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + b"\x05slack"  # a question cut short
    assert struct.unpack_from("!HHHHHH", answer(short, PROXY) or b"")[1] == 0x8181  # FORMERR
