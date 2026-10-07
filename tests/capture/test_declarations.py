"""The declaration as an agent file states it, and the hosts it may not name."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand import session
from minutehand.adapters.proxy.capture import Capturing, refuse_claimed
from minutehand.adapters.proxy.policy import DEFAULT_MODEL_HOSTS
from minutehand.adapters.proxy.registry import ProviderConflict, Registry
from minutehand.application.files import load_agent
from minutehand.application.outbound import described, outbound_uses, suggested
from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import AgentUnderTest, GoalByWake, Reported
from minutehand.domain.outbound import Acknowledge, Answer, HtmlAt, InForks, PassThrough, RecordedRun, Replay
from minutehand.domain.run import OutboundUse
from minutehand.domain.world import CaptureMode

AGENT_FILE = """
name: mailer
wakes:
  - {kind: reported, wake_url: "http://127.0.0.1:9/wake", report_url: "http://127.0.0.1:9/report"}
outbound:
  - host: api.mail.example
    kind: acknowledge
    answer: {status: 202, json_body: {id: queued}}
    routes:
      - {method: POST, path: "/v3/batch/*", answer: {status: 201, text: batched}}
    message:
      recipients: ["personalizations[*].to[*].email"]
      text: [text, {html: "content[0].value"}]
      subject: [subject]
      handles: {"+15550100": sofia}
    redact: ["personalizations[*].custom_args.secret"]
  - host: search.example
    kind: pass_through
  - host: "*.weather.example"
    kind: replay
    source: {kind: run, run: 3f2a9c1e07bb}
    on_miss: pass_through
    ignore_query: [nonce]
    ignore_body: [sent_at]
"""


def _agent(*outbound: Acknowledge | PassThrough | Replay) -> AgentUnderTest:
    return AgentUnderTest(
        name="mailer",
        goal=GoalByWake(),
        wakes=[Reported(wake_url="http://127.0.0.1:9/wake", report_url="http://127.0.0.1:9/report")],
        outbound=list(outbound),
    )


def test_an_agent_file_declares_each_mode(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(AGENT_FILE)
    mail, search, weather = load_agent(path).outbound
    assert isinstance(mail, Acknowledge) and mail.answer == Answer(status=202, json_body={"id": "queued"})
    assert mail.message is not None and mail.message.text == ["text", HtmlAt(html="content[0].value")]
    assert mail.message.handles == {"+15550100": "sofia"} and mail.key == "api_mail_example"
    assert isinstance(search, PassThrough) and search.in_forks is InForks.REPLAY
    assert isinstance(weather, Replay) and weather.source == RecordedRun(run="3f2a9c1e07bb")
    assert weather.key == "any_weather_example"


def test_a_host_declared_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"outbound host declared twice: search\.example"):
        _agent(PassThrough(host="search.example"), Acknowledge(host="search.example"))


def test_overlapping_declarations_are_refused_naming_both() -> None:
    with pytest.raises(ProviderConflict, match=r"'\*\.example\.com' and 'api\.example\.com' overlap"):
        Capturing([PassThrough(host="*.example.com"), Acknowledge(host="api.example.com")])


def test_a_declared_host_a_provider_claims_is_refused_at_load_naming_both(registry: Registry) -> None:
    with pytest.raises(ProviderConflict) as refused:
        refuse_claimed([Acknowledge(host="api.ledger.test")], registry, DEFAULT_MODEL_HOSTS)
    assert "'api.ledger.test' is declared acknowledge" in str(refused.value)
    assert "provider 'ledger' claims '*.ledger.test'" in str(refused.value)


def test_a_declared_model_api_is_refused(registry: Registry) -> None:
    with pytest.raises(ProviderConflict, match=r"'api\.openai\.com' is a model API"):
        refuse_claimed([PassThrough(host="api.openai.com")], registry, DEFAULT_MODEL_HOSTS)


def test_a_declared_model_vendor_infrastructure_host_is_refused(registry: Registry) -> None:
    with pytest.raises(ProviderConflict, match=r"'mcp-proxy\.anthropic\.com' is a model API"):
        refuse_claimed([PassThrough(host="mcp-proxy.anthropic.com")], registry, DEFAULT_MODEL_HOSTS)


def test_a_declaration_named_as_a_provider_is_refused(registry: Registry) -> None:
    with pytest.raises(ProviderConflict, match="recorded as 'ledger', which is the name of provider 'ledger'"):
        refuse_claimed([Acknowledge(host="mail.example", name="ledger")], registry, DEFAULT_MODEL_HOSTS)


def test_a_run_whose_agent_declares_a_provider_host_is_refused_before_it_starts(tmp_path: Path) -> None:
    agent = _agent(Acknowledge(host="slack.com"))
    with pytest.raises(RunRefused, match=r"'slack.com' is declared acknowledge, and provider 'slack' claims"):
        session.capturing_for(agent, Registry.installed(), state=tmp_path)


def test_a_host_used_only_to_change_things_is_suggested_as_acknowledge_and_anything_else_as_pass_through() -> None:
    uses = [
        OutboundUse(host="mail.example", mode=CaptureMode.DISCOVERED, calls=2, methods=["POST"]),
        OutboundUse(host="search.example", mode=None, calls=1, refused=1, methods=["GET"]),
        OutboundUse(host="hook.example", mode=CaptureMode.ACKNOWLEDGE, calls=1, methods=["POST"]),
    ]
    assert suggested(uses) == (
        "outbound:\n- host: mail.example\n  kind: acknowledge\n- host: search.example\n  kind: pass_through\n"
    )
    assert described(uses[1]) == "search.example: 1 call, refused: nobody declares it"


def test_nothing_is_suggested_when_every_host_was_declared() -> None:
    assert outbound_uses([]) == [] and suggested([]) == ""
