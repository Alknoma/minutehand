"""The mitmproxy addon: route each intercepted call by its host, answer or refuse it, and record it.

A claimed host is answered by its provider's ASGI app and the call is recorded as an
`Exchange` tied to the events the provider wrote while answering. An unclaimed host
is refused with 502 and recorded the same way. A model API is tunnelled without being
decrypted, unless the run edits its requests or records model calls. An edited call is
decrypted, edited and sent on. A recorded call (`record_model_calls`) is decrypted and sent
on unchanged, its answer streamed back to the agent as it arrives when it is a stream, and
kept as a span (`model_calls.span_of`); it is not an `Exchange`, and nothing from its
headers or query string is stored.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime

from mitmproxy import http, tls
from mitmproxy.addons import asgiapp
from mitmproxy.net import encoding

from minutehand.adapters.proxy import credentials, redact
from minutehand.adapters.proxy.edit import apply_edits
from minutehand.adapters.proxy.model_calls import EVENT_STREAM, Exchanged, span_of
from minutehand.adapters.proxy.policy import HostPolicy, Routing
from minutehand.adapters.proxy.worlds import Mounted, Worlds, one_run
from minutehand.application.restore import SeenCall
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.domain.telemetry import SpanSource
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
    def __init__(
        self,
        routing: Routing,
        store: Store,
        clock: Clock,
        telemetry: Telemetry | None = None,
        *,
        record_model_calls: bool = False,
    ) -> None:
        self.routing = routing
        self.worlds: Worlds = one_run(store, clock, {}, scenario=None, provider=routing.registry.provider)
        self.telemetry = telemetry
        self.record_model_calls = record_model_calls
        # The streamed answer of each recorded call, chunk by chunk as it passed through, by flow id.
        self._streams: dict[str, list[bytes]] = {}
        self._recorded: set[str] = set()
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
        self.worlds = one_run(world, clock, apps, scenario=scenario, provider=self.routing.registry.provider)

    def route(self, worlds: Worlds) -> None:
        """`minutehand serve`: from now on each call is answered in the world `worlds` finds for it."""
        self.worlds = worlds

    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        host = data.client_hello.sni
        if host is None and data.context.server.address is not None:
            host = data.context.server.address[0]
        if host is not None and self.policy(host) is HostPolicy.TUNNEL:
            self._seen(f"a new tunnelled connection to {host}")
            data.ignore_connection = True

    def policy(self, host: str) -> HostPolicy:
        """The routing's policy for `host`, with a model API this proxy records opened rather than tunnelled."""
        policy = self.routing.policy(host)
        return HostPolicy.RECORD if policy is HostPolicy.TUNNEL and self.record_model_calls else policy

    async def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        self._seen(f"{flow.request.method} {host}{redact.path(flow.request.path)}")
        policy = self.policy(host)
        if self.record_model_calls and policy in (HostPolicy.EDIT, HostPolicy.RECORD):
            self._recorded.add(flow.id)
        if policy is HostPolicy.EDIT:
            self._edit(flow, host)
            return
        if policy not in (HostPolicy.ANSWER, HostPolicy.REFUSE):
            return
        request = flow.request
        world = self.worlds.world_for(
            host,
            credentials.presented(
                authorization=_first_header(request, "authorization"),
                path=request.path,
                content_type=_first_header(request, "content-type") or "",
                body=request.get_content(strict=False) or b"",
            ),
        )
        manifest = self.routing.claimant(host)
        if policy is HostPolicy.ANSWER and manifest is not None and world is not None:
            await self._answer(flow, host, manifest, world)
            return
        refused = world or self.worlds.lobby
        async with refused.lock:
            first = refused.store.head() + 1
            if manifest is None:
                flow.response = _json_response(502, "no provider claims this host", host)
            else:
                flow.response = _json_response(
                    502, "no world claims this call: none holds its credentials or host", host
                )
            self._record(refused, flow, host, flow.request.path, first, manifest.key if manifest else None)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """A recorded call answered as a stream reaches the agent as one: each chunk is passed on as it arrives
        and kept beside, to be read when the stream ends."""
        response = flow.response
        if flow.id not in self._recorded or response is None:
            return
        if (_first_header(response, "content-type") or "").split(";", 1)[0].strip().lower() != EVENT_STREAM:
            return
        chunks = self._streams.setdefault(flow.id, [])

        def tee(chunk: bytes) -> bytes:
            chunks.append(chunk)
            return chunk

        response.stream = tee

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.id not in self._recorded:
            return
        self._recorded.discard(flow.id)
        streamed = self._streams.pop(flow.id, None)
        request, response = flow.request, flow.response
        assert response is not None
        if streamed is None:
            body = response.get_text(strict=False) or ""
        else:
            raw = b"".join(streamed)
            coding = _first_header(response, "content-encoding")
            decoded = encoding.decode(raw, coding) if coding else raw
            body = decoded.decode("utf-8", errors="replace") if isinstance(decoded, bytes) else decoded
        exchanged = Exchanged(
            host=request.pretty_host,
            path=request.path,
            status=response.status_code,
            request_body=request.get_text(strict=False) or "",
            response_body=body,
            response_type=_first_header(response, "content-type") or "",
            traceparent=_first_header(request, TRACEPARENT),
            started=datetime.fromtimestamp(request.timestamp_start, UTC),
            ended=datetime.fromtimestamp(response.timestamp_end or response.timestamp_start, UTC),
        )
        self.worlds.lobby.store.receive([span_of(exchanged)], source=SpanSource.WIRE)

    async def _answer(self, flow: http.HTTPFlow, host: str, manifest: Manifest, world: Mounted) -> None:
        async with world.lock:
            first = world.store.head() + 1
            original = flow.request.path
            exchange: Exchange | None = None
            try:
                app = world.app_for(manifest)
                first = world.store.head() + 1  # what seeding a provider on its first call wrote is not this call's
                flow.request.path = strip_prefix(original, manifest.path_prefix)
                await asgiapp.serve(app, flow)
            except Exception:
                # Never let a claimed host fall through to the real service.
                flow.response = _json_response(500, f"provider {manifest.key!r} failed to load", host)
                raise
            finally:
                flow.request.path = original
                exchange = self._record(world, flow, host, original, first, manifest.key)
        response = flow.response
        minted = (
            credentials.minted(
                content_type=_first_header(response, "content-type") or "",
                body=response.get_content(strict=False) or b"",
            )
            if response is not None and response.status_code < 400
            else []
        )
        self.worlds.answered(world, exchange, minted)

    def _edit(self, flow: http.HTTPFlow, host: str) -> None:
        try:
            edited = apply_edits(flow.request.content or b"", host, self.routing.edits_for(host))
        except Exception:
            # An edit that fails must not send the agent's request on unedited.
            flow.response = _json_response(502, "the run's model edits could not be applied", host)
            raise
        if edited is not None:
            flow.request.content = edited

    def _record(
        self, world: Mounted, flow: http.HTTPFlow, host: str, path: str, first: int, provider: str | None
    ) -> Exchange:
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
        last = world.store.head()
        world.store.attach(exchange, first_seq=first, last_seq=last, provider=provider)
        if self.telemetry is not None and last >= first:
            for event in world.store.events(since=first - 1):
                self.telemetry.recorded(event)
        return exchange
