"""A whole run of a real agent process whose lookup host is forwarded to an external emulator Minutehand starts:
the emulator is killed mid-run, and the run says its environment failed, exits 2, names the emulator, the first
call it failed and the end of its log. Minutehand's span of each forwarded call is the parent of the emulator's own
spans, which the receiver keeps with the run in the wake they happened in. A fork after the emulator's first use
is refused: its state is outside the run's record."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from minutehand import session
from minutehand.adapters.telemetry.otel import ChosenIds, OtelTelemetry
from minutehand.application.emulators import CHECK, health_changes
from minutehand.application.refusals import RunRefused
from minutehand.domain.checks import FindingKind
from minutehand.domain.emulator import EmulatorHealth, ExternalEmulator, Health, ReadyHttp, Upstream
from minutehand.domain.experiment import Fork
from minutehand.domain.outbound import Forward
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.world import CallOutcome
from tests.e2e.support import agent_under_test, answers, scenario
from tests.emulators.support import stand_in

HOST = "api.tracker.test"


async def test_an_emulator_killed_mid_run_fails_the_environment_not_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", hooks=True)
    emulator = ExternalEmulator(
        name="tracker",
        upstream=Upstream(url="http://127.0.0.1:{port}"),
        command=stand_in(tmp_path),
        ready=ReadyHttp(path="/ready"),
        health=Health(every=timedelta(seconds=30)),
    )
    agent = launched.agent.model_copy(
        update={"outbound": [Forward(host=HOST, emulator="tracker")], "emulators": [emulator]}
    )
    # Its readiness check and the agent's first lookup are answered; the second lookup kills it.
    monkeypatch.setenv("DIES_AFTER", "2")
    monkeypatch.setenv("LOOKUP_URL", f"https://{HOST}/search?q=partner+pricing")
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider(id_generator=ChosenIds())
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    telemetry = OtelTelemetry(tracer_provider, LoggerProvider(), MeterProvider(metric_readers=[InMemoryMetricReader()]))
    state = tmp_path / "state"

    [outcome] = await session.play(
        scenario(answers(after=timedelta(hours=36))),
        agent,
        state=state,
        command=launched.command,
        telemetry=telemetry,
    )

    record, result = outcome.record, outcome.result
    assert record.stop is StopReason.ENVIRONMENT_FAILED
    assert record.failure is not None and record.failure.startswith("emulator tracker ")
    assert result.verdict.kind is VerdictKind.ENVIRONMENT_FAILED and result.exit_code == 2
    assert result.verdict.words.startswith("Environment failed:")
    [said] = [f for f in result.findings if f.check == CHECK]
    assert said.kind is FindingKind.FAIL and said.wake == 2
    assert f"the first call it failed: GET {HOST}/search at wake 2, answered 502" in said.message
    assert "dying on request 3" in said.message
    [use] = record.emulators
    assert (use.emulator, use.answered, use.unavailable) == ("tracker", 1, 1)

    with session.reading(state, record.run_id) as kept:
        calls = [c for c in kept.calls() if c.exchange.host == HOST]
        assert [(c.wake, c.exchange.outcome) for c in calls] == [
            (1, CallOutcome.ANSWERED),
            (2, CallOutcome.UNAVAILABLE),
        ]
        changes = [h.health for h in health_changes(kept, "tracker")]
        assert changes[0] is EmulatorHealth.READY and changes[-1] is EmulatorHealth.STOPPED
        assert EmulatorHealth.DIED in changes or EmulatorHealth.UNHEALTHY in changes
        # The emulator's own span, under Minutehand's span of the call it handled, placed in that call's wake.
        answered = calls[0].exchange.captured
        assert answered is not None and answered.forwarded_traceparent is not None
        _, trace, parent, _ = answered.forwarded_traceparent.split("-")
        [its_own] = [s for s in kept.spans(trace_id=trace) if s.span.name == "stand-in handles"]
        assert its_own.span.parent_span_id == parent and its_own.wake == 1
        assert its_own.span.service_name == "tracker"

    # Minutehand's own span of that call is the parent the emulator was told of, in the same trace.
    [mine] = [
        s
        for s in spans.get_finished_spans()
        if s.kind is SpanKind.CLIENT and (s.attributes or {}).get("minutehand.call.outcome") == "answered"
    ]
    assert mine.context is not None
    assert format(mine.context.span_id, "016x") == parent and format(mine.context.trace_id, "032x") == trace
    attributes = dict(mine.attributes or {})
    assert attributes["minutehand.emulator"] == "tracker" and attributes["server.address"] == HOST
    assert attributes["minutehand.operation"] == "GET /search"
    health = [s.name for s in spans.get_finished_spans() if s.name.startswith("minutehand.emulator")]
    assert health[0] == "minutehand.emulator ready" and health[-1] == "minutehand.emulator stopped"

    after_first = next(p for p in session.fork_points(state, record.run_id) if p.wake == 1)
    with pytest.raises(RunRefused, match="cannot rewind the external emulator"):
        await session.fork(
            record.run_id,
            Fork(parent_run=record.run_id, at_seq=after_first.seq),
            state=state,
            command=launched.command,
        )
