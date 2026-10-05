"""Hosts no provider claims, through whole runs of a real agent process: an email it sends counts as a message to
a person, a lookup it makes reaches the real host, the run says what it called, a first run with
`--capture-unknown` says what to declare, and a fork replays its parent's lookups by default."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from minutehand import session
from minutehand.adapters.proxy.capture import RECORDINGS
from minutehand.adapters.telemetry.otel import OtelTelemetry
from minutehand.checks.ledger import build
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.checks import FindingKind, ObligationKind
from minutehand.domain.experiment import Fork
from minutehand.domain.outbound import Acknowledge, Answer, HtmlAt, InForks, MessageReading, PassThrough
from minutehand.domain.scenario import PersonAsked, Scenario, Silent
from minutehand.domain.world import Actor, AnsweredBy, CaptureMode, MessageSnapshot
from tests.capture.support import stored_bytes
from tests.e2e.support import OWNER, agent_under_test, answers, scenario, world
from tests.proxy.upstream import Authority, make_authority, model_api

MAIL_HOST = "api.mail.test"
MAILED = "I have asked Sofia to confirm the partner pricing and will report back."
READING = MessageReading(
    recipients=["personalizations[*].to[*].email"], text=["text", HtmlAt(html="content[0].value")], subject=["subject"]
)
MINUTEHAND = Path(sys.executable).parent / "minutehand"


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return make_authority(tmp_path / "upstream-ca")


def _declaring(agent: AgentUnderTest, *, in_forks: InForks = InForks.REPLAY) -> AgentUnderTest:
    outbound = [
        Acknowledge(
            host=MAIL_HOST, name="mail", answer=Answer(status=202, json_body={"id": "queued"}), message=READING
        ),
        PassThrough(host="::1", name="search", in_forks=in_forks),
    ]
    return agent.model_copy(update={"outbound": outbound})


def _reporting(base: Scenario) -> Scenario:
    return base.model_copy(update={"expect": [PersonAsked(person="owner", mentions=["report back"])]})


async def test_an_email_the_agent_sends_is_a_message_to_the_owner_and_its_lookup_reaches_the_real_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authority: Authority
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    telemetry = OtelTelemetry(tracer_provider, LoggerProvider(), MeterProvider(metric_readers=[InMemoryMetricReader()]))
    state = tmp_path / "state"
    async with model_api(authority, host="::1") as real:
        monkeypatch.setenv("MAIL_URL", f"https://{MAIL_HOST}/v3/mail/send")
        monkeypatch.setenv("MAIL_TO", OWNER)
        monkeypatch.setenv("LOOKUP_URL", f"https://[::1]:{real.port}/search?q=partner+pricing")
        [outcome] = await session.play(
            _reporting(scenario(Silent())),
            _declaring(launched.agent),
            state=state,
            command=launched.command,
            telemetry=telemetry,
            listen=session.Listen(upstream_ca=authority.ca_cert, receive_telemetry=False),
        )

    assert [r.path for r in real.received] == ["/search?q=partner+pricing"]
    assert launched.state()["mailed"] == 202 and launched.state()["looked_up"] == ['{"received": 1}']
    run_id = outcome.record.run_id
    # The email met the expectation the way a chat message would, quoted in the finding.
    [met] = [f for f in outcome.result.findings if f.check == "expectations"]
    assert met.kind is FindingKind.INFORMATIONAL and f"“Partner pricing {MAILED}”" in met.message
    events = world(state, run_id).events()
    [mail] = [e for e in events if e.entity.provider == "mail"]
    assert isinstance(mail.after, MessageSnapshot) and mail.actor is Actor.AGENT and mail.wake == 1
    assert mail.after.recipient_emails == [OWNER] and not mail.after.answerable
    # The owner is silent, and a silent person's every message is a wait: but not one nobody could answer.
    waits = build(_reporting(scenario(Silent())), events, [])
    assert [w.person for w in waits if w.kind is ObligationKind.ANSWER_FROM_PERSON] == ["sofia"]
    assert outcome.result.effectiveness.messages_to_people == 2
    # What the run says it called, besides Slack.
    assert [(u.host, u.mode, u.calls) for u in outcome.record.outbound] == [
        ("::1", CaptureMode.PASS_THROUGH, 1),
        (MAIL_HOST, CaptureMode.ACKNOWLEDGE, 1),
    ]
    # The keys the agent sent the email API with never reached the run's directory.
    kept = stored_bytes(session.run_dir(state, run_id))
    assert b"sg-key-in-header" not in kept and b"sg-key-in-body" not in kept and MAILED.encode() in kept
    assert (session.run_dir(state, run_id) / RECORDINGS).is_file()
    # One client span per captured call, with no body on it.
    client = [s for s in spans.get_finished_spans() if s.kind is SpanKind.CLIENT]
    assert sorted(s.name for s in client) == ["GET ::1", f"POST {MAIL_HOST}"]
    for span in client:
        attributes = dict(span.attributes or {})
        assert attributes["minutehand.wake"] == 1 and "minutehand.request.body" not in attributes
        assert not any(MAILED in str(v) for v in attributes.values())


async def test_a_first_run_with_capture_unknown_says_what_the_agent_called_and_what_to_declare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authority: Authority
) -> None:
    """Through the command: an agent that declares nothing, run once to see what it calls."""
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text(yaml.safe_dump(scenario(Silent()).model_dump(mode="json")))
    agent_file = tmp_path / "agent.yaml"
    agent_file.write_text(yaml.safe_dump(launched.agent.model_dump(mode="json")))
    state = tmp_path / "state"
    async with model_api(authority, host="::1") as real:
        env = dict(os.environ) | {"LOOKUP_URL": f"https://[::1]:{real.port}/search?q=x"}
        command = [str(MINUTEHAND), "run", str(scenario_file), "--agent", str(agent_file), "--state", str(state)]
        command += ["--capture-unknown", "--upstream-ca", str(authority.ca_cert), "--", *launched.command]
        ran = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, env=env, timeout=90)
        heard = len(real.received)
    out = ran.stdout
    assert ran.returncode == 1, ran.stderr
    assert heard == 1
    assert "\noutbound calls\n  ::1: 1 call, passed through, undeclared (--capture-unknown)\n" in out
    suggestion = out[out.index("to capture the hosts nobody declared") :]
    assert "  outbound:\n  - host: ::1\n    kind: pass_through\n" in suggestion


@pytest.mark.parametrize("in_forks", [InForks.REPLAY, InForks.PASS_THROUGH])
async def test_a_fork_replays_its_parents_lookups_by_default_and_calls_the_real_host_when_told_to(
    in_forks: InForks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authority: Authority
) -> None:
    """The agent looks up once when it takes the goal and again on Sofia's answer. A fork after the first wake
    shares the first lookup and makes the second itself."""
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", hooks=True)
    agent = _declaring(launched.agent, in_forks=in_forks)
    state = tmp_path / "state"
    async with model_api(authority, host="::1") as real:
        monkeypatch.setenv("LOOKUP_URL", f"https://[::1]:{real.port}/search?q=partner+pricing")
        listen = session.Listen(upstream_ca=authority.ca_cert, receive_telemetry=False)
        [parent] = await session.play(
            scenario(answers(after=timedelta(hours=36))), agent, state=state, command=launched.command, listen=listen
        )
        assert launched.state()["looked_up"] == ['{"received": 1}', '{"received": 2}']
        after_first_wake = next(p for p in session.fork_points(state, parent.record.run_id) if p.wake == 1)
        changes = Fork(parent_run=parent.record.run_id, at_seq=after_first_wake.seq)
        [child] = await session.fork(
            parent.record.run_id, changes, state=state, command=launched.command, listen=listen
        )
        heard = len(real.received)

    child_calls = [
        c for c in world(state, child.record.run_id, root=parent.record.run_id).calls() if c.exchange.host == "::1"
    ]
    # The fork sees its parent's lookup from before the fork, and not the one after it: then its own.
    assert [(c.wake, c.exchange.response_body) for c in child_calls[:1]] == [(1, '{"received": 1}')]
    [own] = child_calls[1:]
    assert own.exchange.captured is not None
    if in_forks is InForks.REPLAY:
        assert heard == 2  # the real host was not called again
        assert own.exchange.captured.answered_by is AnsweredBy.RECORDING
        assert own.exchange.response_body == '{"received": 2}'  # the parent's answer to the same call
        assert own.exchange.captured.replayed_from == f"parent run {parent.record.run_id}"
    else:
        assert heard == 3
        assert own.exchange.captured.answered_by is AnsweredBy.REAL_HOST
        assert own.exchange.response_body == '{"received": 3}'
    looked = launched.state()["looked_up"]
    assert isinstance(looked, list) and looked[-1] == own.exchange.response_body
