"""Where the agent's telemetry was going before Minutehand stood in front of it, and passing it on.

An agent may already export to a collector or a vendor. When the environment Minutehand was started from names
an OTLP endpoint, every payload the receiver takes is sent on to it unchanged, with the headers that environment
names (`OTEL_EXPORTER_OTLP_HEADERS`, an API key as often as not). The variables are read as OpenTelemetry's
specification defines them:

    OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT   the signal's URL, used as it is
    OTEL_EXPORTER_OTLP_ENDPOINT            a base URL; the signal's path (`v1/traces`) is appended
    OTEL_EXPORTER_OTLP_HEADERS             `key=value` pairs, comma-separated, values URL-encoded
    OTEL_EXPORTER_OTLP_<SIGNAL>_HEADERS    the same, for one signal, taking precedence key by key

Passing on is best effort: a failure is answered by the caller recording it, never by failing the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpx

from minutehand.domain.telemetry import Signal

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
HEADERS = "OTEL_EXPORTER_OTLP_HEADERS"
TIMEOUT = 10.0
"""Seconds one forward may take; OpenTelemetry's own default export timeout."""


def signal_variable(signal: Signal, suffix: str) -> str:
    """`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and its kind."""
    return f"OTEL_EXPORTER_OTLP_{signal.value.upper()}_{suffix}"


def _headers(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in text.split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            pairs[unquote(key.strip()).lower()] = unquote(value.strip())
    return pairs


def _given(environ: Mapping[str, str], name: str) -> str | None:
    return (environ[name].strip() or None) if name in environ else None


@dataclass(frozen=True)
class Destination:
    """One signal's original endpoint and the headers it is sent with."""

    url: str
    headers: tuple[tuple[str, str], ...]

    def points_at(self, hosts: frozenset[str], port: int) -> bool:
        """True when this URL is the receiver itself, which would send every payload round in a circle."""
        parts = urlsplit(self.url)
        return parts.port == port and (parts.hostname or "").lower() in hosts


@dataclass(frozen=True)
class Forwarding:
    """Each signal's original destination; a signal with none is not passed on."""

    destinations: tuple[tuple[Signal, Destination], ...]

    def to(self, signal: Signal) -> Destination | None:
        return next((d for s, d in self.destinations if s is signal), None)

    def without(self, hosts: frozenset[str], port: int) -> Forwarding:
        return Forwarding(tuple((s, d) for s, d in self.destinations if not d.points_at(hosts, port)))

    @staticmethod
    def from_environment(environ: Mapping[str, str]) -> Forwarding:
        """The destinations an environment names; none when it names no OTLP endpoint."""
        base = _given(environ, ENDPOINT)
        shared = _headers(environ[HEADERS]) if HEADERS in environ else {}
        found: list[tuple[Signal, Destination]] = []
        for signal in Signal:
            url = _given(environ, signal_variable(signal, "ENDPOINT"))
            if url is None and base is not None:
                url = f"{base.rstrip('/')}/v1/{signal.value}"
            if url is None:
                continue
            own = signal_variable(signal, "HEADERS")
            headers = {**shared, **(_headers(environ[own]) if own in environ else {})}
            found.append((signal, Destination(url=url, headers=tuple(headers.items()))))
        return Forwarding(tuple(found))


async def forward(
    client: httpx.AsyncClient, destination: Destination, body: bytes, content: Mapping[str, str]
) -> str | None:
    """Send `body` on unchanged with its content headers; the reason it did not arrive, or None when it did."""
    try:
        response = await client.post(
            destination.url, content=body, headers={**dict(destination.headers), **content}, timeout=TIMEOUT
        )
    except httpx.HTTPError as e:
        return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    if response.status_code >= 300:
        return f"answered {response.status_code}"
    return None
