"""The agent's model calls, as recorded on the wire, written out one JSON object per line in the OpenAI Batch API's
shape (`minutehand model-calls`): the request as a batch input line has it (`custom_id`, `method`, `url`, `body`)
and the answer as a batch output line has it (`response.status_code`, `response.body`), with where in the run it
was made under `minutehand`. Any OpenAI-compatible tool reads the requests; a streamed answer's `body` is its
server-sent events as they arrived, as text.

A body is the recording's: credential fields redacted, and kept as text, or as hex when it was no text at all."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence

from minutehand.domain.telemetry import AttributeValue, BytesValue, IntValue, SpanSource, StoredSpan, StringValue


def _body(value: AttributeValue | None) -> object:
    if isinstance(value, StringValue):
        try:
            return json.loads(value.value)
        except json.JSONDecodeError:
            return value.value
    if isinstance(value, BytesValue):
        return {"hex": value.value}
    return None


def _text(value: AttributeValue | None) -> str | None:
    return value.value if isinstance(value, StringValue) else None


def batch_lines(spans: Sequence[StoredSpan]) -> Iterator[str]:
    """One line per model call the agent made, oldest first."""
    for stored in sorted(spans, key=lambda s: (s.span.start, s.after_seq)):
        span = stored.span
        if stored.source is not SpanSource.WIRE:
            continue
        status = span.attribute("http.response.status_code")
        line = {
            "custom_id": span.span_id,
            "method": "POST",
            "url": _text(span.attribute("url.path")),
            "body": _body(span.attribute("minutehand.request.body")),
            "response": {
                "status_code": status.value if isinstance(status, IntValue) else None,
                "body": _body(span.attribute("minutehand.response.body")),
            },
            "minutehand": {
                "run": stored.run_id,
                "wake": stored.wake,
                "at": stored.sim_time.isoformat(),
                "host": _text(span.attribute("server.address")),
                "content_type": _text(span.attribute("minutehand.response.content_type")),
                "replayed_from": _text(span.attribute("minutehand.replayed_from")),
            },
        }
        yield json.dumps(line, ensure_ascii=False)
