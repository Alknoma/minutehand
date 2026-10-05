"""`--record-model-calls`: a model API's calls opened, sent on unchanged, streamed back as streams, and each kept
as a span in the shape an instrumented agent exports; and nothing of the kind without the flag.

The model API is a local HTTPS server with its own CA (`upstream.py`); the run lists `localhost` as its model
host. Nothing here reaches the public internet.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from minutehand import session
from minutehand.adapters.proxy.model_calls import Exchanged, span_of
from minutehand.adapters.proxy.policy import HostPolicy, Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.telemetry import BytesValue, IntValue, SpanSource, SpanStatus, StoredSpan, StringValue
from tests.proxy.support import client, exchanges, stored_bytes
from tests.proxy.upstream import Answer, Authority, make_authority, model_api

MODEL_HOST = "localhost"
KEY = "sk-secret-key-123"
QUERY_KEY = "gm-secret-456"
CALLER_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_SPAN = "00f067aa0ba902b7"
SAID = "I will ask Sofia now."
ASKED = "Ask Sofia about pricing."
SYSTEM = "You chase answers."

REQUESTS: dict[str, tuple[str, object]] = {
    "chat": (
        "/v1/chat/completions",
        {
            "model": "model-luna",
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": ASKED}],
        },
    ),
    "responses": ("/v1/responses", {"model": "model-luna", "instructions": SYSTEM, "input": ASKED}),
    "messages": (
        "/v1/messages",
        {
            "model": "model-luna",
            "max_tokens": 64,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": [{"type": "text", "text": ASKED}]}],
        },
    ),
}

RESPONSES_ANSWER = {
    "object": "response",
    "model": "model-luna-0801",
    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": SAID}]}],
    "usage": {"input_tokens": 21, "output_tokens": 7},
}

WHOLE: dict[str, object] = {
    "chat": {
        "object": "chat.completion",
        "model": "model-luna-0801",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": SAID}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 7},
    },
    "responses": RESPONSES_ANSWER,
    "messages": {
        "type": "message",
        "role": "assistant",
        "model": "model-luna-0801",
        "content": [{"type": "text", "text": SAID}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 21, "output_tokens": 7},
    },
}


def _event(data: object, name: str | None = None) -> bytes:
    return (f"event: {name}\n" if name else "").encode() + f"data: {json.dumps(data)}\n\n".encode()


STREAMED: dict[str, list[bytes]] = {
    "chat": [
        _event(
            {
                "model": "model-luna-0801",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "I will "}}],
            }
        ),
        _event(
            {
                "model": "model-luna-0801",
                "choices": [{"index": 0, "delta": {"content": "ask Sofia now."}, "finish_reason": "stop"}],
            }
        ),
        _event({"model": "model-luna-0801", "choices": [], "usage": {"prompt_tokens": 21, "completion_tokens": 7}})
        + b"data: [DONE]\n\n",
    ],
    "responses": [
        _event({"type": "response.output_text.delta", "delta": "I will "}, "response.output_text.delta"),
        _event({"type": "response.output_text.delta", "delta": "ask Sofia now."}, "response.output_text.delta"),
        _event({"type": "response.completed", "response": RESPONSES_ANSWER}, "response.completed"),
    ],
    "messages": [
        _event(
            {"type": "message_start", "message": {"model": "model-luna-0801", "usage": {"input_tokens": 21}}},
            "message_start",
        ),
        _event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "I will "}}),
        _event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ask Sofia now."}}),
        _event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}})
        + _event({"type": "message_stop"}, "message_stop"),
    ],
}


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return make_authority(tmp_path / "upstream-ca")


def _proxy(registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority) -> Proxy:
    routing = Routing(registry, model_hosts=[MODEL_HOST])
    return Proxy(routing, store, clock, confdir=tmp_path / "ca", upstream_ca=authority.ca_cert, record_model_calls=True)


def _sent(shape: str, port: int) -> tuple[str, bytes, dict[str, str]]:
    path, body = REQUESTS[shape]
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {KEY}",
        "x-api-key": KEY,
        "traceparent": f"00-{CALLER_TRACE}-{CALLER_SPAN}-01",
    }
    return f"https://{MODEL_HOST}:{port}{path}?key={QUERY_KEY}", json.dumps(body).encode(), headers


def _check_span(stored: StoredSpan, *, wake: int) -> None:
    span = stored.span
    assert (stored.source, stored.wake) == (SpanSource.WIRE, wake)
    assert (span.trace_id, span.parent_span_id) == (CALLER_TRACE, CALLER_SPAN)
    assert span.name == "chat model-luna" and span.status is SpanStatus.UNSET and span.start <= span.end
    assert span.attribute("gen_ai.operation.name") == StringValue(value="chat")
    assert span.attribute("gen_ai.request.model") == StringValue(value="model-luna")
    assert span.attribute("gen_ai.response.model") == StringValue(value="model-luna-0801")
    assert span.attribute("gen_ai.usage.input_tokens") == IntValue(value=21)
    assert span.attribute("gen_ai.usage.output_tokens") == IntValue(value=7)
    system = span.attribute("gen_ai.system")
    assert isinstance(system, StringValue) and system.value in ("openai", "anthropic")
    inputs, outputs = span.attribute("gen_ai.input.messages"), span.attribute("gen_ai.output.messages")
    assert isinstance(inputs, StringValue) and isinstance(outputs, StringValue)
    assert {"type": "text", "content": ASKED} in [p for m in json.loads(inputs.value) for p in m["parts"]]
    [answered] = json.loads(outputs.value)
    assert answered["role"] == "assistant" and answered["parts"] == [{"type": "text", "content": SAID}]
    assert answered.get("finish_reason") in ("stop", "end_turn", None)


@pytest.mark.parametrize("shape", sorted(REQUESTS))
async def test_a_json_answer_is_passed_through_unchanged_and_kept_as_a_genai_span(
    shape: str,
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
    world_path: Path,
) -> None:
    clock.begin_wake()
    answer = Answer("application/json", [json.dumps(WHOLE[shape]).encode()])
    async with model_api(authority, answer) as upstream, _proxy(registry, store, clock, tmp_path, authority) as proxy:
        assert proxy.addon.policy(MODEL_HOST) is HostPolicy.RECORD
        url, body, headers = _sent(shape, upstream.port)
        async with client(proxy, proxy.ca_cert) as http:
            response = await http.post(url, content=body, headers=headers)

    assert response.status_code == 200 and response.json() == WHOLE[shape]
    # Sent on as the agent sent it: the same body, the key still on it for the real API.
    assert upstream.received[0].body == body and upstream.headers[0]["authorization"] == f"Bearer {KEY}"
    [kept] = store.spans()
    _check_span(kept, wake=1)
    assert exchanges(world_path) == [] and store.head() == 0
    stored = stored_bytes(world_path)
    assert ASKED.encode() in stored and KEY.encode() not in stored and QUERY_KEY.encode() not in stored


@pytest.mark.parametrize("shape", sorted(STREAMED))
async def test_a_streamed_answer_reaches_the_agent_as_a_stream_and_is_kept_whole(
    shape: str,
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
    world_path: Path,
) -> None:
    hold = asyncio.Event()
    answer = Answer("text/event-stream; charset=utf-8", STREAMED[shape], hold=hold)
    async with model_api(authority, answer) as upstream, _proxy(registry, store, clock, tmp_path, authority) as proxy:
        url, body, headers = _sent(shape, upstream.port)
        async with client(proxy, proxy.ca_cert) as http:
            async with http.stream("POST", url, content=body, headers=headers) as response:
                chunks = response.aiter_raw()
                # The model API holds the rest back until this arrives: a buffered proxy would never deliver it.
                first = await asyncio.wait_for(anext(chunks), timeout=10)
                assert store.spans() == []
                hold.set()
                rest = b"".join([chunk async for chunk in chunks])

    assert first + rest == b"".join(STREAMED[shape])
    [kept] = store.spans()
    _check_span(kept, wake=0)
    assert KEY.encode() not in stored_bytes(world_path)


async def test_without_the_flag_a_model_host_is_tunnelled_and_nothing_is_kept(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority, world_path: Path
) -> None:
    routing = Routing(registry, model_hosts=[MODEL_HOST])
    answer = Answer("application/json", [json.dumps(WHOLE["chat"]).encode()])
    async with model_api(authority, answer) as upstream, Proxy(routing, store, clock, confdir=tmp_path / "ca") as proxy:
        assert proxy.addon.policy(MODEL_HOST) is HostPolicy.TUNNEL
        url, body, headers = _sent("chat", upstream.port)
        # The client trusts ONLY the model API's own CA: a proxy that opened the call would fail it.
        async with client(proxy, authority.ca_cert) as http:
            response = await http.post(url, content=body, headers=headers)
    assert response.status_code == 200
    assert store.spans() == [] and ASKED.encode() not in stored_bytes(world_path)


async def test_a_call_with_no_traceparent_is_the_root_of_a_trace_of_its_own(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    answer = Answer("application/json", [json.dumps(WHOLE["chat"]).encode()])
    async with model_api(authority, answer) as upstream, _proxy(registry, store, clock, tmp_path, authority) as proxy:
        url, body, headers = _sent("chat", upstream.port)
        del headers["traceparent"]
        async with client(proxy, proxy.ca_cert) as http:
            assert (await http.post(url, content=body, headers=headers)).status_code == 200
    [kept] = store.spans()
    assert kept.span.parent_span_id is None and kept.span.trace_id != CALLER_TRACE


def test_an_answer_in_no_known_shape_is_kept_with_what_can_be_read() -> None:
    moment = datetime(2026, 10, 4, tzinfo=UTC)
    span = span_of(
        Exchanged(
            host="api.openai.com",
            path="/v1/embeddings",
            status=429,
            request_body=b'{"model": "embed-1", "input_vectors": [1]}',
            request_type="application/json",
            response_body=b"rate limited",
            response_type="text/plain",
            traceparent="not a traceparent",
            started=moment,
            ended=moment,
        )
    )
    assert span.name == "model call embed-1" and span.parent_span_id is None
    assert span.attribute("gen_ai.operation.name") is None and span.attribute("gen_ai.system") is None
    assert (span.status, span.status_message) == (SpanStatus.ERROR, "the model API answered 429")
    assert span.attribute("gen_ai.output.messages") is None
    assert span.attribute("http.response.status_code") == IntValue(value=429)


@pytest.mark.parametrize("recording", [True, False])
async def test_a_run_records_model_calls_only_when_its_listen_says_so(
    recording: bool, registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    listen = session.Listen(record_model_calls=recording, receive_telemetry=False)
    async with session.intercepting(Routing(registry), store, clock, tmp_path, listen) as running:
        assert running.proxy.addon.policy("api.anthropic.com") is (
            HostPolicy.RECORD if recording else HostPolicy.TUNNEL
        )
        assert running.receiver is None and running.telemetry_port is None


NOT_TEXT_ASKED = b'{"model": "model-luna", "messages": [{"role": "user", "content": "caf\xe9 \xff"}]}'
NOT_TEXT: dict[str, list[bytes]] = {
    "whole": [b'{"model": "model-luna-0801", "choices": [{"index": 0, "message": {"content": "r\xe9ponse"}}]}'],
    "streamed": [
        b'data: {"model": "model-luna-0801", "choices": [{"index": 0, "delta": {"content": "r\xe9"}}]}\n\n',
        b'data: {"choices": [{"index": 0, "delta": {"content": "ponse"}, "finish_reason": "stop"}]}\n\n',
    ],
}
REPLACEMENT = "\ufffd"


def _bodies(stored: StoredSpan) -> tuple[object, object]:
    return stored.span.attribute("minutehand.request.body"), stored.span.attribute("minutehand.response.body")


@pytest.mark.parametrize("answered", sorted(NOT_TEXT))
async def test_a_model_call_whose_bodies_are_not_text_is_kept_with_exactly_their_bytes(
    answered: str,
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
    world_path: Path,
) -> None:
    """A request and an answer that are not valid UTF-8, answered whole and as a stream: the span keeps each as
    the bytes that crossed, and nothing in the record is text with replacement characters."""
    content_type = "application/json" if answered == "whole" else "text/event-stream"
    answer = Answer(content_type, NOT_TEXT[answered])
    async with model_api(authority, answer) as upstream, _proxy(registry, store, clock, tmp_path, authority) as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            response = await http.post(
                f"https://{MODEL_HOST}:{upstream.port}/v1/chat/completions",
                content=NOT_TEXT_ASKED,
                headers={"content-type": "application/json"},
            )
    assert response.content == b"".join(NOT_TEXT[answered])
    assert upstream.received[0].body == NOT_TEXT_ASKED
    [kept] = store.spans()
    assert _bodies(kept) == (
        BytesValue(value=NOT_TEXT_ASKED.hex()),
        BytesValue(value=b"".join(NOT_TEXT[answered]).hex()),
    )
    assert REPLACEMENT not in kept.span.model_dump_json()
    assert REPLACEMENT.encode() not in stored_bytes(world_path)


@pytest.mark.parametrize("answered", ["whole", "streamed"])
async def test_a_model_call_whose_bodies_are_text_is_kept_as_that_text_byte_for_byte(
    answered: str,
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
) -> None:
    chunks = [json.dumps(WHOLE["chat"]).encode()] if answered == "whole" else STREAMED["chat"]
    content_type = "application/json" if answered == "whole" else "text/event-stream"
    async with (
        model_api(authority, Answer(content_type, chunks)) as upstream,
        _proxy(registry, store, clock, tmp_path, authority) as proxy,
    ):
        url, body, headers = _sent("chat", upstream.port)
        async with client(proxy, proxy.ca_cert) as http:
            assert (await http.post(url, content=body, headers=headers)).status_code == 200
    [kept] = store.spans()
    _check_span(kept, wake=0)
    request, response = _bodies(kept)
    assert isinstance(request, StringValue) and request.value.encode() == body
    assert isinstance(response, StringValue) and response.value.encode() == b"".join(chunks)
