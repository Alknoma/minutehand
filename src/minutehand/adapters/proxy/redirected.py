"""Connections the network sends to the proxy without the client asking for a proxy: transparent capture.

A client that ignores `HTTPS_PROXY` (Node's built-in `fetch` without `NODE_USE_ENV_PROXY`, `httplib2` without
PySocks, many Go and JVM clients) connects straight to the real host. In a Linux container that connection can be
rewritten by the container's own packet filter (`iptables -t nat ... -j DNAT`, `redirect_script`) to this listener.
mitmproxy's own transparent mode reads where the connection was going from the kernel (`SO_ORIGINAL_DST`), which
works only when the rewrite happened in the proxy's own network namespace; a rule inside the agent's container
rewrites it in another one, on another machine under Docker Desktop. So this listener reads the destination from
what the client sends first: the server name of a TLS ClientHello (port 443), or the `Host` header of a plain HTTP
request (its port, else 80). From there on the connection is handled exactly as the inside of a `CONNECT` to that
host and port: a claimed host is answered by its provider, a model host tunnelled, anything else refused or captured.

A connection that names no host (TLS without a server name, anything that is neither TLS nor HTTP) is closed: it
cannot be told apart from a call to any other address, and nothing can answer it.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Literal

from mitmproxy.net.tls import starts_like_tls_record
from mitmproxy.proxy import commands, events, layer
from mitmproxy.proxy.context import Context
from mitmproxy.proxy.layers.modes import DestinationKnown
from mitmproxy.proxy.layers.tls import parse_client_hello
from mitmproxy.proxy.mode_servers import AsyncioServerInstance
from mitmproxy.proxy.mode_specs import TCP, ProxyMode

TLS_PORT = 443
PLAIN_PORT = 80
HEAD_LIMIT = 64 * 1024
"""Bytes read while looking for a ClientHello or a request's headers; more without either is no call."""

_REQUEST_LINE = re.compile(rb"^[A-Za-z]{3,}\s+\S+\s+HTTP/\d", re.ASCII)
_HOST = re.compile(rb"\r\nhost:[ \t]*([^\r\n]*?)[ \t]*\r\n", re.IGNORECASE)


class NeedsMore(Exception):
    """What arrived so far is the start of a ClientHello or of a request's headers, not all of it."""


def destination(head: bytes) -> tuple[str, int] | None:
    """The host and port a redirected connection was for, read from its first bytes: a ClientHello's server name
    and 443, or a request's `Host` and its port or 80. None when the bytes name no host. Raises `NeedsMore` while the
    ClientHello or the headers are incomplete."""
    if starts_like_tls_record(head):
        try:
            hello = parse_client_hello(head)
        except ValueError:
            return None
        if hello is None:
            raise NeedsMore
        return (hello.sni.lower(), TLS_PORT) if hello.sni else None
    if not _REQUEST_LINE.match(head):
        return None
    end = head.find(b"\r\n\r\n")
    if end < 0:
        raise NeedsMore
    found = _HOST.search(head[: end + 2])
    if found is None or not found.group(1):
        return None
    authority = found.group(1).decode("ascii", "replace").lower()
    if authority.startswith("["):
        address, _, rest = authority[1:].partition("]")
        port = rest.removeprefix(":")
        return (address, int(port)) if port.isdigit() else (address, PLAIN_PORT)
    host, _, port = authority.partition(":")
    return (host, int(port)) if port.isdigit() else (host, PLAIN_PORT)


class RedirectedMode(ProxyMode):
    """mitmproxy's spec for this listener: `redirected@<host>:<port>`."""

    @property
    def description(self) -> str:
        return "Redirected connections (destination from the TLS server name or the Host header)"

    @property
    def transport_protocol(self) -> Literal["tcp", "udp", "both"]:
        return TCP

    def __post_init__(self) -> None:
        if self.data:
            raise ValueError("the redirected mode takes no arguments")


class Redirected(DestinationKnown):
    """The top layer of a redirected connection: holds what the client sends until it names its host, then hands the
    connection, with every byte it held, to the next layer as if a `CONNECT` had named that host."""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.head = b""

    def _handle_event(self, event: events.Event) -> layer.CommandGenerator[None]:
        if isinstance(event, events.Start):
            return
        if isinstance(event, events.ConnectionClosed):
            yield commands.CloseConnection(self.context.client)
            return
        if not isinstance(event, events.DataReceived):
            return
        self.head += event.data
        try:
            found = destination(self.head)
        except NeedsMore:
            if len(self.head) < HEAD_LIMIT:
                return
            found = None
        if found is None:
            yield commands.Log(
                f"a connection redirected to the proxy from {self.context.client.peername} named no host (no TLS "
                "server name, no Host header), so it was closed",
                logging.WARNING,
            )
            yield commands.CloseConnection(self.context.client)
            self._handle_event = self.done  # type: ignore[method-assign]
            return
        self.context.server.address = found
        self.child_layer = layer.NextLayer(self.context)
        err = yield from self.finish_start()
        if err:
            yield commands.CloseConnection(self.context.client)
            return
        yield from self.child_layer.handle_event(events.DataReceived(self.context.client, self.head))
        self.head = b""


class RedirectedInstance(AsyncioServerInstance[RedirectedMode]):
    """Registered with mitmproxy by being defined: a listener whose connections start with `Redirected`."""

    def make_top_layer(self, context: Context) -> layer.Layer:
        return Redirected(context)


def redirect_script(target: str, port: int) -> str:
    """A POSIX shell script, run as root in the agent's Linux container (it needs `iptables` and the `NET_ADMIN`
    capability), that sends every TCP connection the container opens to port 80 or 443 of an address outside this
    container's loopback and the private ranges to `target`:`port`, the proxy's redirected listener. `target` is the
    proxy's host as the container names it, resolved to an IPv4 address when the script runs. The private ranges are
    left alone because that is where the rest of a stack lives (other containers, the host's own services); every
    SaaS host Minutehand fakes resolves to a public address."""
    return f"""#!/bin/sh
# Written by `minutehand env --transparent-port {port} --format redirect`. Run it as root in the agent's container
# before the agent starts; the container needs iptables and the NET_ADMIN capability.
set -eu
target={shlex.quote(target)}
case "$target" in
  *[!0-9.]*) address=$(getent hosts "$target" | awk '$1 ~ /^[0-9.]+$/ {{print $1; exit}}') ;;
  *) address=$target ;;
esac
if [ -z "$address" ]; then
  echo "minutehand: $target does not resolve to an IPv4 address in this container" >&2
  exit 1
fi
iptables -t nat -N MINUTEHAND
for range in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 "$address/32"; do
  iptables -t nat -A MINUTEHAND -d "$range" -j RETURN
done
iptables -t nat -A MINUTEHAND -p tcp --dport 443 -j DNAT --to-destination "$address:{port}"
iptables -t nat -A MINUTEHAND -p tcp --dport 80 -j DNAT --to-destination "$address:{port}"
iptables -t nat -A OUTPUT -p tcp -j MINUTEHAND
echo "minutehand: TCP to ports 80 and 443 outside the private ranges now goes to $address:{port}" >&2
"""
