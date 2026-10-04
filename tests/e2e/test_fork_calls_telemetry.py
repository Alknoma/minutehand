"""Forking a finished run, a call no provider claims, and the run as OpenTelemetry, each end to end."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext

from minutehand import session
from minutehand.adapters.telemetry.otel import OtelTelemetry
from minutehand.domain.checks import FindingKind
from minutehand.domain.experiment import Fork, PersonChange
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Silent
from minutehand.domain.world import Actor
from tests.e2e.support import ANSWER, T0, agent_under_test, answers, messages, scenario, texts, world

CALLER_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_SPAN = "00f067aa0ba902b7"


async def test_a_fork_where_the_silent_person_answers_ends_differently_and_leaves_its_parent_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", hooks=True)
    state = tmp_path / "state"
    [parent] = await session.play(scenario(Silent()), launched.agent, state=state, command=launched.command)
    assert parent.record.stop is StopReason.NOTHING_PENDING
    parent_events = world(state, parent.record.run_id).events()
    parent_calls = world(state, parent.record.run_id).calls()
    parent_files = {p.name: p.read_bytes() for p in session.run_dir(state, parent.record.run_id).glob("*.json")}

    after_first_wake = next(p for p in session.fork_points(state, parent.record.run_id) if p.wake == 1)
    changes = Fork(
        parent_run=parent.record.run_id,
        at_seq=after_first_wake.seq,
        overrides=[PersonChange(person="sofia", reply=answers(after=timedelta(hours=36)))],
    )
    [child] = await session.fork(parent.record.run_id, changes, state=state, command=launched.command)

    assert child.record.stop is StopReason.AGENT_DONE
    assert (child.record.parent_run, child.record.forked_at) == (parent.record.run_id, after_first_wake.seq)
    assert [w.sim_time for w in child.record.wakes] == [T0, T0 + timedelta(hours=36)]
    assert [f for f in child.result.findings if f.check == "no_follow_up"] == []
    assert [f.check for f in parent.result.findings if f.kind is FindingKind.FAIL].count("no_follow_up") == 1
    child_events = world(state, child.record.run_id, root=parent.record.run_id).events()
    assert [e for e in child_events if e.seq <= after_first_wake.seq] == [
        e for e in parent_events if e.seq <= after_first_wake.seq
    ]
    assert texts(messages(child_events, Actor.PERSON)) == [ANSWER]
    # The child's own calls are recorded in the child, each tied to the event it produced.
    after_fork = [e for e in messages(child_events, Actor.AGENT) if e.seq > after_first_wake.seq]
    assert len(after_fork) == 2 and all(e.exchange is not None and e.exchange.host == "slack.com" for e in after_fork)
    # The parent's log and files are as they were.
    assert world(state, parent.record.run_id).events() == parent_events
    assert world(state, parent.record.run_id).calls() == parent_calls
    assert {p.name: p.read_bytes() for p in session.run_dir(state, parent.record.run_id).glob("*.json")} == parent_files
    assert launched.state()["verified"] == [ANSWER]


async def test_a_call_to_a_host_no_provider_claims_is_refused_recorded_and_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    monkeypatch.setenv("STRAY_URL", "https://api.unclaimed.example/v1/ping?token=hush")

    [outcome] = await session.play(
        scenario(Silent()), launched.agent, state=tmp_path / "state", command=launched.command
    )

    [refused] = [c for c in world(tmp_path / "state", outcome.record.run_id).calls() if c.provider is None]
    assert (refused.exchange.host, refused.exchange.status, refused.wake) == ("api.unclaimed.example", 502, 1)
    assert "hush" not in refused.exchange.path
    [found] = [f for f in outcome.result.findings if f.check == "unmatched_call"]
    assert found.kind is FindingKind.REVIEW
    assert found.message.startswith("GET api.unclaimed.example/v1/ping") and found.message.endswith("answered 502")


async def test_the_run_is_exported_as_spans_and_the_agents_own_span_parents_its_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent")
    monkeypatch.setenv("TRACEPARENT", f"00-{CALLER_TRACE}-{CALLER_SPAN}-01")
    spans, logs = InMemorySpanExporter(), InMemoryLogRecordExporter()
    tracer_provider, logger_provider = TracerProvider(), LoggerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
    telemetry = OtelTelemetry(tracer_provider, logger_provider, MeterProvider(metric_readers=[InMemoryMetricReader()]))

    [outcome] = await session.play(
        scenario(answers(after=timedelta(hours=36))),
        launched.agent,
        state=tmp_path / "state",
        command=launched.command,
        telemetry=telemetry,
    )

    def context(span: ReadableSpan) -> SpanContext:
        assert span.context is not None
        return span.context

    finished = spans.get_finished_spans()
    [run] = [s for s in finished if s.name == "minutehand.run"]
    wakes = [s for s in finished if s.name == "minutehand.wake"]
    assert run.attributes is not None and run.attributes["minutehand.run_id"] == outcome.record.run_id
    assert len(wakes) == 2 and all(w.parent is not None and w.parent.span_id == context(run).span_id for w in wakes)
    posted = [s for s in finished if s.name == "slack create message"]
    by_agent = [s for s in posted if s.attributes is not None and s.attributes["minutehand.actor"] == "agent"]
    by_person = [s for s in posted if s.attributes is not None and s.attributes["minutehand.actor"] == "person"]
    assert len(by_agent) == 3 and len(by_person) == 1
    for span in by_agent:
        assert span.parent is not None and span.parent.is_remote
        assert (format(context(span).trace_id, "032x"), format(span.parent.span_id, "016x")) == (
            CALLER_TRACE,
            CALLER_SPAN,
        )
    [reply] = by_person  # the person's answer has no caller of its own: it parents to the wake it landed in
    assert reply.parent is not None and reply.parent.span_id == context(wakes[1]).span_id
    assert len(logs.get_finished_logs()) == len(outcome.result.findings)
