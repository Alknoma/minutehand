"""The run as OpenTelemetry, read back from the SDK's in-memory exporters."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider, ReadableLogRecord
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader, NumberDataPoint
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from minutehand.adapters.telemetry.otel import BODIES_VARIABLE, ENDPOINT_VARIABLE, OtelTelemetry, from_environment
from minutehand.domain.agent import WakeReason
from minutehand.domain.checks import Effectiveness, Finding, FindingKind, Severity, WakeRecord
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Person, Scenario
from minutehand.domain.world import (
    Actor,
    AnsweredBy,
    Body,
    BodyKept,
    Captured,
    CaptureMode,
    EntityKind,
    EntityRef,
    Exchange,
    Operation,
    RecordedCall,
    TicketSnapshot,
    WorldEvent,
)
from minutehand.ports.telemetry import Telemetry

START = datetime(2026, 8, 24, 10, 50, 3, tzinfo=UTC)
WALL = datetime(2026, 10, 4, 9, 0, 0, 123456, tzinfo=UTC)
CALLER_TRACE = "0af7651916cd43dd8448eb211c80319c"
CALLER_SPAN = "b7ad6b7169203331"
TRACEPARENT = f"00-{CALLER_TRACE}-{CALLER_SPAN}-01"
SCENARIO = Scenario(
    name="partner_pipeline_build",
    goal="Three signed partnership agreements.",
    owner="owner",
    starts_at=START,
    seed=29,
    people=[Person(key="owner", name="Test User", email="owner@example.com")],
)


class Exported:
    def __init__(self, export_bodies: bool = False) -> None:
        self.spans = InMemorySpanExporter()
        self.logs = InMemoryLogRecordExporter()
        self.metrics = InMemoryMetricReader()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        logger_provider = LoggerProvider()
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(self.logs))
        self.telemetry = OtelTelemetry(
            tracer_provider, logger_provider, MeterProvider(metric_readers=[self.metrics]), export_bodies=export_bodies
        )

    def span(self, name: str) -> ReadableSpan:
        named = [s for s in self.spans.get_finished_spans() if s.name == name]
        assert len(named) == 1, [s.name for s in self.spans.get_finished_spans()]
        return named[0]

    def finding_logs(self) -> list[ReadableLogRecord]:
        return list(self.logs.get_finished_logs())

    def sums(self, metric: str) -> dict[frozenset[tuple[str, object]], float]:
        return {
            frozenset((p.attributes or {}).items()): p.value
            for p in self._points(metric)
            if isinstance(p, NumberDataPoint)
        }

    def histogram(self, metric: str) -> HistogramDataPoint:
        points = [p for p in self._points(metric) if isinstance(p, HistogramDataPoint)]
        assert len(points) == 1
        return points[0]

    def _points(self, metric: str) -> list[object]:
        found = self.metrics.get_metrics_data()
        assert found is not None
        return [
            point
            for resource in found.resource_metrics
            for scope in resource.scope_metrics
            for m in scope.metrics
            if m.name == metric
            for point in m.data.data_points
        ]


@pytest.fixture
def exported() -> Exported:
    return Exported()


def event(
    seq: int,
    *,
    wake: int = 1,
    operation: Operation = Operation.CREATE,
    actor: Actor = Actor.AGENT,
    status: int = 201,
    traceparent: str | None = None,
    exchange: bool = True,
) -> WorldEvent:
    return WorldEvent(
        seq=seq,
        run_id="r1",
        wake=wake,
        sim_time=START + timedelta(days=wake, minutes=seq),
        wall_time=WALL + timedelta(seconds=seq),
        actor=actor,
        operation=operation,
        entity=EntityRef(provider="asana", kind=EntityKind.TICKET, external_id=f"T{seq}"),
        after=TicketSnapshot(title="Legal review"),
        exchange=Exchange(
            method="POST",
            host="app.asana.com",
            path="/api/1.0/tasks",
            status=status,
            request_body='{"data": {"name": "Legal review"}}',
            response_body='{"data": {"gid": "T1"}}',
            traceparent=traceparent,
        )
        if exchange
        else None,
    )


def in_one_wake(exported: Exported, *events: WorldEvent) -> None:
    exported.telemetry.run_started("r1", SCENARIO)
    exported.telemetry.wake_started(1, WakeReason.DUE, START + timedelta(days=1))
    for e in events:
        exported.telemetry.recorded(e)
    exported.telemetry.wake_ended(1)


def record(wakes: int) -> RunRecord:
    return RunRecord(
        run_id="r1",
        scenario=SCENARIO.name,
        seed=SCENARIO.seed,
        started_at=START,
        ended_at=START + timedelta(days=3),
        wall_seconds=42.5,
        stop=StopReason.AGENT_DONE,
        wakes=[
            WakeRecord(index=i, sim_time=START + timedelta(days=i), world_changes=0, commitments_changed=False)
            for i in range(1, wakes + 1)
        ],
    )


def test_it_satisfies_the_port(exported: Exported) -> None:
    telemetry: Telemetry = exported.telemetry
    assert telemetry is exported.telemetry


def test_run_wake_and_event_spans_carry_their_names_and_attributes(exported: Exported) -> None:
    in_one_wake(exported, event(1))
    exported.telemetry.run_ended(record(1), None)

    run = exported.span("minutehand.run")
    assert run.parent is None
    assert run.attributes is not None
    assert run.attributes["minutehand.run_id"] == "r1"
    assert run.attributes["minutehand.scenario"] == "partner_pipeline_build"
    assert run.attributes["minutehand.seed"] == 29
    assert run.attributes["minutehand.sim_time"] == START.isoformat()

    wake = exported.span("minutehand.wake")
    assert wake.parent is not None and run.context is not None
    assert wake.parent.span_id == run.context.span_id
    assert wake.attributes is not None
    assert wake.attributes["minutehand.wake"] == 1
    assert wake.attributes["minutehand.wake.reason"] == "due"
    assert wake.attributes["minutehand.sim_time"] == (START + timedelta(days=1)).isoformat()

    span = exported.span("asana create ticket")
    assert span.kind == SpanKind.SERVER
    assert span.attributes is not None
    assert span.attributes["http.request.method"] == "POST"
    assert span.attributes["server.address"] == "app.asana.com"
    assert span.attributes["url.path"] == "/api/1.0/tasks"
    assert span.attributes["http.response.status_code"] == 201
    assert span.attributes["minutehand.entity.id"] == "T1"
    assert span.attributes["minutehand.actor"] == "agent"


def test_an_event_span_is_stamped_with_wall_time_and_carries_simulated_time_as_two_attributes(
    exported: Exported,
) -> None:
    in_one_wake(exported, event(3))

    span = exported.span("asana create ticket")
    wall_ns = 1_791_104_403_123_456_000  # 2026-10-04T09:00:03.123456Z
    assert span.start_time == wall_ns
    assert span.end_time == wall_ns
    sim = START + timedelta(days=1, minutes=3)
    assert span.attributes is not None
    assert span.attributes["minutehand.sim_time"] == "2026-08-25T10:53:03+00:00"
    assert span.attributes["minutehand.sim_time_unix_nano"] == int(sim.timestamp()) * 1_000_000_000


def test_an_event_with_a_traceparent_joins_the_callers_trace_and_links_to_the_wake(exported: Exported) -> None:
    in_one_wake(exported, event(1, traceparent=TRACEPARENT))

    span = exported.span("asana create ticket")
    wake = exported.span("minutehand.wake")
    assert span.context is not None and wake.context is not None
    assert format(span.context.trace_id, "032x") == CALLER_TRACE
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == CALLER_SPAN
    assert span.parent.is_remote
    assert [(link.context.trace_id, link.context.span_id) for link in span.links] == [
        (wake.context.trace_id, wake.context.span_id)
    ]


@pytest.mark.parametrize(
    "traceparent",
    [None, "not-a-traceparent", f"00-{'0' * 32}-{CALLER_SPAN}-01", f"00-{CALLER_TRACE}-{CALLER_SPAN}"],
    ids=["absent", "garbage", "zero-trace-id", "missing-flags"],
)
def test_an_event_without_a_valid_traceparent_is_a_child_of_the_wake(
    exported: Exported, traceparent: str | None
) -> None:
    in_one_wake(exported, event(1, traceparent=traceparent))

    span = exported.span("asana create ticket")
    wake = exported.span("minutehand.wake")
    assert span.context is not None and wake.context is not None
    assert span.context.trace_id == wake.context.trace_id
    assert span.parent is not None and span.parent.span_id == wake.context.span_id
    assert list(span.links) == []


def test_a_setup_event_outside_any_wake_is_a_child_of_the_run(exported: Exported) -> None:
    exported.telemetry.run_started("r1", SCENARIO)
    exported.telemetry.recorded(event(1, wake=0, actor=Actor.SCENARIO, exchange=False))
    exported.telemetry.run_ended(record(0), None)

    span = exported.span("asana create ticket")
    run = exported.span("minutehand.run")
    assert span.parent is not None and run.context is not None
    assert span.parent.span_id == run.context.span_id
    assert span.attributes is not None
    assert "http.request.method" not in span.attributes


def test_a_4xx_from_a_fake_is_not_an_error(exported: Exported) -> None:
    in_one_wake(exported, event(1, status=404))
    assert exported.span("asana create ticket").status.status_code == StatusCode.UNSET


def test_a_5xx_from_a_fake_is_an_error(exported: Exported) -> None:
    in_one_wake(exported, event(1, status=503))
    assert exported.span("asana create ticket").status.status_code == StatusCode.ERROR


def test_bodies_are_not_exported_by_default(exported: Exported) -> None:
    in_one_wake(exported, event(1))
    attributes = exported.span("asana create ticket").attributes
    assert attributes is not None
    assert not any("body" in key for key in attributes)


def test_bodies_are_exported_with_the_flag() -> None:
    exported = Exported(export_bodies=True)
    in_one_wake(exported, event(1))
    attributes = exported.span("asana create ticket").attributes
    assert attributes is not None
    assert attributes["minutehand.request.body"] == '{"data": {"name": "Legal review"}}'
    assert attributes["minutehand.response.body"] == '{"data": {"gid": "T1"}}'


def test_the_environment_flag_turns_bodies_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENDPOINT_VARIABLE, raising=False)
    monkeypatch.setenv(BODIES_VARIABLE, "1")
    assert from_environment()._export_bodies  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setenv(BODIES_VARIABLE, "0")
    assert not from_environment()._export_bodies  # pyright: ignore[reportPrivateUsage]


def finding(evidence: list[int], severity: Severity = Severity.ERROR) -> Finding:
    return Finding(
        check="near_miss_name",
        severity=severity,
        kind=FindingKind.FAIL,
        message='wrote "Aiven" where the scenario says "Ayven"',
        at=START + timedelta(days=1),
        wake=1,
        evidence=evidence,
        pattern="confirm_names",
    )


def test_a_finding_is_a_log_record_in_the_trace_of_its_first_evidence(exported: Exported) -> None:
    in_one_wake(exported, event(1), event(2, traceparent=TRACEPARENT))
    exported.telemetry.found(finding([2, 1]))

    [log] = exported.finding_logs()
    evidence = next(
        s
        for s in exported.spans.get_finished_spans()
        if s.attributes is not None and s.attributes["minutehand.seq"] == 2
    )
    assert evidence.context is not None
    assert log.log_record.trace_id == evidence.context.trace_id
    assert log.log_record.span_id == evidence.context.span_id
    assert log.log_record.body == 'wrote "Aiven" where the scenario says "Ayven"'
    assert log.log_record.severity_number == SeverityNumber.ERROR
    assert log.log_record.attributes is not None
    assert log.log_record.attributes["minutehand.check"] == "near_miss_name"
    assert log.log_record.attributes["minutehand.finding.kind"] == "fail"
    assert log.log_record.attributes["minutehand.pattern"] == "confirm_names"
    assert log.log_record.attributes["minutehand.sim_time"] == (START + timedelta(days=1)).isoformat()


def test_a_finding_without_known_evidence_carries_no_trace_context(exported: Exported) -> None:
    in_one_wake(exported, event(1))
    exported.telemetry.found(finding([]))
    exported.telemetry.found(finding([99]))

    for log in exported.finding_logs():
        assert log.log_record.trace_id == 0
        assert log.log_record.span_id == 0


@pytest.mark.parametrize(
    ("severity", "number"),
    [
        (Severity.ERROR, SeverityNumber.ERROR),
        (Severity.WARNING, SeverityNumber.WARN),
        (Severity.INFORMATION, SeverityNumber.INFO),
    ],
)
def test_a_findings_severity_maps_to_otel(exported: Exported, severity: Severity, number: SeverityNumber) -> None:
    exported.telemetry.found(finding([], severity))
    [log] = exported.finding_logs()
    assert log.log_record.severity_number == number


def test_metrics_after_a_small_scripted_run(exported: Exported) -> None:
    t = exported.telemetry
    t.run_started("r1", SCENARIO)
    t.recorded(event(1, wake=0, actor=Actor.SCENARIO))
    # wake 1: the agent creates a ticket
    t.wake_started(1, WakeReason.START, START)
    t.recorded(event(2, wake=1))
    t.wake_ended(1)
    # wake 2: the agent only reads and searches; a person's change does not count as the agent's
    t.wake_started(2, WakeReason.DUE, START + timedelta(days=1))
    t.recorded(event(3, wake=2, operation=Operation.READ))
    t.recorded(event(4, wake=2, operation=Operation.SEARCH))
    t.recorded(event(5, wake=2, actor=Actor.PERSON, operation=Operation.UPDATE))
    t.wake_ended(2)
    # wake 3: the agent updates a ticket after reading it
    t.wake_started(3, WakeReason.PERSON_REPLIED, START + timedelta(days=2))
    t.recorded(event(6, wake=3, operation=Operation.READ))
    t.recorded(event(7, wake=3, operation=Operation.UPDATE))
    t.wake_ended(3)
    t.found(finding([2]))
    t.found(finding([7]))
    t.found(Finding(check="repeated_message", severity=Severity.WARNING, kind=FindingKind.REVIEW, message="twice"))
    t.run_ended(
        record(3),
        Effectiveness(
            expectations_met=3,
            expectations_total=4,
            waits_opened=12,
            waits_open_at_end=5,
            follow_ups_made=3,
            wakes=3,
            idle_wakes=1,
            failed_checks=5,
        ),
    )

    assert exported.sums("minutehand.wakes") == {
        frozenset({("changed", True)}): 2,
        frozenset({("changed", False)}): 1,
    }
    assert exported.sums("minutehand.findings") == {
        frozenset({("check", "near_miss_name"), ("kind", "fail")}): 2,
        frozenset({("check", "repeated_message"), ("kind", "review")}): 1,
    }
    assert exported.histogram("minutehand.idle_wakes").sum == 1
    assert exported.histogram("minutehand.follow_ups_made").sum == 3
    assert exported.histogram("minutehand.run.sim_seconds").sum == 3 * 86400
    assert exported.histogram("minutehand.run.wall_seconds").sum == 42.5


def test_a_run_without_a_scorecard_still_records_its_length(exported: Exported) -> None:
    exported.telemetry.run_started("r1", SCENARIO)
    exported.telemetry.run_ended(record(0), None)
    assert exported.histogram("minutehand.run.wall_seconds").sum == 42.5
    assert exported._points("minutehand.follow_ups_made") == []  # pyright: ignore[reportPrivateUsage]


class _Collector(BaseHTTPRequestHandler):
    paths: list[str] = []

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"] or 0))
        type(self).paths.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def collector() -> Iterator[tuple[str, list[str]]]:
    _Collector.paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _Collector.paths
    server.shutdown()


def scripted_run(telemetry: OtelTelemetry) -> None:
    """A run end to end, then shutdown, so nothing is left to export once the collector stops."""
    telemetry.run_started("r1", SCENARIO)
    telemetry.wake_started(1, WakeReason.START, START)
    telemetry.recorded(event(1))
    telemetry.wake_ended(1)
    telemetry.found(finding([1]))
    telemetry.run_ended(record(1), None)
    telemetry.shutdown()


def test_the_factory_exports_over_otlp_when_the_endpoint_is_set(
    monkeypatch: pytest.MonkeyPatch, collector: tuple[str, list[str]]
) -> None:
    url, paths = collector
    monkeypatch.setenv(ENDPOINT_VARIABLE, url)
    scripted_run(from_environment())
    assert {"/v1/traces", "/v1/logs", "/v1/metrics"} <= set(paths)


def test_the_factory_exports_nothing_without_the_endpoint(
    monkeypatch: pytest.MonkeyPatch, collector: tuple[str, list[str]]
) -> None:
    url, paths = collector
    monkeypatch.delenv(ENDPOINT_VARIABLE, raising=False)
    # Were the factory to build exporters regardless, these would send them to the collector.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", f"{url}/v1/traces")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", f"{url}/v1/logs")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", f"{url}/v1/metrics")
    scripted_run(from_environment())
    assert paths == []


def test_a_captured_call_is_one_client_span_in_its_callers_trace_and_never_carries_a_body() -> None:
    """Even with bodies exported for world events: a captured call's bodies are another service's data."""
    exported = Exported(export_bodies=True)
    exported.telemetry.run_started("run-1", SCENARIO)
    exported.telemetry.wake_started(1, WakeReason.START, START)
    kept = Body(content_type="application/json", size=20, kept=BodyKept.WHOLE, sha256="0" * 64)
    call = RecordedCall(
        exchange=Exchange(
            method="POST",
            host="api.mail.test",
            path="/v3/mail/send?key=[redacted]",
            status=202,
            request_body='{"text": "the pricing"}',
            response_body='{"id": "queued"}',
            traceparent=TRACEPARENT,
            captured=Captured(
                mode=CaptureMode.REPLAY,
                declared_as="api.mail.test",
                answered_by=AnsweredBy.RECORDING,
                replayed_from="run abc",
                started=WALL,
                ended=WALL + timedelta(milliseconds=40),
                request=kept,
                response=kept,
            ),
        ),
        provider=None,
        first_seq=3,
        last_seq=2,
        wake=1,
        sim_time=START,
    )
    exported.telemetry.captured(call)
    span = exported.span("POST api.mail.test")
    attributes = dict(span.attributes or {})
    assert span.kind is SpanKind.CLIENT and span.context is not None and span.parent is not None
    assert (span.context.trace_id, span.parent.span_id) == (int(CALLER_TRACE, 16), int(CALLER_SPAN, 16))
    assert (span.end_time or 0) - (span.start_time or 0) == 40_000_000
    assert attributes["url.path"] == "/v3/mail/send" and attributes["minutehand.capture.mode"] == "replay"
    assert attributes["minutehand.capture.replayed_from"] == "run abc"
    assert not any("pricing" in str(v) or "queued" in str(v) for v in attributes.values())
