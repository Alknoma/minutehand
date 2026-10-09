"""A model call's cached input tokens, recorded as the vendor counts them: an Anthropic answer's `input_tokens` leaves
out the tokens read from and written to its prompt cache, so they are summed into `gen_ai.usage.input_tokens` and kept
beside it; an OpenAI answer's `prompt_tokens` already holds the cached ones it details. The trial that found this
recorded 898 input tokens for a run that sent about 1.84 million."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from minutehand.adapters.proxy.model_calls import Exchanged, span_of
from minutehand.application.model_calls import model_call
from minutehand.domain.prices import Price, Prices
from minutehand.domain.telemetry import IntValue, Placement, SpanSource, StoredSpan

MOMENT = datetime(2026, 10, 4, tzinfo=UTC)


def _span(path: str, request: object, answer: str, content_type: str = "application/json") -> StoredSpan:
    span = span_of(
        Exchanged(
            host="api.example-model.test",
            path=path,
            status=200,
            request_body=json.dumps(request).encode(),
            request_type="application/json",
            response_body=answer.encode(),
            response_type=content_type,
            traceparent=None,
            started=MOMENT,
            ended=MOMENT,
        )
    )
    return StoredSpan(
        span=span,
        run_id="r",
        source=SpanSource.WIRE,
        wake=1,
        placed_by=Placement.WINDOW,
        arrived_in_wake=1,
        sim_time=MOMENT,
        after_seq=0,
    )


ANTHROPIC_REQUEST = {"model": "claude-test", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


def test_an_anthropic_answer_counts_its_cached_input_tokens() -> None:
    # https://docs.anthropic.com/en/api/messages: usage.input_tokens, cache_creation_input_tokens, cache_read_input_tokens
    answer = json.dumps(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 12,
                "cache_creation_input_tokens": 2_000,
                "cache_read_input_tokens": 30_000,
                "output_tokens": 40,
            },
        }
    )
    stored = _span("/v1/messages", ANTHROPIC_REQUEST, answer)
    assert stored.span.attribute("gen_ai.usage.input_tokens") == IntValue(value=32_012)
    assert stored.span.attribute("gen_ai.usage.cache_read.input_tokens") == IntValue(value=30_000)
    assert stored.span.attribute("gen_ai.usage.cache_creation.input_tokens") == IntValue(value=2_000)
    call = model_call(stored)
    assert (call.input_tokens, call.cache_read_tokens, call.cache_creation_tokens) == (32_012, 30_000, 2_000)
    assert call.uncached_input_tokens == 12


def test_an_anthropic_stream_counts_the_cached_tokens_its_message_start_names() -> None:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [],
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 1,
                },
            },
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
    ]
    stream = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    call = model_call(_span("/v1/messages", {**ANTHROPIC_REQUEST, "stream": True}, stream, "text/event-stream"))
    assert (call.input_tokens, call.cache_read_tokens, call.output_tokens) == (905, 900, 7)


def test_an_openai_answer_keeps_its_prompt_tokens_and_details_the_cached_ones() -> None:
    # https://platform.openai.com/docs/guides/prompt-caching: prompt_tokens includes prompt_tokens_details.cached_tokens
    answer = json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "gpt-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 2_006,
                "completion_tokens": 300,
                "total_tokens": 2_306,
                "prompt_tokens_details": {"cached_tokens": 1_920},
            },
        }
    )
    request = {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]}
    call = model_call(_span("/v1/chat/completions", request, answer))
    assert (call.input_tokens, call.cache_read_tokens, call.cache_creation_tokens) == (2_006, 1_920, None)
    assert call.uncached_input_tokens == 86


def test_a_declared_price_costs_each_kind_of_input_token_at_its_own_rate() -> None:
    prices = Prices(
        prices=[
            Price(
                model="claude-test",
                input_per_million=3.0,
                output_per_million=15.0,
                cache_read_per_million=0.3,
                cache_creation_per_million=3.75,
            )
        ]
    )
    cost = prices.cost("claude-test", 1_000_000, 0, cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000)
    assert cost is not None and round(cost[0], 6) == 7.05


def test_cached_tokens_without_a_declared_cache_price_have_no_cost() -> None:
    prices = Prices(prices=[Price(model="claude-test", input_per_million=3.0, output_per_million=15.0)])
    assert prices.cost("claude-test", 10, 10, cache_read_tokens=1_000) is None
    assert prices.cost("claude-test", 10, 10) is not None
