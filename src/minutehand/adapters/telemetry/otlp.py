"""OTLP/HTTP payloads as an agent's SDK sends them: protobuf or JSON, gzip or not, read into `ReceivedSpan`s.

The wire format is OpenTelemetry's, decoded with `opentelemetry-proto`'s own messages. OTLP/JSON is protobuf's
JSON mapping with one difference the specification makes: trace and span ids are hex, not base64, so they are
rewritten before protobuf reads the document. Unknown JSON fields are ignored, as the specification requires of
a receiver.

Logs and metrics are decoded only to tell a well-formed payload from a malformed one; neither is kept.
"""

from __future__ import annotations

import base64
import gzip
import json
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from google.protobuf import json_format
from google.protobuf.message import DecodeError, Message
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
from pydantic import ValidationError

from minutehand.domain.telemetry import (
    ArrayValue,
    Attribute,
    AttributeValue,
    BoolValue,
    BytesValue,
    DoubleValue,
    IntValue,
    MapValue,
    ReceivedSpan,
    Signal,
    SpanStatus,
    StringValue,
)

PROTOBUF = "application/x-protobuf"
JSON = "application/json"
SERVICE_NAME = "service.name"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ID_FIELDS = frozenset({"traceId", "spanId", "parentSpanId", "trace_id", "span_id", "parent_span_id"})
_STATUS = {
    Status.STATUS_CODE_UNSET: SpanStatus.UNSET,
    Status.STATUS_CODE_OK: SpanStatus.OK,
    Status.STATUS_CODE_ERROR: SpanStatus.ERROR,
}


class NotOtlp(ValueError):
    """The payload is not an OTLP export request this receiver can read."""


class UnsupportedMedia(NotOtlp):
    """The content type or content encoding is not one OTLP/HTTP uses."""


class Format(StrEnum):
    PROTOBUF = "protobuf"
    JSON = "json"


@dataclass(frozen=True)
class Payload:
    """One request's body, decompressed, and the format it is in."""

    format: Format
    body: bytes

    @staticmethod
    def read(body: bytes, content_type: str, content_encoding: str) -> Payload:
        media = content_type.split(";", 1)[0].strip().lower()
        if media == PROTOBUF:
            found = Format.PROTOBUF
        elif media == JSON:
            found = Format.JSON
        else:
            raise UnsupportedMedia(f"content type {content_type or '(none)'} is not {PROTOBUF} or {JSON}")
        encoding = content_encoding.strip().lower()
        try:
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding == "deflate":
                body = zlib.decompress(body)
            elif encoding not in ("", "identity"):
                raise UnsupportedMedia(f"content encoding {content_encoding} is not gzip, deflate or none")
        except (OSError, EOFError, zlib.error) as e:
            raise NotOtlp(f"the body is not valid {encoding}: {e}") from e
        return Payload(found, body)


_REQUESTS: dict[Signal, type[Message]] = {
    Signal.TRACES: ExportTraceServiceRequest,
    Signal.LOGS: ExportLogsServiceRequest,
    Signal.METRICS: ExportMetricsServiceRequest,
}
_RESPONSES: dict[Signal, type[Message]] = {
    Signal.TRACES: ExportTraceServiceResponse,
    Signal.LOGS: ExportLogsServiceResponse,
    Signal.METRICS: ExportMetricsServiceResponse,
}


def _hex_ids(value: object) -> object:
    """OTLP/JSON's hex ids rewritten as the base64 protobuf's JSON mapping reads."""
    if isinstance(value, dict):
        rewritten: dict[str, object] = {}
        for key, item in value.items():
            if key in _ID_FIELDS and isinstance(item, str):
                try:
                    rewritten[key] = base64.b64encode(bytes.fromhex(item)).decode()
                except ValueError as e:
                    raise NotOtlp(f"{key} {item!r} is not hex") from e
            else:
                rewritten[str(key)] = _hex_ids(item)
        return rewritten
    if isinstance(value, list):
        return [_hex_ids(item) for item in value]
    return value


def decode(signal: Signal, payload: Payload) -> Message:
    """The export request in the payload; `NotOtlp` when it is not one."""
    message = _REQUESTS[signal]()
    if payload.format is Format.PROTOBUF:
        try:
            message.ParseFromString(payload.body)
        except DecodeError as e:
            raise NotOtlp(f"the body is not an OTLP {signal.value} request: {e}") from e
        return message
    try:
        document: object = json.loads(payload.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise NotOtlp(f"the body is not JSON: {e}") from e
    if not isinstance(document, dict):
        raise NotOtlp("an OTLP/JSON body is an object")
    rewritten = _hex_ids(document)
    assert isinstance(rewritten, dict)
    try:
        json_format.ParseDict(rewritten, message, ignore_unknown_fields=True)
    except json_format.ParseError as e:
        raise NotOtlp(f"the body is not an OTLP {signal.value} request: {e}") from e
    return message


def answer(signal: Signal, payload_format: Format) -> tuple[bytes, str]:
    """The success response to an export request, in the format it came in."""
    response = _RESPONSES[signal]()
    if payload_format is Format.PROTOBUF:
        return response.SerializeToString(), PROTOBUF
    return json_format.MessageToJson(response).encode(), JSON


def _value(value: AnyValue) -> AttributeValue | None:
    match value.WhichOneof("value"):
        case "string_value":
            return StringValue(value=value.string_value)
        case "bool_value":
            return BoolValue(value=value.bool_value)
        case "int_value":
            return IntValue(value=value.int_value)
        case "double_value":
            return DoubleValue(value=value.double_value)
        case "bytes_value":
            return BytesValue(value=value.bytes_value.hex())
        case "array_value":
            return ArrayValue(values=[v for v in (_value(item) for item in value.array_value.values) if v is not None])
        case "kvlist_value":
            return MapValue(values=_attributes(value.kvlist_value.values))
        case _:
            return None


def _attributes(pairs: Iterable[KeyValue]) -> list[Attribute]:
    return [Attribute(key=pair.key, value=_value(pair.value)) for pair in pairs]


def _moment(unix_nano: int) -> datetime:
    return _EPOCH + timedelta(microseconds=unix_nano // 1000)


def _span(span: Span, service: str | None) -> ReceivedSpan:
    status = _STATUS[span.status.code] if span.status.code in _STATUS else SpanStatus.UNSET
    return ReceivedSpan(
        trace_id=span.trace_id.hex(),
        span_id=span.span_id.hex(),
        parent_span_id=span.parent_span_id.hex() or None,
        name=span.name,
        start=_moment(span.start_time_unix_nano),
        end=_moment(span.end_time_unix_nano),
        status=status,
        status_message=span.status.message or None,
        attributes=_attributes(span.attributes),
        service_name=service,
    )


def spans(request: Message) -> list[ReceivedSpan]:
    """Every span in a decoded trace export request; `NotOtlp` when one carries an id of the wrong length."""
    assert isinstance(request, ExportTraceServiceRequest)
    found: list[ReceivedSpan] = []
    for resource_spans in request.resource_spans:
        named = resource_spans.resource.attributes
        service = next((a.value.string_value for a in named if a.key == SERVICE_NAME), None) or None
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                try:
                    found.append(_span(span, service))
                except ValidationError as e:
                    raise NotOtlp(f"span {span.name!r} is not a valid span: {e}") from e
    return found
