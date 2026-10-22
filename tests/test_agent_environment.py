"""The agent's environment is a declared part of the agent (docs/agent-environment.md): inherited by default, read
from dotenv files, or an allowlist; Minutehand's own credentials never in it; no value anywhere Minutehand writes; a
value the agent sends redacted in the record; a fork's sources re-read at its start.

The agent here is a small program started by Minutehand itself (`session._agent_process`, as `play` and `fork` start
one): it writes the environment and argv it was started with to a file, sends a declared secret to the Slack fake
through the proxy in a header, the query string and the body, then listens as the agent file says it does. The
sandbox is a stand-in: two commands that print an idle sandbox at the run's start and accept a step of the clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from minutehand import session
from minutehand.adapters.proxy import redact
from minutehand.adapters.telemetry.otel import OtelTelemetry
from minutehand.application.checkpoint import Remembered
from minutehand.application.environment import ENVIRONMENT_RECORD, EnvironmentRecord, EnvironmentRefused, parse_dotenv
from minutehand.application.refusals import RunRefused
from minutehand.cli import main
from minutehand.domain.agent import (
    AgentEnvironment,
    AgentUnderTest,
    EnvironmentFiles,
    EnvironmentSourceKind,
    GoalByMessage,
    InheritedEnvironment,
    OnlyFromInherited,
    Sandbox,
)
from minutehand.domain.experiment import Fork
from minutehand.domain.people import InboundTarget
from minutehand.domain.run import StopReason
from minutehand.domain.scenario import Person, Scenario, Silent
from minutehand.domain.world import Header
from tests.ports import free_port
from tests.support import stored
from tests.support.read_model import rows

T0 = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
SECRET = "env-file-secret-7f3a9c1d2e-never-in-the-record"
OWN_KEY = "minutehands-own-model-key-4b2c9d-never-handed-over"
MACHINE = "machine-only-value-5d6e7f"

AGENT = """
import http.server, json, os, ssl, sys, time, urllib.error, urllib.request
out = sys.argv[1]
record = {"argv": sys.argv, "env": dict(os.environ)}
named = os.environ.get("MINUTEHAND_AGENT_ENVIRONMENT")
if named:
    record["file_mode"] = oct(os.stat(named).st_mode & 0o777)
    record["file_text"] = open(named, encoding="utf-8").read()
if "HTTPS_PROXY" in os.environ and "AGENT_SECRET" in os.environ:
    secret = os.environ["AGENT_SECRET"]
    context = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    body = json.dumps({"channel": "C1", "text": "the key is " + secret, "note": secret}).encode()
    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage?echo=" + secret,
        data=body,
        method="POST",
        headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json", "X-Note": secret},
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=30) as answer:
            record["slack"] = answer.status
    except urllib.error.HTTPError as e:
        record["slack"] = e.code
with open(out, "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\\n")
port = int(os.environ.get("AGENT_PORT", "0"))
if port:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        def log_message(self, *args):
            pass
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
while True:
    time.sleep(1)
"""


def _sandbox(starts_at: datetime) -> Sandbox:
    """A sandbox stand-in: idle at the run's start with no deadline, and a step of the clock that does nothing."""
    said = json.dumps({"idle": True, "now_ns": int(starts_at.timestamp() * 1_000_000_000), "earliest_ns": -1})
    return Sandbox(
        deadlines=[sys.executable, "-c", f"print({json.dumps(said)})"],
        advance=[sys.executable, "-c", "pass", "{nanoseconds}"],
    )


def _scenario() -> Scenario:
    return Scenario(
        name="environment",
        goal="Nothing: the agent's environment is what is tested.",
        owner="owen",
        starts_at=T0,
        people=[Person(key="owen", name="Owen", email="owen@example.com", reply=Silent())],
    )


def _files(*paths: Path) -> AgentEnvironment:
    return AgentEnvironment(sources=[EnvironmentFiles(paths=[str(p) for p in paths])])


class _Exported:
    """Minutehand's own telemetry, read back from in-memory exporters, as text a secret can be searched in."""

    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.logs = InMemoryLogRecordExporter()
        self.metrics = InMemoryMetricReader()
        tracer = TracerProvider()
        tracer.add_span_processor(SimpleSpanProcessor(self.spans))
        logger = LoggerProvider()
        logger.add_log_record_processor(SimpleLogRecordProcessor(self.logs))
        self.telemetry = OtelTelemetry(tracer, logger, MeterProvider(metric_readers=[self.metrics]))

    def text(self) -> str:
        spans = [s.to_json() for s in self.spans.get_finished_spans()]
        logs = [f"{r.log_record.body} {dict(r.log_record.attributes or {})}" for r in self.logs.get_finished_logs()]
        return "\n".join([*spans, *logs, str(self.metrics.get_metrics_data())])


async def _until(path: Path) -> None:
    for _ in range(600):
        if path.is_file() and path.read_text(encoding="utf-8").endswith("\n"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"the agent never wrote {path}")


def _last(path: Path) -> dict[str, object]:
    found = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert isinstance(found, dict)
    return found


def _env_of(seen: dict[str, object]) -> dict[str, str]:
    env = seen["env"]
    assert isinstance(env, dict)
    return {str(k): str(v) for k, v in env.items()}


async def _started(tmp_path: Path, agent: AgentUnderTest, own: dict[str, str] | None = None) -> dict[str, object]:
    """The agent started as a run starts it, with `own` as Minutehand's own variables; what it saw."""
    script, out = tmp_path / "agent.py", tmp_path / "seen.jsonl"
    script.write_text(AGENT, encoding="utf-8")
    async with session._agent_process(  # pyright: ignore[reportPrivateUsage]
        [sys.executable, str(script), str(out)], own or {}, agent, tmp_path / "agent.log", state=tmp_path / "state"
    ) as program:
        assert program is not None
        await _until(out)
    return _last(out)


def _record(tmp_path: Path) -> EnvironmentRecord:
    return EnvironmentRecord.model_validate_json((tmp_path / ENVIRONMENT_RECORD).read_text(encoding="utf-8"))


# -- the three sources ------------------------------------------------------------------------------------------


async def test_with_no_environment_block_the_agent_sees_the_inherited_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INHERITED_SENTINEL", MACHINE)
    agent = AgentUnderTest(name="a", sandbox=_sandbox(T0))
    assert agent.environment == AgentEnvironment(sources=[InheritedEnvironment()])

    seen = await _started(tmp_path, agent)

    assert _env_of(seen)["INHERITED_SENTINEL"] == MACHINE
    record = _record(tmp_path)
    assert [s.kind for s in record.sources] == [EnvironmentSourceKind.INHERIT]
    assert "INHERITED_SENTINEL" in record.sources[0].supplied


async def test_files_supplies_a_value_minutehands_own_process_never_held(tmp_path: Path) -> None:
    assert SECRET not in os.environ.values()
    env_file = tmp_path / "agent.env"
    env_file.write_text(f"# the agent's own\nexport AGENT_SECRET='{SECRET}'\nAGENT_FLAG=\"on\"  # inline\n")

    seen = await _started(tmp_path, AgentUnderTest(name="a", sandbox=_sandbox(T0), environment=_files(env_file)))

    env = _env_of(seen)
    assert env["AGENT_SECRET"] == SECRET and env["AGENT_FLAG"] == "on"
    assert "PATH" not in env, "a files source alone hands the agent nothing of the machine's"
    [source] = _record(tmp_path).sources
    assert source.kind is EnvironmentSourceKind.FILES and source.path == str(env_file)
    assert source.supplied == ["AGENT_SECRET", "AGENT_FLAG"]
    assert SECRET not in (tmp_path / ENVIRONMENT_RECORD).read_text(encoding="utf-8")


async def test_only_withholds_everything_not_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEPT_SENTINEL", "kept-by-the-allowlist")
    monkeypatch.setenv("DROPPED_SENTINEL", MACHINE)
    agent = AgentUnderTest(
        name="a",
        sandbox=_sandbox(T0),
        environment=AgentEnvironment(sources=[OnlyFromInherited(names=["KEPT_SENTINEL", "NEVER_SET_SENTINEL"])]),
    )

    seen = await _started(tmp_path, agent)

    env = _env_of(seen)
    # the interpreter adds two of its own on macOS (Python's locale coercion, CoreFoundation's encoding marker)
    assert set(env) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} == {"KEPT_SENTINEL", "MINUTEHAND_AGENT_ENVIRONMENT"}
    assert env["KEPT_SENTINEL"] == "kept-by-the-allowlist"
    [source] = _record(tmp_path).sources
    assert source.kind is EnvironmentSourceKind.ONLY and source.supplied == ["KEPT_SENTINEL"]


async def test_a_later_source_overrides_an_earlier_one_and_minutehands_own_variables_apply_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHARED_SENTINEL", "from-the-machine")
    env_file = tmp_path / "agent.env"
    env_file.write_text("SHARED_SENTINEL=from-the-file\nHTTPS_PROXY=http://hijacked.example:1\n")
    agent = AgentUnderTest(
        name="a",
        sandbox=_sandbox(T0),
        environment=AgentEnvironment(sources=[InheritedEnvironment(), EnvironmentFiles(paths=[str(env_file)])]),
    )

    seen = await _started(tmp_path, agent, own={"HTTPS_PROXY": "http://127.0.0.1:1"})

    env = _env_of(seen)
    assert env["SHARED_SENTINEL"] == "from-the-file" and env["HTTPS_PROXY"] == "http://127.0.0.1:1"
    assert "HTTPS_PROXY" in _record(tmp_path).own


@pytest.mark.parametrize("kind", list(EnvironmentSourceKind))
async def test_minutehands_own_model_key_never_reaches_the_agent_under_any_source_and_an_allowlist_naming_it_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: EnvironmentSourceKind
) -> None:
    monkeypatch.setenv("MINUTEHAND_MODEL_API_KEY", OWN_KEY)
    env_file = tmp_path / "agent.env"
    env_file.write_text(f"MINUTEHAND_MODEL_API_KEY={OWN_KEY}\nAGENT_FLAG=on\n")
    if kind is EnvironmentSourceKind.ONLY:
        agent = AgentUnderTest(
            name="a",
            sandbox=_sandbox(T0),
            environment=AgentEnvironment(sources=[OnlyFromInherited(names=["MINUTEHAND_MODEL_API_KEY"])]),
        )
        with pytest.raises(RunRefused, match="MINUTEHAND_MODEL_API_KEY by name, which is Minutehand's own"):
            await _started(tmp_path, agent)
        return
    source = (
        InheritedEnvironment() if kind is EnvironmentSourceKind.INHERIT else EnvironmentFiles(paths=[str(env_file)])
    )
    agent = AgentUnderTest(name="a", sandbox=_sandbox(T0), environment=AgentEnvironment(sources=[source]))

    seen = await _started(tmp_path, agent)

    assert "MINUTEHAND_MODEL_API_KEY" not in _env_of(seen)
    assert OWN_KEY not in str(seen["file_text"])
    assert _record(tmp_path).withheld == ["MINUTEHAND_MODEL_API_KEY"]


async def test_a_missing_file_is_refused_at_the_start_naming_it(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere.env"
    agent = AgentUnderTest(name="a", sandbox=_sandbox(T0), environment=_files(missing))

    with pytest.raises(EnvironmentRefused, match=re.escape(str(missing)) + ", which does not exist"):
        await _started(tmp_path, agent)
    assert not (tmp_path / "seen.jsonl").exists(), "the agent was never started"


# -- a whole run, and a fork of it -------------------------------------------------------------------------------


async def test_a_run_hands_the_agent_its_file_keeps_no_value_anywhere_and_a_fork_reads_the_file_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MINUTEHAND_MODEL_API_KEY", OWN_KEY)
    monkeypatch.setenv("INHERITED_SENTINEL", MACHINE)
    port = free_port()
    env_file = tmp_path / "agent.env"
    env_file.write_text(f"AGENT_SECRET={SECRET}\nAGENT_FLAG=first\nAGENT_PORT={port}\n")
    script, out = tmp_path / "agent.py", tmp_path / "seen.jsonl"
    script.write_text(AGENT, encoding="utf-8")
    command = [sys.executable, str(script), str(out)]
    agent = AgentUnderTest(
        name="environment",
        sandbox=_sandbox(T0),
        goal=GoalByMessage(provider="slack"),
        inbound=[InboundTarget(provider="slack", url=f"http://127.0.0.1:{port}/slack/events")],
        environment=_files(env_file),
    )
    exported = _Exported()
    state = tmp_path / "state"

    with caplog.at_level(logging.DEBUG):
        [outcome] = await session.play(_scenario(), agent, state=state, command=command, telemetry=exported.telemetry)

    run_id = outcome.record.run_id
    assert outcome.record.stop is StopReason.NOTHING_PENDING, outcome.record.failure
    seen = _last(out)
    env = _env_of(seen)
    assert env["AGENT_SECRET"] == SECRET and SECRET not in os.environ.values()
    assert "INHERITED_SENTINEL" not in env and "MINUTEHAND_MODEL_API_KEY" not in env
    assert env["HTTPS_PROXY"].startswith("http://127.0.0.1:") and env["MINUTEHAND_ON"] == "1"
    assert seen["file_mode"] == "0o600" and f"export AGENT_SECRET={SECRET}\n" in str(seen["file_text"])
    assert not Path(env["MINUTEHAND_AGENT_ENVIRONMENT"]).exists(), "removed when the run ended"
    assert seen["slack"] == 200

    # the call is in the record; the secret the agent sent in its path, header and body is not
    with session.reading(state, run_id) as world:
        [call] = [c for c in world.calls() if c.exchange.host == "slack.com"]
    assert call.exchange.request_body is not None and call.exchange.request_body.count(redact.REDACTED) == 2
    assert call.exchange.path == "/api/chat.postMessage?echo=" + redact.REDACTED
    everything = stored.everything(state)
    assert SECRET.encode() not in everything and OWN_KEY.encode() not in everything
    assert SECRET not in caplog.text and SECRET not in exported.text()
    program = session.program_record(state, run_id)
    argv = seen["argv"]
    assert program is not None and isinstance(argv, list)
    assert SECRET not in " ".join(program.command) and SECRET not in " ".join(str(a) for a in argv)

    # the record and the read model keep the declaration and the names
    record = session.environment_record(state, run_id)
    assert record is not None and [s.kind for s in record.sources] == [EnvironmentSourceKind.FILES]
    assert record.sources[0].supplied == ["AGENT_SECRET", "AGENT_FLAG", "AGENT_PORT"]
    assert {"HTTPS_PROXY", "SSL_CERT_FILE", "MINUTEHAND_AGENT_URL", "MINUTEHAND_AGENT_ENVIRONMENT"} <= set(record.own)
    listed = rows(state, run_id, "SELECT kind, source, names FROM environment ORDER BY position")
    assert [r["kind"] for r in listed] == ["files", "minutehand", "withheld"]
    assert listed[0]["source"] == str(env_file) and json.loads(str(listed[0]["names"])) == record.sources[0].supplied
    assert SECRET not in json.dumps(rows(state, run_id, "SELECT * FROM calls"))

    # a fork starts the agent with the same declaration, its file read again
    env_file.write_text(f"AGENT_SECRET={SECRET}\nAGENT_FLAG=second\nAGENT_PORT={port}\n")
    point = next(p for p in session.fork_points(state, run_id) if p.wake == 1 and isinstance(p.agent, Remembered))
    [forked] = await session.fork(run_id, Fork(parent_run=run_id, at_seq=point.seq), state=state, command=command)
    restored = session.restore_of(state, forked.record.run_id)
    assert restored is not None and restored.verified and restored.replayed_calls == 1
    assert _env_of(_last(out))["AGENT_FLAG"] == "second"
    assert SECRET.encode() not in stored.everything(state)

    # and refuses a source that has gone missing, naming it
    env_file.unlink()
    with pytest.raises(RunRefused, match=re.escape(str(env_file))):
        await session.fork(run_id, Fork(parent_run=run_id, at_seq=point.seq), state=state, command=command)


# -- dotenv, the Compose way ---------------------------------------------------------------------------------------


def test_a_dotenv_file_is_read_as_compose_reads_one() -> None:
    text = (
        "# a comment\n"
        "\n"
        "PLAIN=value with spaces   \n"
        "export EXPORTED=yes\n"
        "INLINE=before # a comment\n"
        "HASH_KEPT=a#b\n"
        "SINGLE='kept $literally \"as is\" # not a comment'\n"
        'DOUBLE="line\\nbreak \\"quoted\\" tab\\t"\n'
        "EMPTY=\n"
        "LAST=1\n"
        "LAST=2\n"
    )
    assert parse_dotenv(text, where="x.env") == {
        "PLAIN": "value with spaces",
        "EXPORTED": "yes",
        "INLINE": "before",
        "HASH_KEPT": "a#b",
        "SINGLE": 'kept $literally "as is" # not a comment',
        "DOUBLE": 'line\nbreak "quoted" tab\t',
        "EMPTY": "",
        "LAST": "2",
    }


@pytest.mark.parametrize(
    ("line", "said"),
    [
        ("NOT A LINE", "line 1: not `NAME=value`"),
        ("1BAD=x", "line 1: not `NAME=value`"),
        ("OPEN='never closed", "a single quote is opened and never closed"),
        ('OPEN="never closed', "a double quote is opened and never closed"),
        ("KEY=${OTHER}", "KEY asks for interpolation"),
    ],
)
def test_a_dotenv_line_compose_would_refuse_is_refused_naming_the_file_and_line(line: str, said: str) -> None:
    with pytest.raises(EnvironmentRefused, match=re.escape(said)):
        parse_dotenv(line + "\n", where="x.env")


# -- the record's redaction ---------------------------------------------------------------------------------------


def test_a_declared_value_is_redacted_wherever_it_stands_and_a_short_one_is_not() -> None:
    secrets = redact.Secrets(["long-enough-secret", "8740", "true"])
    assert len(secrets) == 1

    body = redact.body(
        '{"text": "key long-enough-secret here", "port": "8740", "nested": {"v": "long-enough-secret"}}',
        "application/json",
        secrets=secrets,
    )
    assert body == '{"text": "key [redacted] here", "port": "8740", "nested": {"v": "[redacted]"}}'
    assert redact.body("plain long-enough-secret text", "text/plain", secrets=secrets) == "plain [redacted] text"
    assert redact.path("/v1/items?q=long-enough-secret&n=8740", secrets=secrets) == "/v1/items?q=[redacted]&n=8740"
    assert redact.headers([("x-note", "long-enough-secret"), ("x-port", "8740")], secrets=secrets) == [
        Header(name="x-note", value="[redacted]"),
        Header(name="x-port", value="8740"),
    ]
    text, raw = redact.kept(b"\xff\xfelong-enough-secret", "application/octet-stream", secrets=secrets)
    assert text is None and raw == b"\xff\xfe[redacted]"
    assert "long-enough-secret" not in repr(secrets)


# -- the other commands -------------------------------------------------------------------------------------------


def _agent_file(tmp_path: Path, environment: dict[str, object]) -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "a",
                "sandbox": {
                    "deadlines": ["runsc", "debug", "--deadlines", "a"],
                    "advance": ["runsc", "debug", "--advance-clock={nanoseconds}ns", "a"],
                },
                "environment": environment,
            }
        )
    )
    return path


def test_env_maps_files_to_compose_env_file_and_refuses_what_it_cannot_hand_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "agent.env").write_text("AGENT_SECRET=x\n")
    files = _agent_file(tmp_path, {"sources": [{"kind": "inherit"}, {"kind": "files", "paths": ["agent.env"]}]})
    fixed = ["--state", str(tmp_path / "state"), "--proxy-port", "38080", "--telemetry-port", "38081"]

    assert main(["env", "--agent", str(files), *fixed, "--format", "compose", "--service", "agent"]) == 0
    override = yaml.safe_load(capsys.readouterr().out)
    service = override["services"]["agent"]
    assert service["env_file"] == [str((tmp_path / "agent.env").resolve())]
    assert "HTTPS_PROXY" in service["environment"] and "AGENT_SECRET" not in str(override)

    assert main(["env", "--agent", str(files), *fixed]) == 2
    said = capsys.readouterr().err
    assert "agent.env, which only a command Minutehand starts is handed" in said and "--format compose" in said

    only = _agent_file(tmp_path, {"sources": [{"kind": "only", "names": ["HOME"]}]})
    assert main(["env", "--agent", str(only), *fixed, "--format", "compose", "--service", "agent"]) == 2
    assert "allowlist of Minutehand's (HOME)" in capsys.readouterr().err


def test_a_relative_file_is_read_from_the_agent_files_folder(tmp_path: Path) -> None:
    from minutehand.application.files import load_agent

    (tmp_path / "secrets").mkdir()
    agent = load_agent(_agent_file(tmp_path, {"sources": [{"kind": "files", "paths": ["secrets/agent.env"]}]}))
    [source] = agent.environment.sources
    assert isinstance(source, EnvironmentFiles) and source.paths == [str(tmp_path / "secrets" / "agent.env")]
