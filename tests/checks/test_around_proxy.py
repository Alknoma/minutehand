"""Calls the agent made around the proxy: counted from its own HTTP client spans against what the proxy recorded, and
the check that fails a run on them, or asks a person when the only sign is that nothing was called."""

from __future__ import annotations

from datetime import UTC, datetime

from minutehand.application.around_proxy import around_proxy, uncalled_providers
from minutehand.checks.around_proxy import WentAroundProxy
from minutehand.domain.checks import AroundProxy, FindingKind, RunView
from minutehand.domain.scenario import Person, ProviderKey, Scenario
from minutehand.domain.telemetry import Attribute, Placement, ReceivedSpan, SpanSource, StoredSpan, StringValue
from minutehand.domain.world import Exchange, RecordedCall
from tests.checks.world import ONE_WAKE

AT = datetime(2026, 9, 7, 9, tzinfo=UTC)
TRACE = "0af7651916cd43dd8448eb211c80319c"


def _claims(host: str) -> ProviderKey | None:
    return "slack" if host == "slack.com" else None


def _span(span_id: str, attributes: dict[str, str], *, source: SpanSource = SpanSource.RECEIVED) -> StoredSpan:
    return StoredSpan(
        span=ReceivedSpan(
            trace_id=TRACE,
            span_id=span_id,
            name="POST",
            start=AT,
            end=AT,
            attributes=[Attribute(key=k, value=StringValue(value=v)) for k, v in attributes.items()],
        ),
        run_id="run",
        source=source,
        wake=1,
        placed_by=Placement.WINDOW,
        arrived_in_wake=1,
        sim_time=AT,
        after_seq=0,
    )


def _client(span_id: str, url: str = "https://slack.com/api/chat.postMessage") -> StoredSpan:
    return _span(span_id, {"http.request.method": "POST", "url.full": url})


def _call(host: str = "slack.com", parent: str | None = None) -> RecordedCall:
    traceparent = f"00-{TRACE}-{parent}-01" if parent is not None else None
    exchange = Exchange(method="POST", host=host, path="/api/chat.postMessage", status=200, traceparent=traceparent)
    return RecordedCall(exchange=exchange, provider=_claims(host), first_seq=1, last_seq=0, wake=1, sim_time=AT)


def test_a_client_span_the_proxy_recorded_by_its_traceparent_did_not_go_around() -> None:
    assert around_proxy([_client("a" * 16)], [_call(parent="a" * 16)], _claims) == []


def test_a_client_span_with_no_record_went_around_the_proxy() -> None:
    [went] = around_proxy([_client("a" * 16), _client("b" * 16)], [_call(parent="a" * 16)], _claims) or []
    assert went == AroundProxy(
        host="slack.com",
        provider="slack",
        by_agent=2,
        through_proxy=1,
        around=1,
        example="POST https://slack.com/api/chat.postMessage",
    )


def test_a_recorded_call_without_a_traceparent_accounts_for_one_unmatched_span() -> None:
    spans = [_client("a" * 16), _client("b" * 16)]
    assert around_proxy(spans, [_call()], _claims) == [
        AroundProxy(host="slack.com", provider="slack", by_agent=2, through_proxy=1, around=1, example=_example())
    ]
    assert around_proxy(spans, [_call(), _call()], _claims) == []


def test_a_recorded_call_made_inside_a_span_that_is_no_http_call_accounts_for_no_client_span() -> None:
    turn = _span("c" * 16, {"agent.step": "send"})
    went = around_proxy([turn, _client("a" * 16)], [_call(parent="c" * 16)], _claims)
    assert went is not None and [w.around for w in went] == [1]


def test_hosts_no_provider_claims_and_spans_minutehand_wrote_are_not_counted() -> None:
    elsewhere = _client("a" * 16, url="https://api.example.com/x")
    wire = _span("b" * 16, {"http.request.method": "POST", "server.address": "slack.com"}, source=SpanSource.WIRE)
    assert around_proxy([elsewhere, wire], [], _claims) == []


def test_the_host_is_read_from_server_address_when_the_span_names_no_url() -> None:
    went = around_proxy([_span("a" * 16, {"http.method": "GET", "net.peer.name": "slack.com"})], [], _claims)
    assert went is not None and [(w.host, w.example) for w in went] == [("slack.com", "POST slack.com")]


def test_without_a_span_of_an_http_call_nobody_can_say() -> None:
    assert around_proxy([_span("a" * 16, {"gen_ai.operation.name": "chat"})], [], _claims) is None


def test_providers_none_of_which_was_called_by_an_agent_that_was_woken_are_named() -> None:
    assert uncalled_providers({"slack", "jira"}, [_call("api.example.com")], woken=True) == ["jira", "slack"]
    assert uncalled_providers({"slack", "jira"}, [_call()], woken=True) == []
    assert uncalled_providers({"slack"}, [], woken=False) == []


def _example() -> str:
    return "POST https://slack.com/api/chat.postMessage"


def _view(*, around: list[AroundProxy] | None, uncalled: list[ProviderKey]) -> RunView:
    scenario = Scenario(
        name="s", goal="g", owner="olu", people=[Person(key="olu", name="Olu", email="olu@example.com")], starts_at=AT
    )
    return RunView(scenario=scenario, events=[], wakes=[ONE_WAKE], around_proxy=around, uncalled_providers=uncalled)


def test_calls_that_went_around_fail_the_run_with_the_fixes() -> None:
    went = AroundProxy(host="slack.com", provider="slack", by_agent=2, through_proxy=0, around=2, example=_example())
    [finding] = WentAroundProxy().run(_view(around=[went], uncalled=["slack"])).findings
    assert finding.kind is FindingKind.FAIL
    assert finding.message.startswith(
        "the agent's own telemetry shows 2 calls to slack.com (slack) and the proxy saw 0: 2 went around Minutehand"
    )
    assert "NODE_USE_ENV_PROXY=1" in finding.message and "--transparent-port" in finding.message


def test_an_agent_that_called_nothing_and_exported_no_http_span_is_a_question() -> None:
    [finding] = WentAroundProxy().run(_view(around=None, uncalled=["slack"])).findings
    assert finding.kind is FindingKind.REVIEW
    assert finding.message.startswith(
        "the agent was woken 1 time and called nothing of slack through Minutehand and exported no span of an HTTP call"
    )


def test_an_agent_that_called_a_provider_has_nothing_to_answer_for() -> None:
    assert WentAroundProxy().run(_view(around=[], uncalled=[])).findings == []
