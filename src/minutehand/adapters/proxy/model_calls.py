"""A call to a model API, seen on the wire, as the span an instrumented agent would have exported for it.

For an agent with no tracing of its own, `--record-model-calls` opens its calls to model APIs, sends each on
unchanged and keeps the exchange as a `ReceivedSpan` in the same stored shape as a span the agent exports,
with the attributes OpenTelemetry's GenAI conventions name:

    gen_ai.system, gen_ai.provider.name    the vendor whose wire shape the call used
    gen_ai.operation.name                  chat
    gen_ai.request.model, gen_ai.response.model
    gen_ai.system_instructions             the system prompt, as parts
    gen_ai.input.messages                  the messages sent, each a role and its parts
    gen_ai.output.messages                 the messages answered, each a role, its parts and finish reason
    gen_ai.usage.input_tokens, gen_ai.usage.output_tokens

The three request shapes `edit.py` knows are read (OpenAI chat completions, OpenAI responses, Anthropic
messages), each answered as one JSON body or as a stream of server-sent events. A call in any other shape
is kept with what can be read of it: the host, the path, the status and the model it named.

Nothing from the request's headers or query string is kept, so an API key in `Authorization`, `x-api-key` or
`?key=` never reaches the store. The model API's own JSON is parsed only here.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from string import hexdigits
from urllib.parse import urlsplit

from minutehand.domain.telemetry import (
    Attribute,
    AttributeValue,
    BoolValue,
    IntValue,
    ReceivedSpan,
    SpanStatus,
    StringValue,
)

OPERATION = "chat"
UNREAD = "model call"
"""The name of a call in no shape this module reads; it claims no GenAI operation."""
EVENT_STREAM = "text/event-stream"


class Shape(StrEnum):
    CHAT = "chat"  # OpenAI chat completions
    RESPONSES = "responses"  # OpenAI responses
    MESSAGES = "messages"  # Anthropic messages
    UNKNOWN = "unknown"


_VENDOR = {Shape.CHAT: "openai", Shape.RESPONSES: "openai", Shape.MESSAGES: "anthropic"}


def _member(holder: object, key: str) -> object:
    """One field of the model API's own JSON; None when it is absent or the holder is not an object."""
    return holder[key] if isinstance(holder, dict) and key in holder else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _shape(path: str, request: object) -> Shape:
    """Which wire shape a call used: by the model API's own path first, then by the fields its body carries."""
    route = urlsplit(path).path.rstrip("/")
    if route.endswith("/chat/completions"):
        return Shape.CHAT
    if route.endswith("/responses"):
        return Shape.RESPONSES
    if route.endswith("/messages"):
        return Shape.MESSAGES
    if _member(request, "input") is not None or _member(request, "instructions") is not None:
        return Shape.RESPONSES
    if _member(request, "messages") is not None:
        return Shape.MESSAGES if _member(request, "system") is not None else Shape.CHAT
    return Shape.UNKNOWN


def _parts(content: object) -> list[dict[str, object]]:
    """A message's content as GenAI parts: text as `text`, a tool call as `tool_call`, anything else by its type."""
    if isinstance(content, str):
        return [{"type": "text", "content": content}]
    if not isinstance(content, list):
        return []
    parts: list[dict[str, object]] = []
    for block in content:
        kind = _text(_member(block, "type"))
        text = _text(_member(block, "text"))
        if text is not None:
            parts.append({"type": "text", "content": text})
        elif kind in ("tool_use", "function_call"):
            arguments = _member(block, "input") if kind == "tool_use" else _member(block, "arguments")
            parts.append({"type": "tool_call", "name": _member(block, "name"), "arguments": arguments})
        elif kind is not None:
            parts.append({"type": kind})
    return parts


def _message(role: object, content: object) -> dict[str, object]:
    return {"role": role if isinstance(role, str) else "user", "parts": _parts(content)}


@dataclass
class _Call:
    """What one call says, gathered from its request and its response."""

    shape: Shape
    request_model: str | None = None
    response_model: str | None = None
    system: list[dict[str, object]] = field(default_factory=list)
    inputs: list[dict[str, object]] = field(default_factory=list)
    outputs: list[dict[str, object]] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


def _read_request(call: _Call, request: object) -> None:
    call.request_model = _text(_member(request, "model"))
    if call.shape is Shape.CHAT:
        messages = _member(request, "messages")
        for message in messages if isinstance(messages, list) else []:
            entry = _message(_member(message, "role"), _member(message, "content"))
            calls = _member(message, "tool_calls")
            for tool in calls if isinstance(calls, list) else []:
                function = _member(tool, "function")
                entry_parts = entry["parts"]
                assert isinstance(entry_parts, list)
                entry_parts.append(
                    {
                        "type": "tool_call",
                        "name": _member(function, "name"),
                        "arguments": _member(function, "arguments"),
                    }
                )
            call.inputs.append(entry)
    elif call.shape is Shape.RESPONSES:
        call.system = _parts(_member(request, "instructions"))
        given = _member(request, "input")
        if isinstance(given, str):
            call.inputs.append(_message("user", given))
        for item in given if isinstance(given, list) else []:
            call.inputs.append(_message(_member(item, "role"), _member(item, "content")))
    elif call.shape is Shape.MESSAGES:
        call.system = _parts(_member(request, "system"))
        messages = _member(request, "messages")
        for message in messages if isinstance(messages, list) else []:
            call.inputs.append(_message(_member(message, "role"), _member(message, "content")))


def _usage(call: _Call, usage: object) -> None:
    for key in ("input_tokens", "prompt_tokens"):
        if _int(_member(usage, key)) is not None:
            call.input_tokens = _int(_member(usage, key))
    for key in ("output_tokens", "completion_tokens"):
        if _int(_member(usage, key)) is not None:
            call.output_tokens = _int(_member(usage, key))


def _read_answer(call: _Call, answer: object) -> None:
    """One whole JSON answer, in the call's shape."""
    call.response_model = _text(_member(answer, "model")) or call.response_model
    _usage(call, _member(answer, "usage"))
    if call.shape is Shape.CHAT:
        choices = _member(answer, "choices")
        for choice in choices if isinstance(choices, list) else []:
            message = _member(choice, "message")
            entry = _message(_member(message, "role") or "assistant", _member(message, "content"))
            tools = _member(message, "tool_calls")
            parts = entry["parts"]
            assert isinstance(parts, list)
            for tool in tools if isinstance(tools, list) else []:
                function = _member(tool, "function")
                parts.append(
                    {
                        "type": "tool_call",
                        "name": _member(function, "name"),
                        "arguments": _member(function, "arguments"),
                    }
                )
            entry["finish_reason"] = _member(choice, "finish_reason")
            call.outputs.append(entry)
    elif call.shape is Shape.RESPONSES:
        output = _member(answer, "output")
        for item in output if isinstance(output, list) else []:
            if _member(item, "type") == "message":
                call.outputs.append(_message(_member(item, "role") or "assistant", _member(item, "content")))
            elif _member(item, "type") == "function_call":
                call.outputs.append({"role": "assistant", "parts": _parts([item])})
    elif call.shape is Shape.MESSAGES:
        entry = _message(_member(answer, "role") or "assistant", _member(answer, "content"))
        entry["finish_reason"] = _member(answer, "stop_reason")
        call.outputs.append(entry)


def _events(stream: str) -> list[object]:
    """The JSON payload of each server-sent event; `[DONE]` and anything that is not JSON is passed over."""
    found: list[object] = []
    for block in stream.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.split("\n") if line.startswith("data:"))
        if not data or data == "[DONE]":
            continue
        try:
            found.append(json.loads(data))
        except json.JSONDecodeError:
            continue
    return found


def _read_stream(call: _Call, stream: str) -> None:
    """A streamed answer, put back together into what the whole answer would have said."""
    text: dict[int, list[str]] = {}
    finish: dict[int, object] = {}
    for event in _events(stream):
        kind = _member(event, "type")
        if call.shape is Shape.CHAT:
            call.response_model = _text(_member(event, "model")) or call.response_model
            _usage(call, _member(event, "usage"))
            choices = _member(event, "choices")
            for choice in choices if isinstance(choices, list) else []:
                index = _int(_member(choice, "index")) or 0
                delta = _text(_member(_member(choice, "delta"), "content"))
                text.setdefault(index, []).append(delta or "")
                if _member(choice, "finish_reason") is not None:
                    finish[index] = _member(choice, "finish_reason")
        elif call.shape is Shape.RESPONSES:
            if kind == "response.completed":
                _read_answer(call, _member(event, "response"))
                return
            if kind == "response.output_text.delta":
                text.setdefault(0, []).append(_text(_member(event, "delta")) or "")
        elif call.shape is Shape.MESSAGES:
            if kind == "message_start":
                message = _member(event, "message")
                call.response_model = _text(_member(message, "model")) or call.response_model
                _usage(call, _member(message, "usage"))
            elif kind == "content_block_delta":
                index = _int(_member(event, "index")) or 0
                text.setdefault(index, []).append(_text(_member(_member(event, "delta"), "text")) or "")
            elif kind == "message_delta":
                finish[0] = _member(_member(event, "delta"), "stop_reason")
                _usage(call, _member(event, "usage"))
    if call.shape is Shape.MESSAGES:
        joined = [{"type": "text", "content": "".join(text[i])} for i in sorted(text)]
        call.outputs.append({"role": "assistant", "parts": joined, "finish_reason": finish[0] if 0 in finish else None})
        return
    for index in sorted(text):
        entry: dict[str, object] = {"role": "assistant", "parts": [{"type": "text", "content": "".join(text[index])}]}
        if index in finish:
            entry["finish_reason"] = finish[index]
        call.outputs.append(entry)


def _json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


@dataclass(frozen=True)
class Exchanged:
    """One model call as the proxy saw it. The request's headers and query string are not here, by design."""

    host: str
    path: str
    status: int
    request_body: str
    response_body: str
    response_type: str
    traceparent: str | None
    started: datetime
    ended: datetime


def _caller(traceparent: str | None) -> tuple[str, str] | None:
    """The trace id and span id of a W3C `traceparent`, when it is one."""
    if traceparent is None:
        return None
    parts = traceparent.strip().lower().split("-")
    if len(parts) < 4 or len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    trace_id, span_id = parts[1], parts[2]
    if not set(trace_id + span_id) <= set(hexdigits.lower()) or set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None
    return trace_id, span_id


def span_of(exchanged: Exchanged) -> ReceivedSpan:
    """The span for one model call: in the caller's trace and under its span when the call carried a
    traceparent, else the root of a trace of its own."""
    request = _json(exchanged.request_body)
    call = _Call(shape=_shape(exchanged.path, request))
    _read_request(call, request)
    if call.shape is not Shape.UNKNOWN:
        streamed = exchanged.response_type.split(";", 1)[0].strip().lower() == EVENT_STREAM
        if streamed:
            _read_stream(call, exchanged.response_body)
        else:
            _read_answer(call, _json(exchanged.response_body))
    known = call.shape is not Shape.UNKNOWN
    attributes: list[tuple[str, AttributeValue]] = [
        ("server.address", StringValue(value=exchanged.host)),
        ("url.path", StringValue(value=urlsplit(exchanged.path).path)),
        ("http.response.status_code", IntValue(value=exchanged.status)),
        ("minutehand.recorded_on_the_wire", BoolValue(value=True)),
    ]
    if known:
        vendor = StringValue(value=_VENDOR[call.shape])
        attributes += [
            ("gen_ai.operation.name", StringValue(value=OPERATION)),
            ("gen_ai.system", vendor),
            ("gen_ai.provider.name", vendor),
        ]
    for key, text in (("gen_ai.request.model", call.request_model), ("gen_ai.response.model", call.response_model)):
        if text is not None:
            attributes.append((key, StringValue(value=text)))
    for key, parts in (
        ("gen_ai.system_instructions", call.system),
        ("gen_ai.input.messages", call.inputs),
        ("gen_ai.output.messages", call.outputs),
    ):
        if parts:
            attributes.append((key, StringValue(value=json.dumps(parts, ensure_ascii=False))))
    for key, count in (
        ("gen_ai.usage.input_tokens", call.input_tokens),
        ("gen_ai.usage.output_tokens", call.output_tokens),
    ):
        if count is not None:
            attributes.append((key, IntValue(value=count)))
    caller = _caller(exchanged.traceparent)
    failed = exchanged.status >= 400
    return ReceivedSpan(
        trace_id=caller[0] if caller is not None else secrets.token_hex(16),
        span_id=secrets.token_hex(8),
        parent_span_id=caller[1] if caller is not None else None,
        name=" ".join(part for part in (OPERATION if known else UNREAD, call.request_model) if part),
        start=exchanged.started,
        end=exchanged.ended,
        status=SpanStatus.ERROR if failed else SpanStatus.UNSET,
        status_message=f"the model API answered {exchanged.status}" if failed else None,
        attributes=[Attribute(key=key, value=value) for key, value in attributes],
    )
