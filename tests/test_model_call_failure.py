"""An agent's model call that failed says why: the model API's error answer as recorded on the wire, or the error
the agent's own span reports. An answered call has no failure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from minutehand.application.model_calls import model_call
from minutehand.domain.telemetry import (
    Attribute,
    IntValue,
    Placement,
    ReceivedSpan,
    SpanSource,
    SpanStatus,
    StoredSpan,
    StringValue,
)

AT = datetime(2026, 8, 24, 9, tzinfo=UTC)


def _stored(*attributes: Attribute, status: SpanStatus = SpanStatus.UNSET, said: str | None = None) -> StoredSpan:
    span = ReceivedSpan(
        trace_id="a" * 32,
        span_id="b" * 16,
        name="chat m",
        start=AT,
        end=AT + timedelta(seconds=1),
        attributes=[Attribute(key="gen_ai.operation.name", value=StringValue(value="chat")), *attributes],
        status=status,
        status_message=said,
    )
    return StoredSpan(
        span=span,
        run_id="r",
        source=SpanSource.WIRE,
        wake=1,
        placed_by=Placement.WINDOW,
        arrived_in_wake=1,
        sim_time=AT,
        after_seq=0,
    )


def test_an_error_answer_on_the_wire_is_the_failure_with_the_apis_message() -> None:
    refused = _stored(
        Attribute(key="http.response.status_code", value=IntValue(value=400)),
        Attribute(
            key="minutehand.response.body", value=StringValue(value='{"error": {"message": "use /v1/responses"}}')
        ),
    )
    assert model_call(refused).failure == "answered 400: use /v1/responses"
    plain = _stored(
        Attribute(key="http.response.status_code", value=IntValue(value=502)),
        Attribute(key="minutehand.response.body", value=StringValue(value="Bad Gateway")),
    )
    assert model_call(plain).failure == "answered 502"


def test_an_error_the_agents_span_reports_is_the_failure() -> None:
    typed = _stored(Attribute(key="error.type", value=StringValue(value="RateLimitError")))
    assert model_call(typed).failure == "RateLimitError"
    assert model_call(_stored(status=SpanStatus.ERROR, said="timed out")).failure == "timed out"


def test_an_answered_call_has_no_failure() -> None:
    answered = _stored(Attribute(key="http.response.status_code", value=IntValue(value=200)))
    assert model_call(answered).failure is None and model_call(_stored()).failure is None
