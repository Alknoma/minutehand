"""Which model call led to a world event: by trace when the call carried a traceparent, else the nearest before it
in the same wake."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.model_calls import JoinedBy, trace_of
from minutehand.application.run_clock import RunClock
from minutehand.domain.telemetry import Attribute, ReceivedSpan, SpanSource, StringValue
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    Exchange,
    MessageSnapshot,
    Operation,
    WorldEvent,
)

START = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
TRACE = "0af7651916cd43dd8448eb211c80319c"


def span(
    span_id: str, name: str, start: datetime, *, parent: str | None = None, model: str | None = None
) -> ReceivedSpan:
    attributes = [Attribute(key="gen_ai.request.model", value=StringValue(value=model))] if model else []
    return ReceivedSpan(
        trace_id=TRACE,
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        start=start,
        end=start + timedelta(milliseconds=10),
        attributes=attributes,
    )


@pytest.fixture
def world(tmp_path: Path) -> SqliteStore:
    clock = RunClock(START)
    clock.begin_wake()
    return SqliteStore(tmp_path / "world.db", "run", clock)


def posted(world: SqliteStore, traceparent: str | None) -> WorldEvent:
    ref = EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id="1.0001")
    event = world.apply(Change(entity=ref, operation=Operation.CREATE, actor=Actor.AGENT, body="{}", parent="D1"))
    call = Exchange(method="POST", host="slack.com", path="/api/chat.postMessage", status=200, traceparent=traceparent)
    return event.model_copy(update={"exchange": call})


def test_the_last_model_call_to_end_before_the_calling_span_started_led_to_it(world: SqliteStore) -> None:
    real = datetime.now(UTC) - timedelta(minutes=1)
    world.receive(
        [
            span("a" * 16, "turn", real),
            span("b" * 16, "chat early", real + timedelta(seconds=1), parent="a" * 16, model="m-early"),
            span("c" * 16, "chat chosen", real + timedelta(seconds=2), parent="a" * 16, model="m-chosen"),
            span("d" * 16, "tool", real + timedelta(seconds=3), parent="a" * 16),
            span("e" * 16, "chat after", real + timedelta(seconds=4), parent="a" * 16, model="m-after"),
        ],
        source=SpanSource.RECEIVED,
    )
    event = posted(world, f"00-{TRACE}-{'d' * 16}-01")

    joined = trace_of(event, world)

    assert joined.caller is not None and joined.caller.span.name == "tool"
    assert [s.span.name for s in joined.ancestors] == ["turn"]
    assert [s.span.name for s in joined.genai] == ["chat early", "chat chosen", "chat after"]
    assert joined.model_call is not None and joined.model_call.model == "m-chosen"
    assert joined.joined_by is JoinedBy.TRACE


def test_with_no_traceparent_the_model_call_recorded_on_the_wire_before_the_event_in_its_wake_is_taken(
    world: SqliteStore,
) -> None:
    real = datetime.now(UTC) - timedelta(minutes=1)
    world.receive([span("b" * 16, "chat before", real, model="m-before")], source=SpanSource.WIRE)
    event = posted(world, None)
    world.receive([span("c" * 16, "chat after", real + timedelta(seconds=1), model="m-after")], source=SpanSource.WIRE)

    joined = trace_of(event, world)

    assert joined.trace_id is None and joined.caller is None
    assert joined.model_call is not None and joined.model_call.model == "m-before"
    assert (joined.model_call.source, joined.joined_by) == (SpanSource.WIRE, JoinedBy.WAKE)


def test_nothing_received_joins_nothing(world: SqliteStore) -> None:
    joined = trace_of(posted(world, f"00-{TRACE}-{'d' * 16}-01"), world)
    assert (joined.trace_id, joined.caller, joined.model_call, joined.joined_by) == (TRACE, None, None, None)


def answered(span_id: str, start: datetime, said: str, *, model: str) -> ReceivedSpan:
    """A model call whose answer is a tool call carrying `said` as its argument: JSON inside JSON, as an
    OpenAI-shaped `gen_ai.output.messages` carries it."""
    arguments = json.dumps({"channel": "D1", "text": said})
    output = json.dumps(
        [{"role": "assistant", "parts": [{"type": "tool_call", "name": "send", "arguments": arguments}]}]
    )
    attributes = [
        Attribute(key="gen_ai.operation.name", value=StringValue(value="chat")),
        Attribute(key="gen_ai.request.model", value=StringValue(value=model)),
        Attribute(key="gen_ai.output.messages", value=StringValue(value=output)),
    ]
    return ReceivedSpan(
        trace_id=TRACE,
        span_id=span_id,
        name="chat",
        start=start,
        end=start + timedelta(milliseconds=10),
        attributes=attributes,
    )


def sent(world: SqliteStore, text: str) -> WorldEvent:
    ref = EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id="1.0002")
    snapshot = MessageSnapshot(text=text, channel="D1", recipient_emails=["dani@example.com"])
    return world.apply(
        Change(entity=ref, operation=Operation.CREATE, actor=Actor.AGENT, body="{}", parent="D1", after=snapshot)
    )


def test_with_no_trace_link_the_call_that_answered_the_messages_text_verbatim_wrote_it(world: SqliteStore) -> None:
    """Ranked above the nearest call: the one that wrote it ended earlier, and a later one wrote something else."""
    text = "Morning, Dani — just a hello came through. Nothing you need to act on."
    real = datetime.now(UTC) - timedelta(minutes=1)
    world.receive(
        [
            answered("b" * 16, real, f"  {text}\n", model="m-wrote"),
            answered(
                "c" * 16, real + timedelta(seconds=1), "Something else entirely, to someone else.", model="m-near"
            ),
        ],
        source=SpanSource.RECEIVED,
    )
    joined = trace_of(sent(world, text), world)
    assert joined.model_call is not None and joined.model_call.model == "m-wrote"
    assert joined.joined_by is JoinedBy.CONTENT


@pytest.mark.parametrize(
    ("written", "why"),
    [
        ("Morning, Dani - just a hello came through. Nothing you need to act on.", "one character differs"),
        ("ok", "too short to join by"),
    ],
)
def test_a_text_not_answered_verbatim_or_too_short_is_joined_only_as_the_nearest_call(
    world: SqliteStore, written: str, why: str
) -> None:
    real = datetime.now(UTC) - timedelta(minutes=1)
    said = "Morning, Dani — just a hello came through. Nothing you need to act on." if len(written) > 2 else "ok"
    world.receive([answered("b" * 16, real, said, model="m")], source=SpanSource.RECEIVED)
    joined = trace_of(sent(world, written), world)
    assert joined.joined_by is JoinedBy.WAKE, why
