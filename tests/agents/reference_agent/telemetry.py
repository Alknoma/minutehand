"""The reference agent's own OpenTelemetry: the official SDK, a `BatchSpanProcessor` at its default delay (five
seconds, so a wake's spans usually arrive after the wake has ended), in one of three modes:

    REFERENCE_TELEMETRY=http   OTLP over HTTP, configured by nothing but the OTEL_EXPORTER_OTLP_* environment
    REFERENCE_TELEMETRY=grpc   OTLP over gRPC, the exporter built explicitly in code, as many SDK setups do: it
                               reads the endpoint from the environment and ignores OTEL_EXPORTER_OTLP_PROTOCOL
    REFERENCE_TELEMETRY=none   no telemetry at all (the default)

Each model call is a span with the GenAI semantic conventions' attributes; `inject` puts the current span's
`traceparent` on an outbound request, so a world event can be joined to the span that caused it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_provider: TracerProvider | None = None


def setup(service: str) -> None:
    """Install the tracer provider for this process, in the mode the environment names."""
    global _provider
    mode = os.environ.get("REFERENCE_TELEMETRY", "none")
    if mode == "none":
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if mode == "http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter as HttpExporter

        provider.add_span_processor(BatchSpanProcessor(HttpExporter()))
    elif mode == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GrpcExporter

        provider.add_span_processor(BatchSpanProcessor(GrpcExporter(insecure=True)))
    else:
        raise SystemExit(f"REFERENCE_TELEMETRY is http, grpc or none, not {mode!r}")
    trace.set_tracer_provider(provider)
    _provider = provider


def shutdown() -> None:
    """Flush what the batch holds: called as the process is stopped."""
    if _provider is not None:
        _provider.shutdown()


def tracer() -> trace.Tracer:
    return trace.get_tracer("reference_agent")


def inject(headers: dict[str, str]) -> dict[str, str]:
    """The headers, with the current span's trace context added."""
    propagate.inject(headers)
    return headers


@contextmanager
def model_call(model: str, messages: list[dict[str, str]]) -> Iterator[trace.Span]:
    """One chat completion, as the GenAI conventions name it."""
    with tracer().start_as_current_span(f"chat {model}", kind=trace.SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.system", "openai")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        system = [m["content"] for m in messages if m["role"] == "system"]
        if system:
            span.set_attribute("gen_ai.system_instructions", json.dumps([{"type": "text", "content": system[0]}]))
        span.set_attribute(
            "gen_ai.input.messages",
            json.dumps(
                [
                    {"role": m["role"], "parts": [{"type": "text", "content": m["content"]}]}
                    for m in messages
                    if m["role"] != "system"
                ]
            ),
        )
        yield span


def answered(span: trace.Span, model: str, text: str, usage: Mapping[str, object]) -> None:
    span.set_attribute("gen_ai.response.model", model)
    span.set_attribute(
        "gen_ai.output.messages",
        json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": text}], "finish_reason": "stop"}]),
    )
    for ours, theirs in (
        ("gen_ai.usage.input_tokens", "prompt_tokens"),
        ("gen_ai.usage.output_tokens", "completion_tokens"),
    ):
        value = usage.get(theirs)
        if isinstance(value, int):
            span.set_attribute(ours, value)
