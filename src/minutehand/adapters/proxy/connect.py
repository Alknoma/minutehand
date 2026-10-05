"""A `CONNECT` mitmproxy cannot parse, answered with what is wrong with it.

httpx (through at least 0.28) asks a proxy for a tunnel to an IPv6 literal without its brackets:
`CONNECT ::1:8443 HTTP/1.1`. No proxy can tell where the address ends and the port begins, and mitmproxy refuses
the request line with a bare "400 Bad Request" before any addon hook sees a flow. The proxy's `next_layer` hook does
see the first bytes of every client connection, so such a request is answered there, naming the cause.
"""

from __future__ import annotations

import ipaddress
import json

from mitmproxy import http
from mitmproxy.net.http import http1
from mitmproxy.proxy import commands, events, layer
from mitmproxy.proxy.context import Context


def unbracketed_ipv6(head: bytes) -> str | None:
    """The IPv6 address a `CONNECT` request line names without brackets, or None for any other request."""
    line = head.split(b"\r\n", 1)[0].split(b"\n", 1)[0]
    parts = line.split(b" ")
    if len(parts) != 3 or parts[0] != b"CONNECT" or parts[1].startswith(b"["):
        return None
    authority = parts[1].decode("latin-1")
    if authority.count(":") < 2:
        return None
    address, _, port = authority.rpartition(":")
    for candidate in (address, authority) if port.isdigit() else (authority,):
        try:
            return str(ipaddress.IPv6Address(candidate))
        except ValueError:
            continue
    return None


def refusal(address: str) -> bytes:
    """The answer to a `CONNECT` naming `address` without brackets, as bytes on the wire."""
    message = (
        f"CONNECT names the IPv6 address {address} without brackets, so its port cannot be told from the address; "
        f"the request line must be `CONNECT [{address}]:<port>`. httpx sends it so for every IPv6 literal through "
        "any proxy: reach the host by a name instead"
    )
    response = http.Response.make(
        400,
        json.dumps({"error": message, "host": address}).encode(),
        {"content-type": "application/json", "connection": "close"},
    )
    return http1.assemble_response(response)


class Refused(layer.Layer):
    """A client connection answered once with `answer` and closed, whatever it sends."""

    def __init__(self, context: Context, answer: bytes) -> None:
        super().__init__(context)
        self.answer = answer
        self.sent = False

    def _handle_event(self, event: events.Event) -> layer.CommandGenerator[None]:
        if self.sent or isinstance(event, events.ConnectionClosed):
            return
        self.sent = True
        yield commands.SendData(self.context.client, self.answer)
        yield commands.CloseConnection(self.context.client)
