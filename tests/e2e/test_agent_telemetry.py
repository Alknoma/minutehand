"""The agent's own telemetry through a whole run: the test agent traces itself with the stock SDK (`--trace`),
exports to the endpoint the run hands it, and the finding's evidence resolves to the agent's span and to the
model call that chose the message."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from minutehand import session
from minutehand.adapters.mcp.results import Evidence, FindingList
from minutehand.adapters.mcp.server import build
from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import ModelCallsResponse, TraceResponse
from minutehand.application.model_calls import JoinedBy
from minutehand.domain.experiment import Fork, PersonChange
from minutehand.domain.scenario import Silent
from minutehand.domain.telemetry import Signal, SpanSource
from minutehand.domain.world import Actor
from tests.e2e.support import SOFIA, agent_under_test, answers, messages, scenario, world
from tests.telemetry.collector import collector


async def test_a_findings_evidence_resolves_to_the_agents_span_and_the_model_call_behind_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", tracing=True)
    state = tmp_path / "state"
    [outcome] = await session.play(scenario(Silent()), launched.agent, state=state, command=launched.command)
    run_id = outcome.record.run_id
    [asked] = messages(world(state, run_id).events(), Actor.AGENT, to=SOFIA)

    async with create_connected_server_and_client_session(build(state)) as client:
        listed = FindingList.model_validate(
            (await client.call_tool("list_findings", {"run_id": run_id})).structuredContent
        )
        [silence] = [f for f in listed.findings if f.check == "no_follow_up"]
        shown = await client.call_tool("show_evidence", {"run_id": run_id, "finding": silence.number})
    evidence = Evidence.model_validate(shown.structuredContent)

    [cited] = [e for e in evidence.events if e.seq == asked.seq]
    assert cited.call is not None and cited.call.trace_id is not None
    assert cited.agent_spans == ["send_dm", "agent turn"]
    assert cited.joined_by is JoinedBy.TRACE
    call = cited.model_call
    assert call is not None and call.trace_id == cited.call.trace_id
    assert (call.name, call.model, call.source, call.wake) == ("chat model-test", "model-test", SpanSource.RECEIVED, 1)
    assert (call.input_tokens, call.output_tokens) == (120, 18)
    assert call.input_messages is not None and f"What should {SOFIA} be told?" in call.input_messages
    assert call.output_messages is not None
    [said] = json.loads(call.output_messages)
    assert said["parts"][0]["arguments"]["text"] == "Could you confirm the partner pricing, please?"
    assert "span(s) of the agent's own telemetry were received" in evidence.telemetry

    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as viewer:
        joined = ModelCallsResponse.model_validate_json((await viewer.get(f"/api/runs/{run_id}/model-calls")).content)
        traced = TraceResponse.model_validate_json(
            (await viewer.get(f"/api/runs/{run_id}/traces/{cited.call.trace_id}")).content
        )
    [behind] = [t for t in joined.events if t.seq == asked.seq]
    assert behind.model_call == call and joined.received == len(traced.spans) == 3
    assert sorted(s.span.name for s in traced.spans) == ["agent turn", "chat model-test", "send_dm"]


async def test_a_run_that_received_nothing_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    state = tmp_path / "state"
    [outcome] = await session.play(scenario(Silent()), launched.agent, state=state, command=launched.command)
    run_id = outcome.record.run_id

    async with create_connected_server_and_client_session(build(state)) as client:
        listed = FindingList.model_validate(
            (await client.call_tool("list_findings", {"run_id": run_id})).structuredContent
        )
        [silence] = [f for f in listed.findings if f.check == "no_follow_up"]
        shown = await client.call_tool("show_evidence", {"run_id": run_id, "finding": silence.number})
    evidence = Evidence.model_validate(shown.structuredContent)

    assert evidence.telemetry.startswith("No telemetry was received from the agent in this run")
    assert all(e.model_call is None and e.joined_by is None for e in evidence.events)


async def test_a_fork_sees_its_parents_spans_up_to_the_fork_and_not_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "diligent", hooks=True, tracing=True)
    state = tmp_path / "state"
    [parent] = await session.play(scenario(Silent()), launched.agent, state=state, command=launched.command)
    parent_id = parent.record.run_id
    by_wake = sorted({(s.wake, s.span.name) for s in world(state, parent_id).spans()})
    # Wake 1 asked, wake 2 followed up: each a turn of three spans.
    assert {w for w, _ in by_wake} == {1, 2}

    point = next(p for p in session.fork_points(state, parent_id) if p.wake == 1)
    changes = Fork(
        parent_run=parent_id,
        at_seq=point.seq,
        overrides=[PersonChange(person="sofia", reply=answers(after=timedelta(hours=36)))],
    )
    [child] = await session.fork(parent_id, changes, state=state, command=launched.command)

    seen = world(state, child.record.run_id, root=parent_id).spans()
    inherited = [s for s in seen if s.run_id == parent_id]
    own = [s for s in seen if s.run_id == child.record.run_id]
    assert inherited == [s for s in world(state, parent_id).spans() if s.wake == 1]
    assert len(inherited) == 3 and all(s.after_seq <= point.seq for s in inherited)
    # The child's own turn: thanking Sofia is a plain reply; telling the owner is a DM, traced.
    assert sorted(s.span.name for s in own) == ["agent turn", "chat model-test", "send_dm"]
    assert all(s.wake == 2 for s in own)


async def test_the_agents_telemetry_is_passed_on_and_a_dead_destination_does_not_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", tracing=True)
    async with collector() as original:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", original.url)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-api-key=collector-key")
        [passed_on] = await session.play(
            scenario(Silent()), launched.agent, state=tmp_path / "live", command=launched.command
        )
    assert [c.path for c in original.collected] == ["/v1/traces"]
    assert original.collected[0].header("x-api-key") == "collector-key"
    assert world(tmp_path / "live", passed_on.record.run_id).forward_failures() == []

    async with collector() as gone:
        dead = gone.url
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", dead)
    [outcome] = await session.play(
        scenario(Silent()), launched.agent, state=tmp_path / "dead", command=launched.command
    )

    assert outcome.record.stop is passed_on.record.stop and outcome.record.failure is None
    kept = world(tmp_path / "dead", outcome.record.run_id)
    assert len(kept.spans()) == 3
    [failure] = kept.forward_failures()
    assert (failure.signal, failure.endpoint) == (Signal.TRACES, f"{dead}/v1/traces")
