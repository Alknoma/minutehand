"""The OTLP/HTTP receiver, driven by the stock OpenTelemetry SDK in a process of its own, configured only by the
environment a run hands an agent; and passing what it takes on to where that environment pointed before."""

from __future__ import annotations

import asyncio
import gzip
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue, KeyValueList
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from minutehand import session
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.telemetry.forward import Forwarding
from minutehand.adapters.telemetry.receiver import Receiver
from minutehand.application.model_calls import model_call
from minutehand.application.run_clock import RunClock
from minutehand.domain.telemetry import (
    ArrayValue,
    BoolValue,
    DoubleValue,
    IntValue,
    Signal,
    SpanSource,
    SpanStatus,
    StringValue,
)
from tests.e2e.support import free_port
from tests.telemetry.collector import collector

START = datetime(2026, 8, 24, 10, 50, tzinfo=UTC)
EXPORTER = Path(__file__).with_name("exporting_agent.py")


@pytest.fixture
def clock() -> RunClock:
    return RunClock(START)


@pytest.fixture
def store(tmp_path: Path, clock: RunClock) -> SqliteStore:
    return SqliteStore(tmp_path / "world.db", "run", clock)


@pytest.fixture
async def receiver(store: SqliteStore, clock: RunClock) -> AsyncIterator[Receiver]:
    async with Receiver(store, clock) as running:
        yield running


def handed_out(port: int) -> dict[str, str]:
    """The environment a run hands its agent, over this process's own with any OTLP setting of its removed."""
    own = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    listen = session.Listen()
    return {**own, **session.agent_environment(listen, 1, "/nowhere.pem", {}, telemetry_port=port)}


async def export(mode: str, env: Mapping[str, str], *, clean: bool = True) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(EXPORTER),
        mode,
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, err.decode()
    if clean:
        assert b"Failed to export" not in err and b"Exception" not in err, err.decode()
    return out.decode().strip()


@pytest.mark.parametrize("mode", ["protobuf", "json"])
async def test_spans_a_real_sdk_exports_are_kept_with_the_run(
    mode: str, receiver: Receiver, store: SqliteStore, clock: RunClock
) -> None:
    clock.jump(START.replace(day=26))
    clock.begin_wake()
    env = handed_out(receiver.port)
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"http://127.0.0.1:{receiver.port}"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == f"http://127.0.0.1:{receiver.port}/v1/traces"

    trace_id = await export(mode, env)

    kept = store.spans(trace_id=trace_id)
    assert [s.span.name for s in kept] == ["chat model-luna", "plan the wake"]
    chat, root = (s.span for s in kept)
    assert chat.parent_span_id == root.span_id and root.parent_span_id is None
    assert {(s.wake, s.sim_time, s.source) for s in kept} == {(1, START.replace(day=26), SpanSource.RECEIVED)}
    assert chat.service_name == "exporting-agent"
    assert (chat.status, chat.status_message) == (SpanStatus.ERROR, "the model refused")
    assert chat.attribute("gen_ai.request.model") == StringValue(value="model-luna")
    assert chat.attribute("gen_ai.usage.input_tokens") == IntValue(value=41)
    assert chat.attribute("gen_ai.request.temperature") == DoubleValue(value=0.2)
    assert chat.attribute("flagged") == BoolValue(value=True)
    assert chat.attribute("gen_ai.request.stop_sequences") == ArrayValue(
        values=[StringValue(value="END"), StringValue(value="STOP")]
    )
    # Real times, as the SDK stamped them: this run's simulated clock is in August.
    assert chat.start.year >= 2026 and chat.start != START and chat.start <= chat.end


async def test_metrics_and_logs_are_acknowledged_and_not_kept(receiver: Receiver, store: SqliteStore) -> None:
    await export("metrics", handed_out(receiver.port))
    async with httpx.AsyncClient() as client:
        logs = await client.post(
            f"http://127.0.0.1:{receiver.port}/v1/logs", content=b"{}", headers={"content-type": "application/json"}
        )
    assert logs.status_code == 200 and logs.json() == {}
    assert store.spans() == []


async def test_a_malformed_payload_is_rejected_with_400_and_the_receiver_keeps_receiving(
    receiver: Receiver, store: SqliteStore
) -> None:
    url = f"http://127.0.0.1:{receiver.port}/v1/traces"
    async with httpx.AsyncClient() as client:
        garbage = await client.post(
            url, content=b"\xff\x00 not protobuf", headers={"content-type": "application/x-protobuf"}
        )
        bad_json = await client.post(url, content=b"[1, 2", headers={"content-type": "application/json"})
        bad_id = await client.post(
            url,
            content=b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"traceId":"zz","spanId":"00f067aa0ba902b7"}]}]}]}',
            headers={"content-type": "application/json"},
        )
        short_id = await client.post(
            url,
            content=b'{"resourceSpans":[{"scopeSpans":[{"spans":[{"traceId":"0af7","spanId":"00f067aa0ba902b7"}]}]}]}',
            headers={"content-type": "application/json"},
        )
        bad_gzip = await client.post(
            url, content=b"not gzip", headers={"content-type": "application/json", "content-encoding": "gzip"}
        )
        wrong_type = await client.post(url, content=b"{}", headers={"content-type": "text/plain"})
        good = await client.post(
            url,
            content=gzip.compress(ExportTraceServiceRequest().SerializeToString()),
            headers={"content-type": "application/x-protobuf", "content-encoding": "gzip"},
        )
    assert [r.status_code for r in (garbage, bad_json, bad_id, short_id, bad_gzip)] == [400] * 5
    assert wrong_type.status_code == 415
    assert good.status_code == 200 and good.headers["content-type"] == "application/x-protobuf"
    assert store.spans() == []


async def test_every_payload_is_passed_on_unchanged_with_the_original_headers(
    store: SqliteStore, clock: RunClock
) -> None:
    async with collector() as original:
        environ = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": original.url + "/",
            "OTEL_EXPORTER_OTLP_HEADERS": "x-api-key=k%3D1,x-team=minutes",
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": original.url + "/custom/metrics",
        }
        async with Receiver(store, clock, forwarding=Forwarding.from_environment(environ)) as receiver:
            await export("protobuf", handed_out(receiver.port))
            await export("json", handed_out(receiver.port))
            await export("metrics", handed_out(receiver.port))
        # Leaving the receiver waits for what it was still passing on.

    assert [c.path for c in original.collected] == ["/v1/traces", "/v1/traces", "/custom/metrics"]
    protobuf, as_json, metrics = original.collected
    assert all(c.header("x-api-key") == "k=1" and c.header("x-team") == "minutes" for c in original.collected)
    assert protobuf.header("content-type") == "application/x-protobuf"
    assert (as_json.header("content-type"), as_json.header("content-encoding")) == ("application/json", "gzip")
    # The body is the exporter's own, byte for byte: what was received is what arrives.
    request = ExportTraceServiceRequest()
    request.ParseFromString(protobuf.body)
    sent = [s.span_id.hex() for r in request.resource_spans for scope in r.scope_spans for s in scope.spans]
    kept = [s.span.span_id for s in store.spans()]
    assert sent == kept[:2]
    assert kept[2].encode() in gzip.decompress(as_json.body)
    assert metrics.body
    assert store.forward_failures() == []


async def test_a_dead_original_endpoint_is_recorded_and_the_agent_is_still_answered(
    store: SqliteStore, clock: RunClock
) -> None:
    async with collector() as gone:
        dead = gone.url
    environ = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": dead + "/v1/traces"}
    clock.begin_wake()
    async with Receiver(store, clock, forwarding=Forwarding.from_environment(environ)) as receiver:
        trace_id = await export("protobuf", handed_out(receiver.port))

    assert len(store.spans(trace_id=trace_id)) == 2
    [failure] = store.forward_failures()
    assert (failure.signal, failure.endpoint, failure.wake) == (Signal.TRACES, dead + "/v1/traces", 1)
    assert "ConnectError" in failure.reason


def test_the_original_destinations_are_read_as_opentelemetry_defines_them() -> None:
    found = Forwarding.from_environment(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example:4318",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://traces.example/ingest",
            "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20abc, x-one = 1",
            "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "x-one=2",
        }
    )
    traces, logs, metrics = (found.to(signal) for signal in Signal)
    assert traces is not None and traces.url == "https://traces.example/ingest"
    assert logs is not None and logs.url == "https://collector.example:4318/v1/logs"
    assert metrics is not None and metrics.url == "https://collector.example:4318/v1/metrics"
    assert dict(traces.headers) == {"authorization": "Bearer abc", "x-one": "1"}
    assert dict(logs.headers) == {"authorization": "Bearer abc", "x-one": "2"}
    assert Forwarding.from_environment({}).destinations == ()


async def test_a_destination_that_is_the_receiver_itself_is_not_forwarded_to(
    store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    port = free_port()
    environ = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://localhost:{port}"}
    async with Receiver(store, clock, port=port, forwarding=Forwarding.from_environment(environ)) as receiver:
        assert receiver.forwarding.destinations == ()
        await export("protobuf", handed_out(receiver.port))
    assert len(store.spans()) == 2


async def test_a_grpc_exporter_built_in_code_is_received_on_the_same_port_and_placed_in_its_wake(
    receiver: Receiver, store: SqliteStore, clock: RunClock
) -> None:
    """The stock gRPC exporter, constructed explicitly, reads the endpoint a run hands out and ignores its
    protocol: it opens HTTP/2 to the receiver's one port. Before, that was answered by an HTTP/1.1 server and
    nothing was kept."""
    clock.jump(START.replace(day=26))
    clock.begin_wake()

    trace_id = await export("grpc", handed_out(receiver.port))

    kept = store.spans(trace_id=trace_id)
    assert [s.span.name for s in kept] == ["chat model-luna", "plan the wake"]
    assert {(s.wake, s.source) for s in kept} == {(1, SpanSource.RECEIVED)}
    assert kept[0].span.attribute("gen_ai.request.model") == StringValue(value="model-luna")
    assert receiver.notices == []


async def test_a_grpc_exporter_without_grpc_installed_is_said_once_and_not_left_silent(
    store: SqliteStore, clock: RunClock
) -> None:
    async with Receiver(store, clock, grpc=False) as without:
        refused = {**handed_out(without.port), "EXPORT_GIVE_UP_SECONDS": "1"}
        await export("grpc", refused, clean=False)
        await export("grpc", refused, clean=False)
        notices = without.notices

    assert len(notices) == 1 and "install `minutehand[grpc]`" in notices[0], notices
    assert store.spans() == []


async def test_a_genai_log_event_is_kept_as_a_child_of_its_span_and_shows_what_the_model_was_asked(
    receiver: Receiver, store: SqliteStore
) -> None:
    """An instrumentation on the older GenAI event conventions puts the prompt and the answer in log records, and
    none on the span: the call's evidence reads them from there. A log record of anything else is dropped."""
    trace_id, call = bytes.fromhex("5" * 32), bytes.fromhex("6" * 16)
    span = Span(
        trace_id=trace_id,
        span_id=call,
        name="chat model-luna",
        start_time_unix_nano=1_790_000_000_000_000_000,
        end_time_unix_nano=1_790_000_001_000_000_000,
        attributes=[KeyValue(key="gen_ai.operation.name", value=AnyValue(string_value="chat"))],
    )
    traces = ExportTraceServiceRequest(resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[span])])])

    def event(name: str, body: str) -> LogRecord:
        return LogRecord(
            time_unix_nano=1_790_000_000_500_000_000,
            trace_id=trace_id,
            span_id=call,
            event_name=name,
            body=AnyValue(
                kvlist_value=KeyValueList(values=[KeyValue(key="content", value=AnyValue(string_value=body))])
            ),
            attributes=[KeyValue(key="gen_ai.system", value=AnyValue(string_value="openai"))],
        )

    logs = ExportLogsServiceRequest(
        resource_logs=[
            ResourceLogs(
                scope_logs=[
                    ScopeLogs(
                        log_records=[
                            event("gen_ai.user.message", "Is Lakeside Hall free on Friday?"),
                            event("gen_ai.choice", "Ask Rosa."),
                            LogRecord(time_unix_nano=1, trace_id=trace_id, span_id=call, event_name="http.request"),
                        ]
                    )
                ]
            )
        ]
    )
    async with httpx.AsyncClient() as client:
        for path, message in (("traces", traces), ("logs", logs)):
            answered = await client.post(
                f"http://127.0.0.1:{receiver.port}/v1/{path}",
                content=message.SerializeToString(),
                headers={"content-type": "application/x-protobuf"},
            )
            assert answered.status_code == 200

    kept = store.spans(trace_id=trace_id.hex())
    logged = [s for s in kept if s.source is SpanSource.LOG]
    assert [s.span.name for s in logged] == ["gen_ai.user.message", "gen_ai.choice"]
    assert all(s.span.parent_span_id == call.hex() for s in logged)
    found = model_call(next(s for s in kept if s.source is SpanSource.RECEIVED), kept)
    assert found.input_messages is not None and "Is Lakeside Hall free on Friday?" in found.input_messages
    assert found.output_messages is not None and "Ask Rosa." in found.output_messages


def test_receiving_loads_no_grpc_until_a_grpc_exporter_connects(tmp_path: Path) -> None:
    """Once gRPC is loaded, its fork handlers write to stderr at every fork, and block when nobody reads it: a
    60-day run hung at wake 93 starting a hook. A run whose agent exports over HTTP never loads it."""
    script = """
import asyncio, sys
from datetime import UTC, datetime
from pathlib import Path
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.telemetry.receiver import Receiver
from minutehand.application.run_clock import RunClock

async def main() -> None:
    clock = RunClock(datetime(2026, 8, 24, tzinfo=UTC))
    async with Receiver(SqliteStore(Path(sys.argv[1]), "run", clock), clock):
        pass
    print("grpc" in sys.modules)

asyncio.run(main())
"""
    done = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "world.db")], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False"
