"""The mitmproxy addon: route each intercepted call by its host, answer or refuse it, and record it.

A claimed host is answered by its provider's ASGI app and the call is recorded as an
`Exchange` tied to the events the provider wrote while answering. An unclaimed host
is refused with 502 and recorded the same way. A model API is tunnelled without being
decrypted, unless the run edits its requests or records model calls. An edited call is
decrypted, edited and sent on. A recorded call (`record_model_calls`) is decrypted and sent
on unchanged, its answer streamed back to the agent as it arrives when it is a stream, and
kept as a span (`model_calls.span_of`); it is not an `Exchange`, and nothing from its
headers or query string is stored.

A host no provider claims that the call's world declares outbound (`domain.outbound`) is captured
(`adapters.proxy.capture`): acknowledged with the declared answer, passed through to the real host, or answered
from a recording, and kept as an `Exchange` carrying `Captured`. With `capture_unknown`, an undeclared one is
passed through and kept the same way rather than refused. A pass-through answer reaches the agent chunk by chunk
as it arrives, by the same tee a recorded model call's stream uses.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import unquote

from mitmproxy import http, tcp, tls
from mitmproxy.addons import asgiapp
from mitmproxy.net import encoding
from mitmproxy.proxy import layer, layers
from mitmproxy.proxy.layers import modes

from minutehand.adapters.proxy import capture, connect, credentials, redact
from minutehand.adapters.proxy.capture import Capturing, Declaration
from minutehand.adapters.proxy.edit import apply_edits
from minutehand.adapters.proxy.hosts import loopback_name
from minutehand.adapters.proxy.model_calls import EVENT_STREAM, Exchanged, span_of
from minutehand.adapters.proxy.policy import HostPolicy, Routing
from minutehand.adapters.proxy.tunnel import Tunnel
from minutehand.adapters.proxy.worlds import Mounted, Worlds, one_run
from minutehand.application.restore import SeenCall
from minutehand.domain.outbound import BODY_LIMIT, Acknowledge, OnMiss, PassThrough
from minutehand.domain.provider import Manifest, world_keys
from minutehand.domain.scenario import ProviderKey, Scenario
from minutehand.domain.telemetry import SpanSource
from minutehand.domain.world import (
    Actor,
    AnsweredBy,
    BodyKept,
    Captured,
    CaptureMode,
    Change,
    EntityKind,
    EntityRef,
    Exchange,
    MessageSnapshot,
    Operation,
    Recipient,
)
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, Message, Scope
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


TEE_LIMIT = 64 * 1024 * 1024
"""Bytes of a passed-through answer held to be kept; past this the answer still reaches the agent whole, and
is kept as its length only."""

REPLAYED_HEADER = "x-minutehand-replayed"


@dataclass
class _Passing:
    """A captured call sent on to the real host, until its answer has passed."""

    world: Mounted
    declaration: Declaration | None
    mode: CaptureMode
    note: str | None
    chunks: list[bytes] = field(default_factory=lambda: list[bytes]())
    size: int = 0
    streamed: bool = False


class ProxyAddon:
    def __init__(
        self,
        routing: Routing,
        store: Store,
        clock: Clock,
        telemetry: Telemetry | None = None,
        *,
        record_model_calls: bool = False,
        capturing: Capturing | None = None,
        capture_unknown: bool = False,
    ) -> None:
        self.routing = routing
        self.capturing = capturing or Capturing()
        self.capture_unknown = capture_unknown
        self.worlds: Worlds = one_run(
            store, clock, {}, scenario=None, provider=routing.registry.provider, capturing=self.capturing
        )
        self.telemetry = telemetry
        self.record_model_calls = record_model_calls
        # Each captured call on its way to the real host, by flow id, until its answer has passed.
        self._passing: dict[str, _Passing] = {}
        # The streamed answer of each recorded call, chunk by chunk as it passed through, by flow id.
        self._streams: dict[str, list[bytes]] = {}
        self._recorded: set[str] = set()
        self.last_seen: SeenCall | None = None
        # Calls sent on to a real host and not answered yet, by flow id; tunnels by flow id, each with what its
        # bytes tell of whether the agent awaits an answer on it (`tunnel.Tunnel`).
        self._sent_on: dict[str, str] = {}
        self._tunnels: dict[str, Tunnel] = {}

    def _seen(self, what: str) -> None:
        """Every outbound call is seen as it starts and, when the proxy answers it, as it ends, so a checkpoint
        can wait until the agent has been quiet. A tunnel the proxy does not open is seen as its bytes move."""
        self.last_seen = SeenCall(at=time.monotonic(), what=what)

    def waiting(self) -> list[str]:
        """What the agent sent and has not had answered: a call sent on to a real host, and a tunnel on which a
        request went from the agent and nothing since reads as its answer (`tunnel.Tunnel`: a TLS 1.3 server's
        session tickets do not)."""
        sent = list(self._sent_on.values())
        tunnels = [awaiting for tunnel in self._tunnels.values() if (awaiting := tunnel.awaiting()) is not None]
        return sent + tunnels

    def forwarded(self, host: str) -> bool:
        """`localhost` itself, which nothing claims or declares: the agent's environment no longer sends it direct
        (`session.Listen.direct`), so the proxy sends it on to this machine untouched, unrecorded and unseen, as
        if it had gone direct. A name under `localhost` is a host like any other."""
        return (
            loopback_name(host)
            and self.routing.policy(host) is HostPolicy.REFUSE
            and self.worlds.lobby.capturing.find(host) is None
        )

    def http_connect(self, flow: http.HTTPFlow) -> None:
        if self.forwarded(flow.request.pretty_host):
            return
        self._seen(f"CONNECT {flow.request.pretty_host}:{flow.request.port}")

    def next_layer(self, nextlayer: layer.NextLayer) -> None:
        """A tunnel to a model API the run neither edits nor records is relayed as bytes, never decrypted, as a
        TCP flow rather than an ignored connection, so the bytes it carries are seen (`tcp_message`): a request
        on a tunnel that was already open is activity like any other call. mitmproxy's own NextLayer addon has
        chosen first; this replaces its choice for those hosts only."""
        context = nextlayer.context
        chosen = nextlayer.layer
        if isinstance(chosen, layers.HttpLayer) and context.layers[-2:] == [context.layers[0], chosen]:
            # The client's own connection to the proxy, about to be read as HTTP: mitmproxy would refuse a CONNECT
            # it cannot parse with a bare 400, before any hook sees a flow.
            address6 = connect.unbracketed_ipv6(nextlayer.data_client())
            if address6 is not None and isinstance(context.layers[0], modes.HttpProxy):
                self._seen(f"CONNECT {address6} without brackets, refused")
                context.layers.remove(chosen)
                nextlayer.layer = connect.Refused(context, connect.refusal(address6))
            return
        address = context.server.address
        if context.client.transport_protocol != "tcp" or address is None:
            return
        if not any(isinstance(lay, layers.HttpLayer) for lay in context.layers):
            return  # not the inside of a CONNECT
        if self.forwarded(str(address[0])):
            nextlayer.layer = layers.TCPLayer(context, ignore=True)
            return
        if isinstance(nextlayer.layer, layers.TCPLayer) or self.policy(str(address[0])) is not HostPolicy.TUNNEL:
            return
        nextlayer.layer = layers.TCPLayer(context)

    def tcp_start(self, flow: tcp.TCPFlow) -> None:
        host = str(flow.server_conn.address[0]) if flow.server_conn.address else "?"
        self._seen(f"a new tunnelled connection to {host}")
        self._tunnels[flow.id] = Tunnel(host)

    def tcp_message(self, flow: tcp.TCPFlow) -> None:
        message = flow.messages[-1]
        tunnel = self._tunnels.setdefault(flow.id, Tunnel("?"))
        tunnel.moved(message.content, from_client=message.from_client)
        self._seen(f"bytes {'to' if message.from_client else 'from'} {tunnel.host} on an open tunnel")
        del flow.messages[:-1]  # bytes are relayed, not kept: a long-lived tunnel would grow without end

    def tcp_end(self, flow: tcp.TCPFlow) -> None:
        self._tunnels.pop(flow.id, None)

    def tcp_error(self, flow: tcp.TCPFlow) -> None:
        self._tunnels.pop(flow.id, None)

    def mount(
        self, world: Store, clock: Clock, apps: Mapping[ProviderKey, ASGIApp], *, scenario: Scenario | None = None
    ) -> None:
        """`application.orchestrator.Mounts`: from now on calls are recorded in `world` and each of `apps` answers
        its provider's hosts. A provider claimed but not mounted is still built on its first call, over `world`,
        and seeded then with `scenario`'s people and things, unless `world` already holds anything of it."""
        self.worlds = one_run(
            world, clock, apps, scenario=scenario, provider=self.routing.registry.provider, capturing=self.capturing
        )

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
        return HostPolicy.RECORD if policy is HostPolicy.TUNNEL and self._records(host) else policy

    def _records(self, host: str) -> bool:
        """Whether a model call to `host` is kept as a span: every one, under `record_model_calls`, or one to a
        model host a world declared with `record`."""
        return self.record_model_calls or self.routing.records(host)

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            return  # answered as its headers arrived: a base-URL request that names no host (`base_url`)
        host = flow.request.pretty_host
        if self.forwarded(host):
            return
        self._seen(f"{flow.request.method} {host}{redact.path(flow.request.path)}")
        policy = self.policy(host)
        if policy in (HostPolicy.EDIT, HostPolicy.RECORD) and self._records(host):
            self._recorded.add(flow.id)
        if policy is HostPolicy.EDIT:
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            self._edit(flow, host)
            return
        if policy not in (HostPolicy.ANSWER, HostPolicy.REFUSE):
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            return
        request = flow.request
        manifest = self.routing.claimant(host)
        world = self.worlds.world_for(
            host,
            credentials.presented(
                authorization=_first_header(request, "authorization"),
                path=request.path,
                content_type=_first_header(request, "content-type") or "",
                body=request.get_content(strict=False) or b"",
            ),
            world_keys(manifest, host, request.path) if manifest is not None else [],
        )
        if policy is HostPolicy.ANSWER and manifest is not None and world is not None:
            await self._answer(flow, host, manifest, world)
            return
        held = world or self.worlds.lobby
        declaration = held.capturing.find(host) if manifest is None else None
        if declaration is not None or (manifest is None and self.capture_unknown):
            await self._capture(flow, host, held, declaration)
            return
        refused = held
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
        and kept beside, to be read when the stream ends. A captured call passed through is always streamed so."""
        response = flow.response
        if flow.id in self._passing and response is not None:
            self._tee_passing(flow.id, response)
            return
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
        self._sent_on.pop(flow.id, None)
        if flow.id in self._passing:
            self._passed(flow, self._passing.pop(flow.id))
            return
        if flow.id not in self._recorded:
            return
        self._recorded.discard(flow.id)
        streamed = self._streams.pop(flow.id, None)
        request, response = flow.request, flow.response
        assert response is not None
        if streamed is None:
            body = response.get_content(strict=False) or b""
        else:
            raw = b"".join(streamed)
            coding = _first_header(response, "content-encoding")
            decoded = encoding.decode(raw, coding) if coding else raw
            body = decoded if isinstance(decoded, bytes) else decoded.encode("utf-8")
        exchanged = Exchanged(
            host=request.pretty_host,
            path=request.path,
            status=response.status_code,
            request_body=request.get_content(strict=False) or b"",
            request_type=_first_header(request, "content-type") or "",
            response_body=body,
            response_type=_first_header(response, "content-type") or "",
            traceparent=_first_header(request, TRACEPARENT),
            started=datetime.fromtimestamp(request.timestamp_start, UTC),
            ended=datetime.fromtimestamp(response.timestamp_end or response.timestamp_start, UTC),
        )
        span = span_of(exchanged)
        kept_in = self.worlds.keeping(exchanged.host, span.trace_id if span.parent_span_id is not None else None)
        kept_in.store.receive([span], source=SpanSource.WIRE)

    async def _answer(self, flow: http.HTTPFlow, host: str, manifest: Manifest, world: Mounted) -> None:
        async with world.lock:
            first = world.store.head() + 1
            original = flow.request.path
            exchange: Exchange | None = None
            try:
                app = world.app_for(manifest)
                first = world.store.head() + 1  # what seeding a provider on its first call wrote is not this call's
                flow.request.path = strip_prefix(original, manifest.path_prefix)
                await asgiapp.serve(_path_decoded(app), flow)
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
        asked, asked_bytes = redact.kept(
            request.get_content(strict=False) or b"", _first_header(request, "content-type") or ""
        )
        answered, answered_bytes = redact.kept(
            response.get_content(strict=False) or b"", _first_header(response, "content-type") or ""
        )
        exchange = Exchange(
            method=request.method,
            host=host,
            path=redact.path(path),
            status=response.status_code,
            request_body=asked,
            response_body=answered,
            request_bytes=asked_bytes,
            response_bytes=answered_bytes,
            traceparent=_first_header(request, TRACEPARENT),
        )
        self._seen(f"{request.method} {host}{exchange.path}")
        last = world.store.head()
        world.store.attach(exchange, first_seq=first, last_seq=last, provider=provider)
        if self.telemetry is not None and last >= first:
            for event in world.store.events(since=first - 1):
                self.telemetry.recorded(event)
        return exchange

    # -- hosts no provider claims, captured -------------------------------------------------------------------

    async def _capture(self, flow: http.HTTPFlow, host: str, world: Mounted, declaration: Declaration | None) -> None:
        """Answer a call to a host no provider claims as its world declares, or, undeclared, pass it through."""
        if declaration is None:
            self._passing[flow.id] = _Passing(world, None, CaptureMode.DISCOVERED, None)
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            return
        if isinstance(declaration, Acknowledge):
            await self._acknowledge(flow, host, world, declaration)
            return
        mode = CaptureMode.PASS_THROUGH if isinstance(declaration, PassThrough) else CaptureMode.REPLAY
        plan = world.capturing.replaying[declaration.host] if declaration.host in world.capturing.replaying else None
        if plan is None:
            self._passing[flow.id] = _Passing(world, declaration, mode, None)
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            return
        request = flow.request
        whole = capture.keep(
            request.get_content(strict=False) or b"",
            _first_header(request, "content-type"),
            limit=TEE_LIMIT,
            paths=declaration.redact,
        )
        asked = capture.Asked(
            method=request.method,
            host=host,
            path=redact.path(request.path, also=capture.query_keys(declaration)),
            body=whole.text,
            content_type=_first_header(request, "content-type"),
            raw_digest=whole.body.sha256,
        )
        found, why = plan.recordings.answer(asked, ignore_query=plan.ignore_query, ignore_body=plan.ignore_body)
        if found is not None:
            recorded = found.exchange
            assert recorded.captured is not None
            headers = {REPLAYED_HEADER: plan.recordings.source}
            if recorded.captured.response.content_type is not None:
                headers["content-type"] = recorded.captured.response.content_type
            answer = recorded.response_bytes or (recorded.response_body or "").encode("utf-8")
            flow.response = http.Response.make(recorded.status, answer, headers)
            await self._keep(
                flow, host, world, declaration, mode, AnsweredBy.RECORDING, replayed_from=plan.recordings.source
            )
            return
        if plan.on_miss is OnMiss.PASS_THROUGH:
            self._passing[flow.id] = _Passing(world, declaration, mode, f"not replayed: {why}")
            self._sent_on[flow.id] = f"{flow.request.method} {host}{redact.path(flow.request.path)}"
            return
        flow.response = _json_response(502, f"no recording answers this call: {why}", host)
        await self._keep(flow, host, world, declaration, mode, AnsweredBy.REFUSAL, note=f"not replayed: {why}")

    async def _acknowledge(self, flow: http.HTTPFlow, host: str, world: Mounted, declaration: Acknowledge) -> None:
        """Answer as declared, with an id made for this call in place of `{message_id}`; with a message reading,
        the send is also a message from the agent to a person."""
        request = flow.request
        reading = declaration.message
        async with world.lock:
            first = world.store.head() + 1
            answer = capture.canned(
                declaration, request.method, request.path, message_id=message_id(declaration, first)
            )
            flow.response = http.Response.make(answer.status, answer.body, answer.headers)
            if reading is None:
                await self._keep(
                    flow, host, world, declaration, CaptureMode.ACKNOWLEDGE, AnsweredBy.DECLARATION, locked=True
                )
                return
            content_type = _first_header(request, "content-type")
            whole = capture.keep(
                request.get_content(strict=False) or b"", content_type, limit=TEE_LIMIT, paths=declaration.redact
            )
            read = capture.read_message(reading, whole.text, content_type, world.capturing.people)
            if read.unread is None:
                self._message(world, declaration, read, first)
            await self._keep(
                flow,
                host,
                world,
                declaration,
                CaptureMode.ACKNOWLEDGE,
                AnsweredBy.DECLARATION,
                note=f"not read as a message: {read.unread}" if read.unread is not None else None,
                recipients=read.recipients,
                first=first,
                locked=True,
            )

    @staticmethod
    def _message(world: Mounted, declaration: Acknowledge, read: capture.Read, seq: int) -> None:
        """The send as a world event: a message from the agent to each person it reached, as their email, and to
        each address that reaches nobody, as written. Its people can answer it when the declaration says how an
        answer reaches the agent (`replies`)."""
        by_key = {p.key: p for p in world.capturing.people}
        emails = [by_key[r.person].email if r.person is not None else r.address for r in read.recipients]
        channel = "to:" + ",".join(sorted({r.address.lower() for r in read.recipients}))
        text = f"{read.subject}\n\n{read.text}" if read.subject else read.text
        body = json.dumps(
            {"to": [r.address for r in read.recipients], "subject": read.subject, "text": read.text},
            ensure_ascii=False,
        )
        world.store.apply(
            Change(
                entity=EntityRef(provider=declaration.key, kind=EntityKind.MESSAGE, external_id=str(seq)),
                operation=Operation.CREATE,
                actor=Actor.AGENT,
                body=body,
                parent=channel,
                after=MessageSnapshot(
                    text=text, channel=channel, recipient_emails=emails, answerable=declaration.replies is not None
                ),
            )
        )

    def _tee_passing(self, flow_id: str, response: http.Response) -> None:
        passing = self._passing[flow_id]
        kind = capture.media(_first_header(response, "content-type"))
        passing.streamed = kind == EVENT_STREAM or _first_header(response, "content-length") is None

        def tee(chunk: bytes) -> bytes:
            passing.size += len(chunk)
            if passing.size <= TEE_LIMIT:
                passing.chunks.append(chunk)
            return chunk

        response.stream = tee

    def _passed(self, flow: http.HTTPFlow, passing: _Passing) -> None:
        """A passed-through call whose answer has reached the agent: kept with what of the answer was held."""
        response = flow.response
        assert response is not None
        if passing.size > TEE_LIMIT:
            raw = b""
            note = f"its answer of {passing.size} bytes was longer than the proxy holds; kept as its length only"
        else:
            note = None
            coded = b"".join(passing.chunks)
            coding = _first_header(response, "content-encoding")
            try:
                decoded = encoding.decode(coded, coding) if coding else coded
            except ValueError:
                decoded = coded
            raw = decoded if isinstance(decoded, bytes) else (decoded or "").encode("utf-8")
        notes = "; ".join(n for n in (passing.note, note) if n) or None
        self._keep_now(
            flow,
            flow.request.pretty_host,
            passing.world,
            passing.declaration,
            passing.mode,
            AnsweredBy.REAL_HOST,
            note=notes,
            answer=raw,
            streamed=passing.streamed,
            whole_size=passing.size,
        )

    def error(self, flow: http.HTTPFlow) -> None:
        """A captured call whose real host could not be reached or broke off: kept, saying so."""
        self._sent_on.pop(flow.id, None)
        passing = self._passing.pop(flow.id, None)
        if passing is None:
            return
        reason = flow.error.msg if flow.error is not None else "the connection failed"
        if flow.response is None:
            flow.response = _json_response(
                502, f"the real host could not be reached: {reason}", flow.request.pretty_host
            )
        notes = "; ".join(n for n in (passing.note, f"the real host failed: {reason}") if n)
        self._keep_now(
            flow,
            flow.request.pretty_host,
            passing.world,
            passing.declaration,
            passing.mode,
            AnsweredBy.REAL_HOST,
            note=notes,
            answer=b"",
            streamed=passing.streamed,
            whole_size=passing.size,
        )

    async def _keep(
        self,
        flow: http.HTTPFlow,
        host: str,
        world: Mounted,
        declaration: Declaration | None,
        mode: CaptureMode,
        answered_by: AnsweredBy,
        *,
        replayed_from: str | None = None,
        note: str | None = None,
        recipients: list[Recipient] | None = None,
        first: int | None = None,
        locked: bool = False,
    ) -> Exchange:
        """Keep a captured call the proxy answered itself, tied to the events written for it since `first`."""
        response = flow.response
        assert response is not None
        if locked:
            return self._keep_now(
                flow,
                host,
                world,
                declaration,
                mode,
                answered_by,
                replayed_from=replayed_from,
                note=note,
                recipients=recipients,
                first=first,
                answer=response.get_content(strict=False) or b"",
                streamed=False,
                whole_size=None,
            )
        async with world.lock:
            return self._keep_now(
                flow,
                host,
                world,
                declaration,
                mode,
                answered_by,
                replayed_from=replayed_from,
                note=note,
                recipients=recipients,
                first=first,
                answer=response.get_content(strict=False) or b"",
                streamed=False,
                whole_size=None,
            )

    def _keep_now(
        self,
        flow: http.HTTPFlow,
        host: str,
        world: Mounted,
        declaration: Declaration | None,
        mode: CaptureMode,
        answered_by: AnsweredBy,
        *,
        answer: bytes,
        streamed: bool,
        whole_size: int | None,
        replayed_from: str | None = None,
        note: str | None = None,
        recipients: list[Recipient] | None = None,
        first: int | None = None,
    ) -> Exchange:
        request, response = flow.request, flow.response
        assert response is not None
        limit = declaration.body_limit if declaration is not None else BODY_LIMIT
        paths = declaration.redact if declaration is not None else []
        asked = capture.keep(
            request.get_content(strict=False) or b"", _first_header(request, "content-type"), limit=limit, paths=paths
        )
        answered = capture.keep(answer, _first_header(response, "content-type"), limit=limit, paths=paths)
        if whole_size is not None and whole_size > len(answer):
            answered = capture.KeptBody(
                None, answered.body.model_copy(update={"size": whole_size, "kept": BodyKept.BINARY}), None
            )
        keys = capture.query_keys(declaration) if declaration is not None else redact.CAPTURED_QUERY_KEYS
        started = datetime.fromtimestamp(request.timestamp_start, UTC)
        ended = datetime.fromtimestamp(response.timestamp_end or response.timestamp_start, UTC)
        exchange = Exchange(
            method=request.method,
            host=host,
            path=redact.path(request.path, also=keys),
            status=response.status_code,
            request_body=asked.text,
            response_body=answered.text,
            request_bytes=asked.raw,
            response_bytes=answered.raw,
            traceparent=_first_header(request, TRACEPARENT),
            captured=Captured(
                mode=mode,
                declared_as=declaration.host if declaration is not None else None,
                answered_by=answered_by,
                replayed_from=replayed_from,
                note=note,
                started=started,
                ended=max(started, ended),
                request=asked.body,
                response=answered.body,
                streamed=streamed,
                recipients=recipients or [],
            ),
        )
        self._seen(f"{request.method} {host}{exchange.path}")
        head = world.store.head()
        world.store.attach(exchange, first_seq=first if first is not None else head + 1, last_seq=head)
        self.worlds.answered(world, exchange, [])
        return exchange


def message_id(declaration: Acknowledge, seq: int) -> str:
    """The id an acknowledged send is answered with: its declaration's name and the seq its message takes."""
    return f"{declaration.key}-{seq}"


def _path_decoded(app: ASGIApp) -> ASGIApp:
    """The app, handed `path` decoded as ASGI says it arrives: mitmproxy percent-encodes the request target into
    it, so Docs' `/v1/documents/{id}:batchUpdate` would reach a router as `{id}%3AbatchUpdate`."""

    async def decoded(
        scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]
    ) -> None:
        raw = scope["raw_path"] if "raw_path" in scope else None
        if isinstance(raw, str):
            scope = {**scope, "path": unquote(raw.split("?", 1)[0])}
        await app(scope, receive, send)

    return decoded
