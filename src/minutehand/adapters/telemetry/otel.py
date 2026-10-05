"""The run as OpenTelemetry: spans for the run, its wakes and every world event, a log
record per finding, and the scorecard as metrics.

The SQLite store is the record; this is an export of it. Span timestamps for world
events are the events' own wall time, because backends reject or misplace future
timestamps; simulated time travels as `minutehand.sim_time` (ISO 8601) and
`minutehand.sim_time_unix_nano` on every span. The run and wake spans are given no
wall time by the port, so the SDK stamps them when they open and close.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from opentelemetry._logs import Logger, LoggerProvider, SeverityNumber
from opentelemetry.context import Context
from opentelemetry.metrics import MeterProvider
from opentelemetry.sdk._logs import LoggerProvider as SdkLoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import (
    INVALID_SPAN,
    Link,
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    Tracer,
    TracerProvider,
    get_current_span,
    set_span_in_context,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from minutehand.domain.agent import WakeReason
from minutehand.domain.checks import Effectiveness, Finding, Severity
from minutehand.domain.emulator import EmulatorChange, EmulatorHealth
from minutehand.domain.run import RunRecord
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, Operation, RecordedCall, WorldEvent

SCOPE = "minutehand"
ENDPOINT_VARIABLE = "OTEL_EXPORTER_OTLP_ENDPOINT"
BODIES_VARIABLE = "MINUTEHAND_EXPORT_BODIES"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NANOSECOND_PER_MICROSECOND = 1000
_PROPAGATOR = TraceContextTextMapPropagator()
_NOT_A_CHANGE = frozenset({Operation.READ, Operation.SEARCH})


_CHOSEN: ContextVar[str | None] = ContextVar("minutehand_chosen_span", default=None)
_CHOSEN_TRACE: ContextVar[str | None] = ContextVar("minutehand_chosen_trace", default=None)


class ChosenIds(IdGenerator):
    """Random ids, except the one a forwarded call's span must have: the id its emulator was already told is the
    parent of its own spans, before Minutehand's span of the call existed. Give it to the tracer provider for a
    forwarded call's span to join the emulator's spans in a backend."""

    def __init__(self) -> None:
        self._random = RandomIdGenerator()

    def generate_span_id(self) -> int:
        chosen = _CHOSEN.get()
        return int(chosen, 16) if chosen is not None else self._random.generate_span_id()

    def generate_trace_id(self) -> int:
        chosen = _CHOSEN_TRACE.get()
        return int(chosen, 16) if chosen is not None else self._random.generate_trace_id()


def unix_nano(moment: datetime) -> int:
    """Nanoseconds since the epoch, exact to the microsecond a datetime holds."""
    return (moment - _EPOCH) // timedelta(microseconds=1) * _NANOSECOND_PER_MICROSECOND


def _sim_time(moment: datetime) -> dict[str, str | int]:
    return {"minutehand.sim_time": moment.isoformat(), "minutehand.sim_time_unix_nano": unix_nano(moment)}


def _severity(severity: Severity) -> SeverityNumber:
    match severity:
        case Severity.ERROR:
            return SeverityNumber.ERROR
        case Severity.WARNING:
            return SeverityNumber.WARN
        case Severity.INFORMATION:
            return SeverityNumber.INFO


def _caller(traceparent: str | None) -> SpanContext | None:
    """The agent's span that made the call, when it sent a valid W3C traceparent."""
    if traceparent is None:
        return None
    caller = get_current_span(_PROPAGATOR.extract({"traceparent": traceparent})).get_span_context()
    return caller if caller.is_valid and caller.is_remote else None


class OtelTelemetry:
    """`ports.telemetry.Telemetry` over explicit providers, so a test can hand in in-memory ones."""

    def __init__(
        self,
        tracer_provider: TracerProvider,
        logger_provider: LoggerProvider,
        meter_provider: MeterProvider,
        *,
        export_bodies: bool = False,
    ) -> None:
        self._tracer_provider = tracer_provider
        self._logger_provider = logger_provider
        self._meter_provider = meter_provider
        self._tracer: Tracer = tracer_provider.get_tracer(SCOPE)
        self._logger: Logger = logger_provider.get_logger(SCOPE)
        self._export_bodies = export_bodies

        meter = meter_provider.get_meter(SCOPE)
        self._findings = meter.create_counter(
            "minutehand.findings", unit="{finding}", description="Findings by check and kind"
        )
        self._wakes = meter.create_counter(
            "minutehand.wakes", unit="{wake}", description="Wakes, by whether the agent changed the world in them"
        )
        self._time_lost = meter.create_histogram(
            "minutehand.time_lost_seconds", unit="s", description="Simulated time the agent itself added"
        )
        self._idle_wakes = meter.create_histogram(
            "minutehand.idle_wakes", unit="{wake}", description="Wakes in a run that changed nothing"
        )
        self._follow_ups_late = meter.create_histogram(
            "minutehand.follow_ups_late", unit="{follow_up}", description="Follow-ups made after their wait expired"
        )
        self._run_sim = meter.create_histogram(
            "minutehand.run.sim_seconds", unit="s", description="Simulated length of a run"
        )
        self._run_wall = meter.create_histogram(
            "minutehand.run.wall_seconds", unit="s", description="Real length of a run"
        )

        self._run: Span | None = None
        self._wake_spans: dict[int, Span] = {}
        self._changes: dict[int, int] = {}
        self._spans_by_seq: dict[int, SpanContext] = {}

    def run_started(self, run_id: str, scenario: Scenario) -> None:
        self._wake_spans.clear()
        self._changes.clear()
        self._spans_by_seq.clear()
        self._run = self._tracer.start_span(
            "minutehand.run",
            context=Context(),
            attributes={
                "minutehand.run_id": run_id,
                "minutehand.scenario": scenario.name,
                "minutehand.seed": scenario.seed,
                **_sim_time(scenario.starts_at),
            },
        )

    def wake_started(self, wake: int, reason: WakeReason, now: datetime) -> None:
        parent = set_span_in_context(self._run) if self._run is not None else Context()
        self._wake_spans[wake] = self._tracer.start_span(
            "minutehand.wake",
            context=parent,
            attributes={"minutehand.wake": wake, "minutehand.wake.reason": reason.value, **_sim_time(now)},
        )
        self._changes[wake] = 0

    def recorded(self, event: WorldEvent) -> None:
        wake_span = self._wake_spans.get(event.wake)
        caller = _caller(event.exchange.traceparent if event.exchange else None)
        links: list[Link] = []
        if caller is not None:
            parent = set_span_in_context(NonRecordingSpan(caller))
            if wake_span is not None:
                links.append(Link(wake_span.get_span_context()))
        elif wake_span is not None:
            parent = set_span_in_context(wake_span)
        elif self._run is not None:
            parent = set_span_in_context(self._run)
        else:
            parent = Context()

        attributes: dict[str, str | int] = {
            "minutehand.seq": event.seq,
            "minutehand.wake": event.wake,
            "minutehand.entity.id": event.entity.external_id,
            "minutehand.actor": event.actor.value,
            **_sim_time(event.sim_time),
        }
        exchange = event.exchange
        if exchange is not None:
            attributes["http.request.method"] = exchange.method
            attributes["server.address"] = exchange.host
            attributes["url.path"] = exchange.path
            attributes["http.response.status_code"] = exchange.status
            if self._export_bodies:
                if exchange.request_body is not None:
                    attributes["minutehand.request.body"] = exchange.request_body
                if exchange.response_body is not None:
                    attributes["minutehand.response.body"] = exchange.response_body

        at = unix_nano(event.wall_time)
        span = self._tracer.start_span(
            f"{event.entity.provider} {event.operation.value} {event.entity.kind.value}",
            context=parent,
            kind=SpanKind.SERVER,
            attributes=attributes,
            links=links,
            start_time=at,
        )
        if exchange is not None and exchange.status >= 500:
            span.set_status(Status(StatusCode.ERROR, f"the fake answered {exchange.status}"))
        span.end(end_time=at)
        self._spans_by_seq[event.seq] = span.get_span_context()

        if event.actor == Actor.AGENT and event.operation not in _NOT_A_CHANGE and event.wake in self._changes:
            self._changes[event.wake] += 1

    def captured(self, call: RecordedCall) -> None:
        """One span per captured call, `SpanKind.CLIENT`, at the real moments it began and ended; never its
        bodies, whatever `MINUTEHAND_EXPORT_BODIES` says: an outbound call's bodies are another service's data."""
        exchange = call.exchange
        captured = exchange.captured
        if captured is None:
            return
        caller = _caller(exchange.traceparent)
        wake_span = self._wake_spans.get(call.wake)
        links: list[Link] = []
        if caller is not None:
            parent = set_span_in_context(NonRecordingSpan(caller))
            if wake_span is not None:
                links.append(Link(wake_span.get_span_context()))
        elif wake_span is not None:
            parent = set_span_in_context(wake_span)
        elif self._run is not None:
            parent = set_span_in_context(self._run)
        else:
            parent = Context()
        attributes: dict[str, str | int] = {
            "http.request.method": exchange.method,
            "server.address": exchange.host,
            "url.path": exchange.path.split("?", 1)[0],
            "http.response.status_code": exchange.status,
            "minutehand.wake": call.wake,
            "minutehand.capture.mode": captured.mode.value,
            "minutehand.capture.answered_by": captured.answered_by.value,
            **_sim_time(call.sim_time),
        }
        if captured.declared_as is not None:
            attributes["minutehand.capture.declared_as"] = captured.declared_as
        if captured.replayed_from is not None:
            attributes["minutehand.capture.replayed_from"] = captured.replayed_from
        if captured.emulator is not None:
            attributes["minutehand.emulator"] = captured.emulator
        if captured.operation is not None:
            attributes["minutehand.operation"] = captured.operation
        if exchange.outcome is not None:
            attributes["minutehand.call.outcome"] = exchange.outcome.value
        forwarded = _caller(captured.forwarded_traceparent)
        if forwarded is not None:
            # The emulator was told this span is its parent, so it is this span's own id (`ChosenIds`); when the
            # agent sent no trace, the one begun for it.
            _CHOSEN.set(format(forwarded.span_id, "016x"))
            if caller is None:
                # The agent sent no trace: the span begins the one its emulator was handed, linked to its wake.
                _CHOSEN_TRACE.set(format(forwarded.trace_id, "032x"))
                parent = Context()
                links = [Link(wake_span.get_span_context())] if wake_span is not None else []
        span = self._tracer.start_span(
            f"{exchange.method} {exchange.host}",
            context=parent,
            kind=SpanKind.CLIENT,
            attributes=attributes,
            links=links,
            start_time=unix_nano(captured.started),
        )
        _CHOSEN.set(None)
        _CHOSEN_TRACE.set(None)
        if exchange.status >= 500:
            span.set_status(Status(StatusCode.ERROR, f"answered {exchange.status}"))
        span.end(end_time=unix_nano(captured.ended))

    def emulator_changed(self, change: EmulatorChange) -> None:
        """A span with no duration under the run, `minutehand.emulator <health>`, as the transition happens: its
        reason, never its log."""
        parent = set_span_in_context(self._run) if self._run is not None else Context()
        attributes: dict[str, str | int] = {
            "minutehand.emulator": change.emulator,
            "minutehand.emulator.health": change.health.value,
        }
        if change.reason is not None:
            attributes["minutehand.emulator.reason"] = change.reason
        span = self._tracer.start_span(
            f"minutehand.emulator {change.health.value}", context=parent, attributes=attributes
        )
        if change.health in (EmulatorHealth.UNHEALTHY, EmulatorHealth.DIED):
            span.set_status(Status(StatusCode.ERROR, change.reason or change.health.value))
        span.end()

    def wake_ended(self, wake: int) -> None:
        span = self._wake_spans.pop(wake, None)
        if span is not None:
            span.end()
        changed = self._changes.pop(wake, 0) > 0
        self._wakes.add(1, {"changed": changed})

    def found(self, finding: Finding) -> None:
        attributes: dict[str, str | int | list[int]] = {
            "minutehand.check": finding.check,
            "minutehand.finding.kind": finding.kind.value,
            "minutehand.evidence": list(finding.evidence),
        }
        if finding.pattern is not None:
            attributes["minutehand.pattern"] = finding.pattern
        if finding.at is not None:
            attributes.update(_sim_time(finding.at))
        if finding.wake is not None:
            attributes["minutehand.wake"] = finding.wake
        if finding.judged is not None:
            attributes["minutehand.judge.model"] = finding.judged.model
            attributes["minutehand.judge.prompt_version"] = finding.judged.prompt_version

        evidence = self._spans_by_seq.get(finding.evidence[0]) if finding.evidence else None
        context = set_span_in_context(NonRecordingSpan(evidence) if evidence is not None else INVALID_SPAN)
        self._logger.emit(
            context=context,
            severity_number=_severity(finding.severity),
            severity_text=finding.severity.value,
            body=finding.message,
            attributes=attributes,
            event_name="minutehand.finding",
        )
        self._findings.add(1, {"check": finding.check, "kind": finding.kind.value})

    def run_ended(self, record: RunRecord, effectiveness: Effectiveness | None) -> None:
        scenario = {"minutehand.scenario": record.scenario}
        self._run_sim.record((record.ended_at - record.started_at).total_seconds(), scenario)
        self._run_wall.record(record.wall_seconds, scenario)
        if effectiveness is not None:
            self._time_lost.record(effectiveness.time_lost.total_seconds(), scenario)
            self._idle_wakes.record(effectiveness.idle_wakes, scenario)
            self._follow_ups_late.record(effectiveness.follow_ups_late, scenario)
        for span in self._wake_spans.values():
            span.end()
        self._wake_spans.clear()
        if self._run is not None:
            self._run.set_attributes(
                {"minutehand.stop": record.stop.value, "minutehand.wall_seconds": record.wall_seconds}
            )
            self._run.end()
            self._run = None
        self.flush()

    def flush(self) -> None:
        """Hand everything buffered to the exporters. The run is complete once `run_ended` returns."""
        for provider in (self._tracer_provider, self._logger_provider, self._meter_provider):
            if isinstance(provider, SdkTracerProvider | SdkLoggerProvider | SdkMeterProvider):
                provider.force_flush()

    def shutdown(self) -> None:
        """Flush and close the exporters; the process is done with telemetry."""
        for provider in (self._tracer_provider, self._logger_provider, self._meter_provider):
            if isinstance(provider, SdkTracerProvider | SdkLoggerProvider | SdkMeterProvider):
                provider.shutdown()


def from_environment() -> OtelTelemetry:
    """OTLP over HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; otherwise providers with nowhere to send anything.

    Bodies are exported only when `MINUTEHAND_EXPORT_BODIES=1`.
    """
    resource = Resource.create({"service.name": SCOPE})
    tracer_provider = SdkTracerProvider(resource=resource, id_generator=ChosenIds())
    logger_provider = SdkLoggerProvider(resource=resource)
    export_bodies = os.environ.get(BODIES_VARIABLE) == "1"
    if not os.environ.get(ENDPOINT_VARIABLE):
        return OtelTelemetry(
            tracer_provider, logger_provider, SdkMeterProvider(resource=resource), export_bodies=export_bodies
        )

    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    meter_provider = SdkMeterProvider(
        resource=resource, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())]
    )
    return OtelTelemetry(tracer_provider, logger_provider, meter_provider, export_bodies=export_bodies)
