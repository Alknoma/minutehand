"""A program that traces with the stock OpenTelemetry SDK and exports over OTLP/HTTP, configured by its
environment alone: no endpoint, protocol or header is written here.

    python exporting_agent.py protobuf    the SDK's own OTLPSpanExporter, which reads the OTEL_* variables
    python exporting_agent.py json        the same spans as OTLP/JSON, gzip-compressed, posted to
                                          OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (the Python SDK ships no JSON
                                          exporter; this encodes the SDK's own protobuf request with protobuf's
                                          JSON mapping and hex ids, as the OTLP specification defines)
    python exporting_agent.py metrics     the SDK's own metric exporter, which the receiver acknowledges
    python exporting_agent.py grpc        the SDK's own gRPC exporter, built in code as many setups do: it reads
                                          the endpoint from the environment and ignores OTEL_EXPORTER_OTLP_PROTOCOL

It prints the trace id it made, then exits.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import sys
import urllib.request
from collections.abc import Sequence

from google.protobuf import json_format
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

IDS = ("traceId", "spanId", "parentSpanId")


def _hex(value: object) -> object:
    if isinstance(value, dict):
        return {k: base64.b64decode(v).hex() if k in IDS and isinstance(v, str) else _hex(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_hex(v) for v in value]
    return value


class JsonExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        document = _hex(json_format.MessageToDict(encode_spans(spans)))
        request = urllib.request.Request(
            os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"],
            data=gzip.compress(json.dumps(document).encode()),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as answered:
            return SpanExportResult.SUCCESS if answered.status == 200 else SpanExportResult.FAILURE


def main() -> None:
    mode = sys.argv[1]
    resource = Resource.create({"service.name": "exporting-agent"})
    if mode == "metrics":
        meters = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())])
        meters.get_meter("agent").create_counter("asks").add(1)
        meters.shutdown()
        return
    provider = TracerProvider(resource=resource)
    if mode == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter as GrpcSpanExporter

        # How long it retries a server that does not answer gRPC; a test of that refusal shortens it.
        give_up = float(os.environ.get("EXPORT_GIVE_UP_SECONDS", "5"))
        exporter: SpanExporter = GrpcSpanExporter(insecure=True, timeout=give_up)
    else:
        exporter = OTLPSpanExporter() if mode == "protobuf" else JsonExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    tracer = provider.get_tracer("agent")
    with tracer.start_as_current_span("plan the wake") as root:
        with tracer.start_as_current_span(
            "chat model-luna",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "model-luna",
                "gen_ai.usage.input_tokens": 41,
                "gen_ai.request.temperature": 0.2,
                "gen_ai.request.stop_sequences": ["END", "STOP"],
                "flagged": True,
            },
        ) as chat:
            chat.set_status(Status(StatusCode.ERROR, "the model refused"))
        print(format(root.get_span_context().trace_id, "032x"), flush=True)
    provider.shutdown()


if __name__ == "__main__":
    main()
