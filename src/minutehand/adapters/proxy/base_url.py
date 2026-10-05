"""Base-URL mode: a client that cannot be given a proxy but can be given a base URL.

The proxy's own listener also answers plain requests addressed to itself at
`http://<minutehand>/_host/<real-host>[:<port>]/<path>`, and HTTPS ones on a certificate its own CA mints for the
name the client asked for. `BaseUrls.requestheaders` rewrites such a request, before any other hook reads it, into
the request the client would have sent to `<real-host>` through the proxy (`https`, the real host in the URL and in
`Host`, the path after it), so everything after it — the provider's answer, the capture declarations, the record
with the real host's name, the world a standing proxy routes it to — is what a proxied call gets.

The answer is then rewritten for the client that came in this way: every absolute URL in `Location`,
`Content-Location` and `Link`, and in a text body the proxy answered itself, whose host a provider claims (and whose
path is under that provider's `path_prefix`) or which is the call's own host, is put in the same base-URL form, so a
pagination link, an upload session, a redirect or a download URL leads back through the proxy. The record keeps the
answer as the provider gave it: an answered call is recorded before the `response` hook, which this addon reaches
only after `ProxyAddon` (it is added after it).

A request is taken for a base-URL one when it is not inside a `CONNECT` tunnel, its path starts with `/_host/`, and
its own host is one nothing routes (no provider claims it, it is not a model host): a proxied plain-HTTP call to a
real host whose path happens to start so would otherwise be refused anyway.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from mitmproxy import http

from minutehand.adapters.proxy.policy import HostPolicy, Routing

PREFIX = "/_host/"
REWRITTEN_HEADERS = ("location", "content-location", "link")
_TEXT = re.compile(r"^text/|^application/.*(json|xml|javascript|x-www-form-urlencoded)")
_URL = re.compile(rb"(https?):(\\?/)\\?/([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?)")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$")


@dataclass(frozen=True)
class Target:
    """Where a base-URL request is for: the real host, its port when one was named, and the path on it."""

    host: str
    port: int | None
    path: str

    @property
    def authority(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"


def base_url(origin: str, host: str) -> str:
    """The base URL that reaches `host` through the proxy at `origin` (`http://127.0.0.1:8080`)."""
    return f"{origin.rstrip('/')}{PREFIX}{host}"


def target(path: str) -> Target | str:
    """The real host and path a `/_host/...` request target names, or why it names none."""
    rest = path[len(PREFIX) :]
    cut = min((i for i in (rest.find("/"), rest.find("?")) if i >= 0), default=len(rest))
    authority, tail = rest[:cut], rest[cut:]
    if not _HOST.match(authority):
        return f"{authority!r} is not a host: a base URL is {PREFIX}<host>[:<port>]/<path>"
    host, _, port = authority.partition(":")
    if tail.startswith("?") or not tail:
        tail = "/" + tail
    return Target(host=host.lower(), port=int(port) if port else None, path=tail)


@dataclass
class _Rebased:
    """A request that came in by base URL: the origin it was addressed to, and the real host it was for."""

    origin: str
    host: str


class BaseUrls:
    """The mitmproxy addon for base-URL requests. Added after `ProxyAddon`, so its `response` hook runs after the
    call was recorded."""

    def __init__(self, routing: Routing) -> None:
        self.routing = routing
        self._flows: dict[str, _Rebased] = {}

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        request = flow.request
        if flow.server_conn.address is not None or not request.path.startswith(PREFIX):
            return  # inside a tunnel, or not a base-URL request
        if self.routing.policy(request.pretty_host) is not HostPolicy.REFUSE:
            return  # addressed to a host something routes: a proxied call, whatever its path
        origin = f"{'https' if flow.client_conn.tls_established else 'http'}://{request.host_header or request.host}"
        found = target(request.path)
        if isinstance(found, str):
            flow.response = http.Response.make(
                400, json.dumps({"error": found}).encode(), {"content-type": "application/json"}
            )
            return
        request.scheme = "https"
        request.host = found.host
        request.port = found.port if found.port is not None else 443
        request.host_header = found.authority
        request.path = found.path
        self._flows[flow.id] = _Rebased(origin=origin, host=found.authority.lower())

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """A streamed answer's headers leave before `response`: rewrite them now."""
        rebased = self._flows.get(flow.id)
        if rebased is not None and flow.response is not None:
            _rewrite_headers(flow.response, rebased.origin, self._routable(rebased))

    def response(self, flow: http.HTTPFlow) -> None:
        rebased = self._flows.pop(flow.id, None)
        response = flow.response
        if rebased is None or response is None:
            return
        routable = self._routable(rebased)
        _rewrite_headers(response, rebased.origin, routable)
        if response.stream:
            return  # already on its way to the client as it arrived
        kind = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        content = response.get_content(strict=False)
        if not content or not _TEXT.search(kind):
            return
        rewritten = rewrite(content, rebased.origin, routable)
        if rewritten != content:
            response.set_content(rewritten)

    def error(self, flow: http.HTTPFlow) -> None:
        self._flows.pop(flow.id, None)

    def _routable(self, rebased: _Rebased) -> Callable[[str, bytes], bool]:
        def routable(authority: str, rest: bytes) -> bool:
            manifest = self.routing.claimant(authority.split(":", 1)[0].lower())
            if manifest is None:
                return authority.lower() == rebased.host
            prefix = manifest.path_prefix.encode()
            if not prefix:
                return True
            path = rest.replace(b"\\/", b"/")
            return path.startswith(prefix) and path[len(prefix) : len(prefix) + 1] in (b"", b"/", b"?", b'"', b"#")

        return routable


def rewrite(text: bytes, origin: str, routable: Callable[[str, bytes], bool]) -> bytes:
    """`text` with every absolute URL `routable` accepts put in base-URL form against `origin`. A URL written with
    JSON's escaped slashes (`https:\\/\\/`) is rewritten with them escaped too."""

    def one(found: re.Match[bytes]) -> bytes:
        authority = found.group(3).decode()
        rest = text[found.end() : found.end() + 256]
        if not routable(authority, rest):
            return found.group(0)
        rebased = base_url(origin, authority).encode()
        return rebased.replace(b"/", b"\\/") if found.group(2) == b"\\/" else rebased

    return _URL.sub(one, text)


def _rewrite_headers(response: http.Response, origin: str, routable: Callable[[str, bytes], bool]) -> None:
    for name in REWRITTEN_HEADERS:
        values = response.headers.get_all(name)
        if values:
            response.headers.set_all(
                name, [rewrite(v.encode("latin-1"), origin, routable).decode("latin-1") for v in values]
            )
