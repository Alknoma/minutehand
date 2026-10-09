"""The agent's own instructions, read from its calls to its model as the proxy records them: the system prompt it
gives its model is its statement of its work, which the assessment reads (`application.model_calls.agent_instructions`).
Whichever way the API takes the prompt (a Messages `system`, a Responses `instructions`, a chat `system` message),
the same instructions are read, and a line its framework writes into each call does not make a prompt a new one."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from minutehand.adapters.proxy.model_calls import Exchanged, span_of
from minutehand.application.model_calls import agent_instructions
from minutehand.domain.telemetry import Placement, SpanSource, StoredSpan

MOMENT = datetime(2026, 10, 4, tzinfo=UTC)
WORK = (
    "You keep the team's purchases moving.\nOnly order once the request is approved.\nTell the requester the outcome."
)
ANSWER = json.dumps({"id": "1", "choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}})


def _span(path: str, request: object) -> StoredSpan:
    span = span_of(
        Exchanged(
            host="api.example-model.test",
            path=path,
            status=200,
            request_body=json.dumps(request).encode(),
            request_type="application/json",
            response_body=ANSWER.encode(),
            response_type="application/json",
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


def _messages(system: str) -> StoredSpan:
    return _span("/v1/messages", {"model": "m", "system": system, "messages": [{"role": "user", "content": "go"}]})


def test_each_api_shape_gives_the_same_instructions() -> None:
    chat = _span(
        "/v1/chat/completions",
        {"model": "m", "messages": [{"role": "system", "content": WORK}, {"role": "user", "content": "go"}]},
    )
    responses = _span("/v1/responses", {"model": "m", "instructions": WORK, "input": "go"})

    for span in (_messages(WORK), chat, responses):
        assert agent_instructions([span]) == [WORK]


def test_a_line_the_framework_writes_into_each_call_does_not_make_a_new_prompt() -> None:
    calls = [_messages(f"x-request-header: id={n}\n{WORK}") for n in range(5)]

    assert agent_instructions(calls) == [f"x-request-header: id=4\n{WORK}"], "one prompt, as it was last given"


def test_a_prompt_that_changes_its_work_is_a_second_one_and_none_given_is_none() -> None:
    other = "You answer questions about the office.\nNever order anything."
    no_system = _span("/v1/messages", {"model": "m", "messages": [{"role": "user", "content": "go"}]})

    assert agent_instructions([_messages(WORK), _messages(other), _messages(WORK)]) == [WORK, other]
    assert agent_instructions([no_system]) == []
