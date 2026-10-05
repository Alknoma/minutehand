"""The MCP tools, driven through the SDK's own client over an in-memory connection, on real runs of the
end-to-end test agent: a forgetful agent and a person who never answers give a `no_follow_up` failure."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

from minutehand.adapters.mcp.results import (
    Evidence,
    FindingList,
    OutboundCalls,
    RunListing,
    RunsPlayed,
    ScenarioListing,
)
from minutehand.adapters.mcp.server import build
from minutehand.checks.patterns import pattern
from minutehand.domain.checks import FindingKind
from minutehand.domain.experiment import PersonChange
from minutehand.domain.outbound import Acknowledge, HtmlAt, MessageReading
from minutehand.domain.run import StopReason, VerdictKind
from minutehand.domain.scenario import PersonAsked, Silent
from minutehand.domain.world import Actor, AnsweredBy, CaptureMode, MessageSnapshot
from tests.e2e.support import OWNER, QUESTION, SOFIA, Launched, agent_under_test, answers, scenario

CALLER_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


@asynccontextmanager
async def connected(state: Path) -> AsyncIterator[ClientSession]:
    async with create_connected_server_and_client_session(build(state)) as client:
        yield client


async def call[M: BaseModel](client: ClientSession, tool: str, model: type[M], **arguments: object) -> M:
    result = await client.call_tool(tool, arguments)
    assert not result.isError, _text(result)
    return model.model_validate(result.structuredContent)


async def refused(client: ClientSession, tool: str, **arguments: object) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.isError, result.structuredContent
    return _text(result)


def _text(result: CallToolResult) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


def files(tmp_path: Path, launched: Launched) -> tuple[Path, Path]:
    """The forgetful agent's scenario (Sofia never answers) and its agent file, written where a user keeps them."""
    directory = tmp_path / "scenarios"
    directory.mkdir()
    scenario_file = directory / "partner_pricing.yaml"
    scenario_file.write_text(yaml.safe_dump(scenario(Silent()).model_dump(mode="json")))
    agent_file = directory / "agent.yaml"
    agent_file.write_text(yaml.safe_dump(launched.agent.model_dump(mode="json")))
    return scenario_file, agent_file


async def play_forgetful(client: ClientSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunsPlayed:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    scenario_file, agent_file = files(tmp_path, launched)
    return await call(
        client, "run_scenario", RunsPlayed, scenario=str(scenario_file), agent=str(agent_file), command=launched.command
    )


async def test_list_scenarios_gives_name_goal_people_and_expectations_and_sets_aside_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_file, agent_file = files(tmp_path, agent_under_test(tmp_path, monkeypatch, "forgetful"))
    async with connected(tmp_path / "state") as client:
        listing = await call(client, "list_scenarios", ScenarioListing, directory=str(scenario_file.parent))

    [found] = listing.scenarios
    assert (found.path, found.name, found.goal) == (
        str(scenario_file),
        "partner_pricing",
        "The partner pricing is confirmed with Sofia.",
    )
    assert [p.key for p in found.people] == ["owner", "sofia"]
    assert [e.kind for e in found.expectations] == ["person_asked", "person_asked"]
    assert [n.path for n in listing.not_scenarios] == [str(agent_file)]
    assert "not a valid Scenario" in listing.not_scenarios[0].reason


async def test_list_scenarios_on_a_missing_directory_is_refused(tmp_path: Path) -> None:
    async with connected(tmp_path / "state") as client:
        assert "is not a directory" in await refused(client, "list_scenarios", directory=str(tmp_path / "nowhere"))


async def test_run_scenario_reports_the_stop_the_counts_the_scorecard_and_the_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connected(tmp_path / "state") as client:
        played = await play_forgetful(client, tmp_path, monkeypatch)
        listed = await call(client, "list_runs", RunListing)

    [run] = played.runs
    assert run.stop is StopReason.NOTHING_PENDING and run.verdict.kind is VerdictKind.FAILED
    assert run.findings.fail >= 1
    assert (run.scorecard.expectations_met, run.scorecard.expectations_total) == (1, 2)
    assert [p.wake for p in run.checkpoints][:2] == [0, 1]
    assert played.stability is None
    assert [r.run_id for r in listed.runs] == [run.run_id]
    assert listed.runs[0].findings == run.findings and listed.runs[0].children == []


async def test_list_findings_names_each_findings_kind_time_wake_and_pattern_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connected(tmp_path / "state") as client:
        [run] = (await play_forgetful(client, tmp_path, monkeypatch)).runs
        found = await call(client, "list_findings", FindingList, run_id=run.run_id)

    assert [f.number for f in found.findings] == list(range(1, len(found.findings) + 1))
    [silence] = [f for f in found.findings if f.check == "no_follow_up"]
    assert silence.kind is FindingKind.FAIL and silence.message.startswith("wait on sofia expired")
    assert silence.at is not None and silence.evidence
    assert (silence.pattern, silence.pattern_title) == ("expiry_on_every_wait", "An expiry on every wait")
    assert sum(1 for f in found.findings if f.kind is FindingKind.FAIL) == run.findings.fail


async def test_show_evidence_hands_over_the_events_their_calls_the_trace_the_wake_and_the_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACEPARENT", f"00-{CALLER_TRACE}-00f067aa0ba902b7-01")
    async with connected(tmp_path / "state") as client:
        [run] = (await play_forgetful(client, tmp_path, monkeypatch)).runs
        found = await call(client, "list_findings", FindingList, run_id=run.run_id)
        [silence] = [f for f in found.findings if f.check == "no_follow_up"]
        evidence = await call(client, "show_evidence", Evidence, run_id=run.run_id, finding=silence.number)

    assert evidence.finding == silence
    [asked] = evidence.events
    assert asked.seq in silence.evidence and asked.actor is Actor.AGENT and asked.wake == 1
    assert isinstance(asked.after, MessageSnapshot) and asked.after.text == QUESTION
    assert SOFIA in asked.after.recipient_emails
    assert asked.call is not None
    assert (asked.call.method, asked.call.host, asked.call.path.split("?")[0], asked.call.status) == (
        "POST",
        "slack.com",
        "/api/chat.postMessage",
        200,
    )
    assert asked.call.trace_id == CALLER_TRACE
    assert [w.index for w in evidence.wakes] == [1]
    expiry = pattern("expiry_on_every_wait")
    assert evidence.pattern == expiry
    assert evidence.pattern is not None and evidence.pattern.design == expiry.design and expiry.failure


async def test_show_evidence_for_a_finding_that_does_not_exist_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connected(tmp_path / "state") as client:
        [run] = (await play_forgetful(client, tmp_path, monkeypatch)).runs
        message = await refused(client, "show_evidence", run_id=run.run_id, finding=999)
    assert "numbered from 1; there is no 999" in message


async def test_reading_a_run_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    async with connected(tmp_path / "state") as client:
        for tool, arguments in (
            ("list_findings", {"run_id": "nosuchrun"}),
            ("show_evidence", {"run_id": "nosuchrun", "finding": 1}),
            ("rerun_from", {"run_id": "nosuchrun", "at_seq": 1, "changes": []}),
        ):
            assert "no finished run nosuchrun" in await refused(client, tool, **arguments)


async def test_run_scenario_with_samples_reports_stability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", hooks=True)
    scenario_file, agent_file = files(tmp_path, launched)
    async with connected(tmp_path / "state") as client:
        played = await call(
            client,
            "run_scenario",
            RunsPlayed,
            scenario=str(scenario_file),
            agent=str(agent_file),
            command=launched.command,
            samples=2,
        )
    assert len({r.run_id for r in played.runs}) == 2
    assert played.stability is not None and (played.stability.samples, played.stability.passed) == (2, 0)


async def test_run_scenario_whose_agent_exits_at_once_is_refused_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_file, agent_file = files(tmp_path, agent_under_test(tmp_path, monkeypatch, "forgetful"))
    async with connected(tmp_path / "state") as client:
        message = await refused(
            client,
            "run_scenario",
            scenario=str(scenario_file),
            agent=str(agent_file),
            command=[sys.executable, "-c", "import sys; print('no secret for me'); sys.exit(4)"],
        )
        missing = await refused(client, "run_scenario", scenario=str(tmp_path / "gone.yaml"), agent=str(agent_file))
        # The refusal released the server: a run can be played after it.
        assert (await call(client, "list_runs", RunListing)).runs == []
    assert "the run could not be performed" in message and "exited 4" in message and "no secret for me" in message
    assert "could not be performed" in missing and "cannot be read" in missing


async def test_a_second_run_while_one_plays_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    scenario_file, agent_file = files(tmp_path, launched)
    arguments = {"scenario": str(scenario_file), "agent": str(agent_file), "command": launched.command}
    async with connected(tmp_path / "state") as client:
        results = await asyncio.gather(
            client.call_tool("run_scenario", arguments), client.call_tool("run_scenario", arguments)
        )
        listed = await call(client, "list_runs", RunListing)

    [played] = [r for r in results if not r.isError]
    [turned_away] = [r for r in results if r.isError]
    assert "only one can run at a time" in _text(turned_away)
    assert [r.run_id for r in listed.runs] == [RunsPlayed.model_validate(played.structuredContent).runs[0].run_id]


async def test_rerun_from_a_seq_that_is_not_a_checkpoint_is_refused_with_the_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connected(tmp_path / "state") as client:
        [run] = (await play_forgetful(client, tmp_path, monkeypatch)).runs
        not_one = max(p.seq for p in run.checkpoints) + 1000
        message = await refused(client, "rerun_from", run_id=run.run_id, at_seq=not_one, changes=[])
    assert f"seq {not_one} is not a checkpoint" in message
    for point in run.checkpoints:
        assert f"{point.seq} (after wake {point.wake})" in message


async def test_rerun_from_a_checkpoint_where_the_silent_person_answers_passes_and_is_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", hooks=True)
    scenario_file, agent_file = files(tmp_path, launched)
    async with connected(tmp_path / "state") as client:
        played = await call(
            client,
            "run_scenario",
            RunsPlayed,
            scenario=str(scenario_file),
            agent=str(agent_file),
            command=launched.command,
        )
        [parent] = played.runs
        after_first_wake = next(p for p in parent.checkpoints if p.wake == 1)
        change = PersonChange(person="sofia", reply=answers(after=timedelta(hours=36)))
        rerun = await call(
            client,
            "rerun_from",
            RunsPlayed,
            run_id=parent.run_id,
            at_seq=after_first_wake.seq,
            changes=[change.model_dump(mode="json")],
            command=launched.command,
        )
        child_findings = await call(client, "list_findings", FindingList, run_id=rerun.runs[0].run_id)
        listed = await call(client, "list_runs", RunListing)

    [child] = rerun.runs
    assert child.stop is StopReason.AGENT_DONE
    assert (child.parent_run, child.forked_at) == (parent.run_id, after_first_wake.seq)
    assert [f for f in child_findings.findings if f.check == "no_follow_up"] == []
    by_id = {r.run_id: r for r in listed.runs}
    assert by_id[parent.run_id].children == [child.run_id]
    assert by_id[child.run_id].parent_run == parent.run_id


async def test_list_outbound_calls_and_show_evidence_say_how_each_host_beyond_the_fakes_was_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent emails the owner through a declared host and calls one nobody declared."""
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful")
    monkeypatch.setenv("MAIL_URL", "https://api.mail.test/v3/mail/send")
    monkeypatch.setenv("MAIL_TO", OWNER)
    monkeypatch.setenv("STRAY_URL", "https://api.unclaimed.example/v1/ping")
    reading = MessageReading(recipients=["personalizations[*].to[*].email"], text=[HtmlAt(html="content[0].value")])
    agent = launched.agent.model_copy(
        update={"outbound": [Acknowledge(host="api.mail.test", name="mail", message=reading)]}
    )
    directory = tmp_path / "scenarios"
    directory.mkdir()
    reported = scenario(Silent()).model_copy(update={"expect": [PersonAsked(person="owner", mentions=["report"])]})
    (directory / "s.yaml").write_text(yaml.safe_dump(reported.model_dump(mode="json")))
    (directory / "agent.yaml").write_text(yaml.safe_dump(agent.model_dump(mode="json")))
    async with connected(tmp_path / "state") as client:
        [run] = (
            await call(
                client,
                "run_scenario",
                RunsPlayed,
                scenario=str(directory / "s.yaml"),
                agent=str(directory / "agent.yaml"),
                command=launched.command,
            )
        ).runs
        outbound = await call(client, "list_outbound_calls", OutboundCalls, run_id=run.run_id)
        found = await call(client, "list_findings", FindingList, run_id=run.run_id)
        [met] = [f for f in found.findings if f.check == "expectations"]
        evidence = await call(client, "show_evidence", Evidence, run_id=run.run_id, finding=met.number)

    assert [(h.host, h.mode, h.calls, h.refused) for h in outbound.hosts] == [
        ("api.unclaimed.example", None, 1, 1),
        ("api.mail.test", CaptureMode.ACKNOWLEDGE, 1, 0),
    ]
    [mail] = outbound.calls
    assert mail.call.captured is not None and mail.call.captured.answered_by is AnsweredBy.DECLARATION
    assert mail.call.request_body is not None and "sg-key-in-body" not in mail.call.request_body
    [emailed] = evidence.events
    assert emailed.seq in mail.events and emailed.call is not None and emailed.call.captured == mail.call.captured
