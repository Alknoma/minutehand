"""Calls that went around the proxy, from what the run can see of them.

A client that ignores `HTTPS_PROXY` reaches the real service, and the proxy, which never sees the call, can say
nothing about it. Two things in a run say it happened:

- **The agent's own telemetry.** OpenTelemetry's HTTP client instrumentations export a span per call, naming the URL
  (`url.full`, or `http.url` in the older conventions) or the host (`server.address`, `net.peer.name`). A span for a
  host a provider claims is matched to the call the proxy recorded for it by the `traceparent` that call carried
  (the client span's own id). What no traceparent matched is counted against the host's recorded calls that carried
  no traceparent, or one naming a span the agent never exported; whatever is left over the proxy never saw. A
  recorded call whose traceparent names an exported span that is no HTTP client call was made by an uninstrumented
  client inside that span, so it stands for no client span. Deterministic, and only as complete as the agent's
  instrumentation.
- **Silence.** The agent was woken and the proxy saw not one call to any provider the scenario and the agent file
  name. That is what an agent whose client ignores the proxy looks like from here, and also what an agent that did
  nothing looks like, so it is a question for a person, never a failure.

Not done, and why: resolving the claimed hosts to an address of the proxy's for the agent alone (a hosts file through
`HOSTALIASES`, a resolver through `LD_PRELOAD`) reaches neither a static Go binary nor macOS without root; and watching
the agent's sockets misses a call shorter than the interval it is polled at. A container can be made to send every
such call to the proxy instead (`adapters.proxy.redirected`).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from urllib.parse import urlsplit

from minutehand.domain.checks import AroundProxy
from minutehand.domain.scenario import ProviderKey
from minutehand.domain.telemetry import ReceivedSpan, SpanSource, StoredSpan, StringValue
from minutehand.domain.world import RecordedCall

METHOD_KEYS = ("http.request.method", "http.method")
URL_KEYS = ("url.full", "http.url")
HOST_KEYS = ("server.address", "net.peer.name")


def _text(span: ReceivedSpan, keys: Sequence[str]) -> str | None:
    for key in keys:
        value = span.attribute(key)
        if isinstance(value, StringValue) and value.value:
            return value.value
    return None


def called_host(span: ReceivedSpan) -> tuple[str, str] | None:
    """The host an HTTP client span says it called, and the URL or host it names, as the span carries them; None for
    a span that is no HTTP call or names no host."""
    if _text(span, METHOD_KEYS) is None:
        return None
    url = _text(span, URL_KEYS)
    if url is not None:
        host = urlsplit(url).hostname
        return (host.lower(), url) if host else None
    host = _text(span, HOST_KEYS)
    return (host.lower(), host) if host is not None else None


def _parent_id(traceparent: str | None) -> str | None:
    """The span id a W3C `traceparent` names as the caller's (`00-<trace>-<span>-<flags>`)."""
    parts = traceparent.split("-") if traceparent is not None else []
    return parts[2].lower() if len(parts) == 4 else None


def around_proxy(
    spans: Sequence[StoredSpan], calls: Sequence[RecordedCall], claims: Callable[[str], ProviderKey | None]
) -> list[AroundProxy] | None:
    """Each host a provider claims (`claims`) that the agent's exported HTTP client spans called more often than the
    proxy recorded, with how many it did not see; None when no span the agent exported is an HTTP client call."""
    seen = [
        (stored.span, called)
        for stored in spans
        if stored.source is SpanSource.RECEIVED and (called := called_host(stored.span)) is not None
    ]
    if not seen:
        return None
    by_host: dict[str, list[tuple[ReceivedSpan, str]]] = {}
    for span, (host, named) in seen:
        if claims(host) is not None:
            by_host.setdefault(host, []).append((span, named))
    exported = {stored.span.span_id for stored in spans}
    found: list[AroundProxy] = []
    for host, made in by_host.items():
        recorded = [
            c
            for c in calls
            if c.exchange.host.lower() == host and c.exchange.tunnelled is None and c.exchange.inbox_call is None
        ]
        joined = {_parent_id(c.exchange.traceparent) for c in recorded}
        unjoined = [(span, named) for span, named in made if span.span_id not in joined]
        anonymous = sum(1 for c in recorded if _parent_id(c.exchange.traceparent) not in exported)
        missed = len(unjoined) - anonymous
        if missed <= 0:
            continue
        span, named = unjoined[0]
        provider = claims(host)
        assert provider is not None
        found.append(
            AroundProxy(
                host=host,
                provider=provider,
                by_agent=len(made),
                through_proxy=len(recorded),
                around=missed,
                example=f"{span.name} {named}",
            )
        )
    return found


def uncalled_providers(
    named: Collection[ProviderKey], calls: Sequence[RecordedCall], *, woken: bool
) -> list[ProviderKey]:
    """`named`, sorted, when the agent was `woken` and the proxy saw a call to none of them; else empty."""
    called = {c.provider for c in calls if c.provider is not None}
    if not woken or not named or called & set(named):
        return []
    return sorted(named)
