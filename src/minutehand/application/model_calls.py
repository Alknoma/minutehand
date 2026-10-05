"""The agent's side of a world event: the spans of the trace its call came from, and the model call that led to it.

An intercepted call that carried a W3C `traceparent` names the agent's span that made it. The join reads, from
the spans the run received, that span, its ancestors up to the root, and every span of the trace that
OpenTelemetry's GenAI conventions mark as generative AI (an attribute under `gen_ai.`). Of those, the model
call that led to the event is the last one to END before the calling span started (or, when that span never
arrived, before the event was written): a model answers, then the agent acts on the answer.

An event whose call carried no traceparent, or whose trace holds no model call, is joined by WAKE instead: the
last model call of the same wake that ended before the event, a call recorded on the wire counting only when it
was kept before the event's seq. That is the nearest preceding call, not a proven cause, and `JoinedBy.WAKE`
says so.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, Field

from minutehand.domain.checks import WakeModelCalls
from minutehand.domain.scenario import Model
from minutehand.domain.telemetry import AttributeValue, IntValue, SpanSource, StoredSpan, StringValue
from minutehand.domain.world import WorldEvent
from minutehand.ports.store import Store

GENAI = "gen_ai."
MODEL_OPERATIONS = frozenset({"chat", "text_completion", "generate_content"})
"""`gen_ai.operation.name` values that are a call to a model; `invoke_agent` and `execute_tool` are not."""


class JoinedBy(StrEnum):
    TRACE = "trace"  # the event's call carried a traceparent, and the model call is in that trace
    WAKE = "wake"  # the last model call in the same wake before the event: nearest, not proven


class ModelCall(Model):
    """What a model was asked and what it answered, as the span of the call carries it."""

    trace_id: str
    span_id: str
    name: str
    source: SpanSource = Field(description="received: the agent exported it; wire: Minutehand recorded it")
    wake: int = Field(description="The wake it arrived in")
    started: AwareDatetime = Field(description="Real time")
    ended: AwareDatetime = Field(description="Real time")
    model: str | None = Field(description="gen_ai.response.model, else gen_ai.request.model")
    system: str | None = Field(description="gen_ai.provider.name, else gen_ai.system")
    system_instructions: str | None = None
    input_messages: str | None = Field(description="gen_ai.input.messages as the span carries it, usually JSON")
    output_messages: str | None = Field(description="gen_ai.output.messages as the span carries it, usually JSON")
    input_tokens: int | None
    output_tokens: int | None


class EventTrace(Model):
    """The agent's side of one world event."""

    seq: int
    trace_id: str | None = Field(description="From the call's traceparent; None when it carried none")
    caller: StoredSpan | None = Field(description="The agent's span that made the call, when it was received")
    ancestors: list[StoredSpan] = Field(description="The caller's parent, its parent, and so on up to the root")
    genai: list[StoredSpan] = Field(description="Every span of the trace with a gen_ai.* attribute")
    model_call: ModelCall | None = Field(description="The model call that led to the event")
    joined_by: JoinedBy | None = Field(description="How model_call was found; None when it was not")


def _text(value: AttributeValue | None) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, StringValue) else value.model_dump_json()


def _count(value: AttributeValue | None) -> int | None:
    return value.value if isinstance(value, IntValue) else None


def _first(stored: StoredSpan, *keys: str) -> AttributeValue | None:
    return next((v for v in (stored.span.attribute(k) for k in keys) if v is not None), None)


def is_genai(stored: StoredSpan) -> bool:
    return any(a.key.startswith(GENAI) for a in stored.span.attributes)


def is_model_call(stored: StoredSpan) -> bool:
    """A span the GenAI conventions mark as a call to a model: its operation says so, or it names none and
    carries a model."""
    operation = _text(stored.span.attribute("gen_ai.operation.name"))
    if operation is not None:
        return operation in MODEL_OPERATIONS
    return _first(stored, "gen_ai.request.model", "gen_ai.response.model") is not None


def per_wake(spans: list[StoredSpan], wakes: list[int]) -> list[WakeModelCalls] | None:
    """How many model calls each wake made, by the wake each span is placed in; None when no span is a model
    call at all, since then nobody can tell a wake that made none from an agent that reports none."""
    calls = [s.wake for s in spans if is_model_call(s)]
    if not calls:
        return None
    return [WakeModelCalls(wake=w, calls=calls.count(w)) for w in wakes]


def model_call(stored: StoredSpan) -> ModelCall:
    span = stored.span
    return ModelCall(
        trace_id=span.trace_id,
        span_id=span.span_id,
        name=span.name,
        source=stored.source,
        wake=stored.wake,
        started=span.start,
        ended=span.end,
        model=_text(_first(stored, "gen_ai.response.model", "gen_ai.request.model")),
        system=_text(_first(stored, "gen_ai.provider.name", "gen_ai.system")),
        system_instructions=_text(span.attribute("gen_ai.system_instructions")),
        input_messages=_text(_first(stored, "gen_ai.input.messages", "gen_ai.prompt")),
        output_messages=_text(_first(stored, "gen_ai.output.messages", "gen_ai.completion")),
        input_tokens=_count(_first(stored, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")),
        output_tokens=_count(_first(stored, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")),
    )


def caller_of(traceparent: str | None) -> tuple[str, str] | None:
    """The trace id and span id a W3C `traceparent` names, when it is one."""
    if traceparent is None:
        return None
    parts = traceparent.strip().lower().split("-")
    if len(parts) < 4 or len(parts[1]) != 32 or len(parts[2]) != 16:
        return None
    trace_id, span_id = parts[1], parts[2]
    if any(c not in "0123456789abcdef" for c in trace_id + span_id) or set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None  # W3C: an id of all zeros is invalid
    return trace_id, span_id


def _last_before(candidates: list[StoredSpan], cutoff: datetime) -> StoredSpan | None:
    before = [s for s in candidates if s.span.end <= cutoff]
    return max(before, key=lambda s: s.span.end) if before else None


def trace_of(event: WorldEvent, world: Store) -> EventTrace:
    caller_ids = caller_of(event.exchange.traceparent if event.exchange is not None else None)
    caller: StoredSpan | None = None
    ancestors: list[StoredSpan] = []
    genai: list[StoredSpan] = []
    found: StoredSpan | None = None
    if caller_ids is not None:
        trace_id, span_id = caller_ids
        spans = world.spans(trace_id=trace_id)
        by_id = {s.span.span_id: s for s in spans}
        caller = by_id[span_id] if span_id in by_id else None
        parent = caller.span.parent_span_id if caller is not None else None
        while parent is not None and parent in by_id and all(a.span.span_id != parent for a in ancestors):
            ancestors.append(by_id[parent])
            parent = by_id[parent].span.parent_span_id
        genai = [s for s in spans if is_genai(s)]
        cutoff = caller.span.start if caller is not None else event.wall_time
        found = _last_before([s for s in genai if is_model_call(s)], cutoff)
    if found is not None:
        joined: JoinedBy | None = JoinedBy.TRACE
    else:
        in_wake = [
            s
            for s in world.spans(wake=event.wake)
            if is_model_call(s) and (s.source is SpanSource.RECEIVED or s.after_seq < event.seq)
        ]
        found = _last_before(in_wake, event.wall_time)
        joined = JoinedBy.WAKE if found is not None else None
    return EventTrace(
        seq=event.seq,
        trace_id=caller_ids[0] if caller_ids is not None else None,
        caller=caller,
        ancestors=ancestors,
        genai=genai,
        model_call=model_call(found) if found is not None else None,
        joined_by=joined,
    )
