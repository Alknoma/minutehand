"""An agent's planner, as far as telemetry goes: one model call that wrote TEXT, exported over OTLP/HTTP by the stock
OpenTelemetry SDK to wherever OTEL_EXPORTER_OTLP_ENDPOINT points, with no trace link to any call.

    python model_span.py TEXT
"""

import json
import sys
import time

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "standin-planner"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
with provider.get_tracer("planner").start_as_current_span("chat model-test") as span:
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", "model-test")
    output = [{"role": "assistant", "parts": [{"type": "text", "content": sys.argv[1]}]}]
    span.set_attribute("gen_ai.output.messages", json.dumps(output))
    span.set_attribute("gen_ai.usage.input_tokens", 120)
    span.set_attribute("gen_ai.usage.output_tokens", 30)
    time.sleep(0.01)
provider.shutdown()
