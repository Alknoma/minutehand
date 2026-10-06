"""The agent's spans kept with the run: stamped as they arrive, read by trace and by wake, shared with a fork up
to the fork, and a file from an older schema refused."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from minutehand.adapters.store.sqlite import SCHEMA_VERSION, SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.telemetry import (
    ArrayValue,
    Attribute,
    IntValue,
    MapValue,
    Placement,
    ReceivedSpan,
    Signal,
    SpanSource,
    StringValue,
)
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation

START = datetime(2026, 8, 24, 10, 51, tzinfo=UTC)
REAL = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
TRACE_A = "a" * 32
TRACE_B = "b" * 32


def span(
    trace_id: str, span_id: str, name: str = "work", parent: str | None = None, *, start: datetime = REAL
) -> ReceivedSpan:
    return ReceivedSpan(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        start=start,
        end=start + timedelta(milliseconds=30),
        attributes=[
            Attribute(key="gen_ai.request.model", value=StringValue(value="model-luna")),
            Attribute(key="gen_ai.usage.input_tokens", value=IntValue(value=12)),
            Attribute(
                key="nested",
                value=MapValue(values=[Attribute(key="list", value=ArrayValue(values=[IntValue(value=1)]))]),
            ),
        ],
        service_name="agent",
    )


def change(n: int) -> Change:
    ref = EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id=f"m{n}")
    return Change(entity=ref, operation=Operation.CREATE, actor=Actor.AGENT, body="{}", parent="C1")


@pytest.fixture
def world(tmp_path: Path) -> tuple[SqliteStore, RunClock]:
    clock = RunClock(START)
    return SqliteStore(tmp_path / "world.db", "root", clock), clock


def test_a_batch_is_stamped_with_the_wake_simulated_time_and_head_it_arrived_at(
    world: tuple[SqliteStore, RunClock],
) -> None:
    store, clock = world
    store.apply(change(1))
    clock.jump(START + timedelta(days=2))
    clock.begin_wake()
    stored = store.receive([span(TRACE_A, "1" * 16), span(TRACE_B, "2" * 16)], source=SpanSource.RECEIVED)

    assert [(s.wake, s.sim_time, s.after_seq, s.source) for s in stored] == [
        (1, START + timedelta(days=2), 1, SpanSource.RECEIVED)
    ] * 2
    assert store.spans() == stored
    # The span's own times are the real times its SDK gave it, not the simulated clock.
    assert stored[0].span.start == REAL
    assert store.spans()[0].span == span(TRACE_A, "1" * 16)


def test_spans_are_read_by_trace_and_by_wake_in_the_order_they_arrived(world: tuple[SqliteStore, RunClock]) -> None:
    store, clock = world
    store.receive([span(TRACE_A, "1" * 16), span(TRACE_B, "2" * 16)], source=SpanSource.RECEIVED)
    clock.begin_wake()
    store.receive([span(TRACE_A, "3" * 16, parent="1" * 16)], source=SpanSource.WIRE)

    assert [s.span.span_id for s in store.spans(trace_id=TRACE_A)] == ["1" * 16, "3" * 16]
    assert [s.span.span_id for s in store.spans(wake=0)] == ["1" * 16, "2" * 16]
    assert [s.span.span_id for s in store.spans(trace_id=TRACE_A, wake=1)] == ["3" * 16]
    assert [s.source for s in store.spans(wake=1)] == [SpanSource.WIRE]
    assert store.spans(trace_id="c" * 32) == []


def _wake(store: SqliteStore, clock: RunClock) -> int:
    wake = clock.begin_wake()
    store.wake_began(wake)
    return wake


def _now() -> datetime:
    return datetime.now(UTC)


def test_a_span_is_placed_in_the_wake_whose_real_window_holds_its_start_however_late_it_arrives(
    world: tuple[SqliteStore, RunClock],
) -> None:
    store, clock = world
    before = span(TRACE_A, "1" * 16, "before any wake", start=_now() - timedelta(hours=1))
    _wake(store, clock)
    during_first = _now()
    store.receive([span(TRACE_A, "2" * 16, "on time", start=_now())], source=SpanSource.RECEIVED)
    store.wake_ended(1)
    _wake(store, clock)
    late = store.receive(
        [span(TRACE_A, "3" * 16, "flushed late", start=during_first), before], source=SpanSource.RECEIVED
    )

    assert [(s.wake, s.arrived_in_wake, s.placed_by) for s in late] == [
        (1, 2, Placement.WINDOW),
        (2, 2, Placement.ARRIVAL),
    ]
    assert [s.span.name for s in store.spans(wake=1)] == ["on time", "flushed late"]
    assert [s.span.name for s in store.spans(wake=2)] == ["before any wake"]


def test_a_fork_sees_its_parents_spans_of_the_wakes_up_to_the_fork_however_late_they_arrived(
    world: tuple[SqliteStore, RunClock],
) -> None:
    store, clock = world
    store.receive([span(TRACE_A, "1" * 16, "at setup")], source=SpanSource.RECEIVED)
    _wake(store, clock)
    during_first = _now()
    forked_at = store.apply(change(1)).seq
    store.wake_ended(1)
    _wake(store, clock)
    store.apply(change(2))
    # A batching exporter flushes wake 1's span during wake 2, after the seq the fork is taken at.
    store.receive([span(TRACE_A, "2" * 16, "of wake 1, flushed late", start=during_first)], source=SpanSource.RECEIVED)
    store.receive([span(TRACE_A, "3" * 16, "of wake 2", start=_now())], source=SpanSource.RECEIVED)

    fork = store.fork("what-if", at_seq=forked_at, clock=RunClock(START))
    fork.receive([span(TRACE_A, "4" * 16, "in the fork")], source=SpanSource.RECEIVED)

    assert [s.span.name for s in fork.spans(trace_id=TRACE_A)] == ["at setup", "of wake 1, flushed late", "in the fork"]
    assert [s.run_id for s in fork.spans()] == ["root", "root", "what-if"]
    assert [s.span.name for s in store.spans()] == ["at setup", "of wake 1, flushed late", "of wake 2"]


def test_a_failed_forward_is_recorded_with_the_run(world: tuple[SqliteStore, RunClock]) -> None:
    store, clock = world
    clock.begin_wake()
    failure = store.forward_failed(Signal.TRACES, "http://collector.example:4318/v1/traces", "connection refused")
    assert store.forward_failures() == [failure]
    assert (failure.wake, failure.sim_time) == (1, START)
    assert store.fork("what-if", at_seq=0, clock=RunClock(START)).forward_failures() == []


def test_a_discarded_fork_takes_its_spans_and_forward_failures_with_it(
    world: tuple[SqliteStore, RunClock], tmp_path: Path
) -> None:
    store, _ = world
    store.receive([span(TRACE_A, "1" * 16, "the parent's")], source=SpanSource.RECEIVED)
    fork = store.fork("refused", at_seq=0, clock=RunClock(START))
    fork.receive([span(TRACE_A, "2" * 16, "the fork's")], source=SpanSource.RECEIVED)
    fork.forward_failed(Signal.TRACES, "http://collector.example:4318/v1/traces", "connection refused")

    fork.discard()

    [path] = tmp_path.glob("*.db")
    db = sqlite3.connect(path)
    for table in ("span", "forward_failure"):
        assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id='refused'").fetchone()[0] == 0, table
    assert [s.span.name for s in store.spans()] == ["the parent's"]


OLDER_SCHEMA = """
CREATE TABLE run(run_id TEXT PRIMARY KEY, parent TEXT, forked_at INTEGER, forked_calls INTEGER);
CREATE TABLE event(run_id TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY (run_id, seq));
CREATE TABLE reply(run_id TEXT NOT NULL, position INTEGER NOT NULL, reply TEXT NOT NULL);
PRAGMA user_version=2;
"""


def test_a_file_written_before_spans_were_kept_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "older.db"
    with sqlite3.connect(path) as db:
        db.executescript(OLDER_SCHEMA)
    assert SCHEMA_VERSION > 2
    with pytest.raises(RuntimeError, match="was written with store schema 2; this version reads schema"):
        SqliteStore(path, "root", RunClock(START))
