"""A harness that drives its own agent and uses `minutehand serve` only for the fakes and the record, played through
the client alone: one case of three worlds, four steps marked beside four clock moves, the agent on stock
`slack_sdk`, a person answering through the control API, a message rewritten in place, a relay, two pings, and the
agent's model calls exported over OTLP by a process of its own with the real OpenTelemetry SDK and no trace link to
any call. The case reads as one run, judged on what happened; the same run with no step and nothing declared is not
judged.

Before, each world read "Passed", 0 of 0 expectations, 0 waits, "woke 0 times", while the record held a wrong
question, a rewrite and a relay; and every span sat in the lobby."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from minutehand.adapters.control.wire import Claims, CreateWorld, Inbound, Reply
from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import MessageChange, MessagesResponse, ModelTrafficResponse
from minutehand.application.forks import scorecard_lines
from minutehand.application.model_calls import JoinedBy
from minutehand.checks.runner import NOTHING_ASSESSED, RunResult
from minutehand.domain.checks import FindingKind
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import Seed
from minutehand.domain.world import Actor, EntityKind, EntityRef
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenCase, open_case
from tests.serve.support import SECRET, Served, answer, dm, event_receiver

ASK = "Dani, which day works for moving the engineering sync this week?"
ANSWER = "Wednesday afternoon works for me."
REWRITTEN = "Dani, which day works for moving the engineering sync this week? (Answered, thank you.)"
RELAY = "Nadia, Dani says Wednesday afternoon works for the engineering sync."
PINGS = ("Dani, just checking in on the sync day.", "Dani, any news on the sync day?")

EXPORT = """
import json, sys, time
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

said = sys.argv[1]
provider = TracerProvider(resource=Resource.create({"service.name": "the-agents-planner"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
tracer = provider.get_tracer("planner")
output = json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": said}]}])
with tracer.start_as_current_span("chat gpt-test") as span:
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", "gpt-test")
    span.set_attribute("gen_ai.output.messages", output)
    span.set_attribute("gen_ai.usage.input_tokens", 812)
    span.set_attribute("gen_ai.usage.output_tokens", 37)
    time.sleep(0.01)
provider.shutdown()
"""


def _export(environment: dict[str, str], said: str) -> None:
    """The agent's model writes `said`: one span, exported by the SDK in a process of its own, carrying no link to
    any call the proxy will see."""
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = environment["OTEL_EXPORTER_OTLP_ENDPOINT"]
    env["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
    subprocess.run([sys.executable, "-c", EXPORT, said], env=env, check=True, timeout=30)


def _seed(*, expect: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "starts_at": "2026-10-05T15:00:00Z",
        "people": [
            {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
            {
                "key": "dani",
                "name": "Dani Ruiz",
                "email": "dani@example.com",
                "reply": {"kind": "scripted", "replies": [{"to_ask": 1, "text": ANSWER}]},
            },
            # told things, never asked: scripted, with nothing to say
            {
                "key": "nadia",
                "name": "Nadia Okafor",
                "email": "nadia@example.com",
                "reply": {"kind": "scripted", "replies": []},
            },
        ],
        "expect": expect,
    }


EXPECT = [
    {"kind": "person_asked", "person": "dani"},
    {"kind": "relayed", "said_by": "dani", "to": "nadia", "tell": "Wednesday afternoon"},
    {"kind": "person_asked", "person": "owen", "mentions": ["Wednesday"]},
]


def _specs(inbound: str, suffix: str, *, expect: list[dict[str, Any]]) -> list[CreateWorld]:
    token = f"xoxb-driven-{suffix}"
    messaging = CreateWorld(
        seed=Seed.model_validate(_seed(expect=expect)),
        claims=Claims(tokens=[token]),
        inbound=[Inbound(provider="slack", url=inbound, secret=SECRET)],
    )
    refresh = f"1//driven-{suffix}"
    documents = CreateWorld(
        seed=Seed.model_validate(
            {
                **_seed(expect=[]),
                "documents": [{"provider": "google_workspace", "title": "Sync notes", "text": "# Sync notes"}],
                "sign_ins": [{"provider": "google_workspace", "credential": refresh, "person": "owen"}],
            }
        ),
        claims=Claims(tokens=[refresh]),
    )
    asana = f"asana-driven-{suffix}"
    tracker = CreateWorld(
        seed=Seed.model_validate(
            {
                **_seed(expect=[]),
                "tickets": [{"provider": "asana", "project": "Ops", "title": "Move the sync", "assignee": "dani"}],
                "provider_seeds": [{"provider": "asana", "body": {"tokens": [{"token": asana}]}}],
            }
        ),
        claims=Claims(tokens=[asana]),
    )
    return [messaging, documents, tracker]


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def served(state: Path) -> Iterator[Served]:
    with serve_in_background(state) as url, MinutehandClient(url) as client:
        yield Served(url=url, client=client, environment=client.environment())


def _play(served: Served, case: OpenCase, *, steps: bool) -> None:
    """The consumer's cycles: at each, the clocks move; with `steps`, a step is marked around what the agent does."""
    messaging = case.worlds[0]
    token = messaging.view.claims.tokens[0]
    slack = served.slack(token)
    to_dani = dm(served, token, "dani@example.com")
    to_nadia = dm(served, token, "nadia@example.com")
    start = messaging.view.now

    @contextmanager
    def cycle(n: int, reason: str) -> Iterator[None]:
        at = start + timedelta(hours=n)
        if not steps:
            yield
            return
        if n:
            case.advance(to=at)
        with case.step(at=at, reason=reason):
            yield

    with cycle(0, "the morning look"):
        _export(served.environment, ASK)
        asked = answer(slack.chat_postMessage(channel=to_dani, text=ASK))
    message = EntityRef(provider="slack", kind=EntityKind.MESSAGE, external_id=asked["ts"])
    messaging.client.act(messaging.world_id, Reply(person="dani", text=ANSWER, to=message))
    with cycle(1, "dani answered"):
        _export(served.environment, REWRITTEN)
        slack.chat_update(channel=to_dani, ts=asked["ts"], text=REWRITTEN)
        _export(served.environment, RELAY)
        slack.chat_postMessage(channel=to_nadia, text=RELAY)
    for n, ping in enumerate(PINGS, start=2):
        with cycle(n, "the hourly look"):
            slack.chat_postMessage(channel=to_dani, text=ping)


def _messages(state: Path, run_id: str) -> MessagesResponse:
    with TestClient(create_app(state)) as viewer:
        return MessagesResponse.model_validate_json(viewer.get(f"/api/runs/{run_id}/messages").content)


def _traffic(state: Path, run_id: str) -> ModelTrafficResponse:
    with TestClient(create_app(state)) as viewer:
        return ModelTrafficResponse.model_validate_json(viewer.get(f"/api/runs/{run_id}/model-traffic").content)


def test_a_case_driven_from_outside_is_one_run_judged_on_what_it_did(served: Served, state: Path) -> None:
    with event_receiver() as inbound:
        case = open_case(served.client, "timed dm, run 2", _specs(inbound.url, "one", expect=EXPECT))
        _play(served, case, steps=True)
        closed = case.close()
    result: RunResult = closed.result

    assert result.verdict.kind is VerdictKind.FAILED, result.verdict.words
    assert result.verdict.words.startswith("Failed: 1 check failed")
    [failed] = [f for f in result.findings if f.kind is FindingKind.FAIL]
    assert failed.check == "expectations" and failed.message.startswith("owen asked mentioning ['Wednesday']")
    met = sorted(f.message.split(":")[0] for f in result.findings if f.kind is FindingKind.INFORMATIONAL)
    assert met == ["dani asked", "nadia told what dani said ('Wednesday afternoon')"], met

    card = result.effectiveness
    lines = {x.label: x.value for x in scorecard_lines(card)}
    assert lines["expectations met"] == "2 of 3"
    assert lines["waits opened"] == "1, still open at the end: 0"
    assert lines["wakes"] == "4, of which changed nothing: 0"
    assert lines["messages to people"] == "4, edited in place: 1, deleted: 0"
    assert [(b.person, b.messages) for b in card.burden] == [("owen", 0), ("dani", 3), ("nadia", 1)]

    said = _messages(state, case.case_id).messages
    agents = [m for m in said if m.actor is Actor.AGENT]
    assert [(m.change, m.to) for m in agents] == [
        (MessageChange.SENT, ["Dani Ruiz"]),
        (MessageChange.EDITED, ["Dani Ruiz"]),
        (MessageChange.SENT, ["Nadia Okafor"]),
        (MessageChange.SENT, ["Dani Ruiz"]),
        (MessageChange.SENT, ["Dani Ruiz"]),
    ]
    assert agents[1].before == ASK and agents[1].text == REWRITTEN
    written = [(m.text, m.written_by.joined_by if m.written_by else None) for m in agents]
    assert written[0] == (ASK, JoinedBy.CONTENT) and written[2] == (RELAY, JoinedBy.CONTENT)
    assert written[3][1] is not JoinedBy.CONTENT, "a ping no model call answered is joined, if at all, as nearest"

    traffic = _traffic(state, case.case_id)
    assert [(c.step, c.model, c.input_tokens) for c in traffic.calls] == [(1, "gpt-test", 812)] + [
        (2, "gpt-test", 812)
    ] * 2, "the spans reached the case, with no trace link, each placed in its step"
    assert sorted(len(c.wrote) for c in traffic.calls) == [1, 1, 1]


def test_the_same_run_with_no_step_and_nothing_declared_is_not_judged(served: Served, state: Path) -> None:
    with event_receiver() as inbound:
        case = open_case(served.client, "timed dm, unmarked", _specs(inbound.url, "two", expect=[]))
        _play(served, case, steps=False)
        closed = case.close()

    verdict = closed.result.verdict
    assert verdict.kind is VerdictKind.NOT_JUDGED and closed.result.exit_code == 5, verdict.words
    assert verdict.unjudged == [NOTHING_ASSESSED] and verdict.words.startswith("Not assessed:")
    assert closed.result.effectiveness.waits_opened == 1, "the ask Dani answered is still in the ledger"
