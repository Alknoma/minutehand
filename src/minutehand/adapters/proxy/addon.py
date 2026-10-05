"""The mitmproxy addon: route each intercepted call by its host, answer or refuse it, and record it.

A claimed host is answered by its provider's ASGI app and the call is recorded as an
`Exchange` tied to the events the provider wrote while answering. An unclaimed host
is refused with 502 and recorded the same way. A model API is tunnelled without being
decrypted, unless the run edits its requests; then it is decrypted, edited and sent on,
and neither its request nor its response is stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping

from mitmproxy import http, tls
from mitmproxy.addons import asgiapp

from minutehand.adapters.proxy import redact
from minutehand.adapters.proxy.edit import apply_edits
from minutehand.adapters.proxy.policy import HostPolicy, Routing
from minutehand.application.restore import SeenCall
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.domain.world import Exchange
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store
from minutehand.ports.telemetry import Telemetry

logger = logging.getLogger(__name__)

TRACEPARENT = "traceparent"


def strip_prefix(path: str, prefix: str) -> str:
    """The path a provider's app sees: the real API's prefix removed, when the path is under it."""
    if not prefix:
        return path
    if path != prefix and not path.startswith((prefix + "/", prefix + "?")):
        return path
    rest = path[len(prefix) :]
    return rest if rest.startswith("/") else "/" + rest


def _json_response(status: int, message: str, host: str) -> http.Response:
    return http.Response.make(
        status, json.dumps({"error": message, "host": host}).encode(), {"content-type": "application/json"}
    )


def _first_header(message: http.Message, name: str) -> str | None:
    values = message.headers.get_all(name)
    return values[0] if values else None


class ProxyAddon:
    def __init__(self, routing: Routing, store: Store, clock: Clock, telemetry: Telemetry | None = None) -> None:
        self.routing = routing
        self.store = store
        self.clock = clock
        self.telemetry = telemetry
        self._apps: dict[str, ASGIApp] = {}
        self._scenario: Scenario | None = None
        # One answered call at a time, so the events between two reads of the head
        # are exactly the events this call produced.
        self._recording = asyncio.Lock()
        self.last_seen: SeenCall | None = None

    def _seen(self, what: str) -> None:
        """Every outbound call is seen as it starts and, when the proxy answers it, as it ends, so a checkpoint
        can wait until the agent has been quiet. A request on a tunnelled connection that is already open is
        never seen: the proxy does not read inside a tunnel."""
        self.last_seen = SeenCall(at=time.monotonic(), what=what)

    def http_connect(self, flow: http.HTTPFlow) -> None:
        self._seen(f"CONNECT {flow.request.pretty_host}:{flow.request.port}")

    def mount(
        self, world: Store, clock: Clock, apps: Mapping[ProviderKey, ASGIApp], *, scenario: Scenario | None = None
    ) -> None:
        """`application.orchestrator.Mounts`: from now on calls are recorded in `world` and each of `apps` answers
        its provider's hosts. A provider claimed but not mounted is still built on its first call, over `world`,
        and seeded then with `scenario`'s people and things, unless `world` already holds anything of it."""
        self.store = world
        self.clock = clock
        self._apps = dict(apps)
        self._scenario = scenario

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        host = data.client_hello.sni
        if host is None and data.context.server.address is not None:
            host = data.context.server.address[0]
        if host is not None and self.routing.policy(host) is HostPolicy.TUNNEL:
            self._seen(f"a new tunnelled connection to {host}")
            data.ignore_connection = True

    async def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        self._seen(f"{flow.request.method} {host}{redact.path(flow.request.path)}")
        policy = self.routing.policy(host)
        if policy is HostPolicy.ANSWER:
            manifest = self.routing.claimant(host)
            assert manifest is not None
            await self._answer(flow, host, manifest)
        elif policy is HostPolicy.EDIT:
            self._edit(flow, host)
        elif policy is HostPolicy.REFUSE:
            async with self._recording:
                first = self.store.head() + 1
                flow.response = _json_response(502, "no provider claims this host", host)
                self._record(flow, host, flow.request.path, first, None)

    def _app(self, manifest: Manifest) -> ASGIApp:
        """The provider's app for this run; built on its first call, and seeded first when it is new to the world:
        an agent calling a service the scenario never named still finds the scenario's people there."""
        if manifest.key not in self._apps:
            provider = self.routing.registry.provider(manifest)
            if self._scenario is not None and not any(e.entity.provider == manifest.key for e in self.store.events()):
                provider.seed(self._scenario, self.store)
            self._apps[manifest.key] = provider.app(self.store, self.clock)
        return self._apps[manifest.key]

    async def _answer(self, flow: http.HTTPFlow, host: str, manifest: Manifest) -> None:
        async with self._recording:
            first = self.store.head() + 1
            original = flow.request.path
            try:
                app = self._app(manifest)
                first = self.store.head() + 1  # what seeding a provider on its first call wrote is not this call's
                flow.request.path = strip_prefix(original, manifest.path_prefix)
                await asgiapp.serve(app, flow)
            except Exception:
                # Never let a claimed host fall through to the real service.
                flow.response = _json_response(500, f"provider {manifest.key!r} failed to load", host)
                raise
            finally:
                flow.request.path = original
                self._record(flow, host, original, first, manifest.key)

    def _edit(self, flow: http.HTTPFlow, host: str) -> None:
        try:
            edited = apply_edits(flow.request.content or b"", host, self.routing.edits_for(host))
        except Exception:
            # An edit that fails must not send the agent's request on unedited.
            flow.response = _json_response(502, "the run's model edits could not be applied", host)
            raise
        if edited is not None:
            flow.request.content = edited

    def _record(self, flow: http.HTTPFlow, host: str, path: str, first: int, provider: str | None) -> None:
        request, response = flow.request, flow.response
        assert response is not None
        exchange = Exchange(
            method=request.method,
            host=host,
            path=redact.path(path),
            status=response.status_code,
            request_body=redact.body(
                request.get_text(strict=False) or None, _first_header(request, "content-type") or ""
            ),
            response_body=redact.body(
                response.get_text(strict=False) or None, _first_header(response, "content-type") or ""
            ),
            traceparent=_first_header(request, TRACEPARENT),
        )
        self._seen(f"{request.method} {host}{exchange.path}")
        last = self.store.head()
        self.store.attach(exchange, first_seq=first, last_seq=last, provider=provider)
        if self.telemetry is not None and last >= first:
            for event in self.store.events(since=first - 1):
                self.telemetry.recorded(event)
