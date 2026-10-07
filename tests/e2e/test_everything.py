"""One run that touches every part of Minutehand built for proactive agents, through the real session, proxy and
providers, with an agent in its own process and every model call answered by a local fake (`fake_completions`):
nothing reaches a real service or a real model.

Recorded dispatch table and dispatch rules, a Cloud Tasks booking made late, machine commands and watched folders,
the agent's own checks, its reported commitments held against the world, a model standing in for undeclared hosts,
MCP tool calls over HTTP, and a contained agent's own timer as its wake."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand import session
from minutehand.adapters.model.openai_compatible import OpenAICompatible
from minutehand.adapters.providers.google_cloud_tasks.provider import CloudTasksSeed, SeededQueue
from minutehand.adapters.proxy.modeled import ModeledAnswer
from minutehand.application.dues import due_entries
from minutehand.domain.agent import AgentUnderTest, Booked, Contained, Reported
from minutehand.domain.checks import FindingKind
from minutehand.domain.clock import DueClosed, DueSource
from minutehand.domain.outbound import UnknownHosts
from minutehand.domain.scenario import DispatchFault, WrittenScenario
from minutehand.domain.world import (
    Actor,
    AnsweredBy,
    EntityKind,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    ToolCallSnapshot,
)
from tests.e2e.support import free_port, world
from tests.model.fake_completions import Received, fake_completions

pytestmark = pytest.mark.timeout(120)

AGENTS = Path(__file__).parent / "agents"
QUEUE = "projects/sim/locations/us-central1/queues/follow-ups"
HOUR = 3600 * 10**9

OWN_CHECK = """
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, Severity
from minutehand.domain.world import MessageSnapshot


class NoPasswordsInMessages:
    id = "no_passwords_in_messages"
    needs = frozenset({Needs.WORLD})

    def run(self, view):
        sent = [e for e in view.events if isinstance(e.after, MessageSnapshot) and e.actor.value == "agent"]
        bad = [e.seq for e in sent if "password" in e.after.text.lower()]
        kind = FindingKind.FAIL if bad else FindingKind.INFORMATIONAL
        return CheckReport(findings=[Finding(check=self.id, severity=Severity.INFORMATION, kind=kind,
                                             message=f"{len(sent)} messages read, {len(bad)} with a password", evidence=bad)])
"""


def model_rule(received: Received) -> ModeledAnswer:
    """What the model standing in for each undeclared host answers."""
    asked = received.last.split("The request to answer now:\n", 1)[1]
    if asked.startswith("POST /api/contacts"):
        return ModeledAnswer(status=201, body=json.dumps({"id": "c_1", "name": "Rosa Lind"}))
    if asked.startswith("POST /mcp"):
        result = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "created ISSUE-7"}]}}
        return ModeledAnswer(status=200, body=json.dumps(result))
    return ModeledAnswer(status=404, body="{}")


async def test_one_run_through_every_part(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "old.zip").write_bytes(b"x" * 100)
    (tmp_path / "team_checks.py").write_text(OWN_CHECK)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    sandbox_state = tmp_path / "sandbox.json"
    sandbox_state.write_text(json.dumps({"now_ns": 0, "timers": [30 * HOUR], "fire_url": f"{base}/timer"}))
    stub = [sys.executable, str(AGENTS / "sandbox_stub.py"), str(sandbox_state)]
    agent = AgentUnderTest(
        name="everything",
        wakes=[
            Reported(wake_url=f"{base}/wake", report_url=f"{base}/report"),
            Booked(take_limit=timedelta(seconds=5)),
            Contained(deadlines=[*stub, "deadlines"], advance=[*stub, "advance", "{nanoseconds}"]),
        ],
        watches=[str(downloads)],
        checks=[str(tmp_path / "team_checks.py")],
    )
    queue = SeededQueue(name=QUEUE)
    written = WrittenScenario.model_validate(
        {
            "name": "everything",
            "goal": "Confirm the venue for the offsite with Rosa.",
            "owner": "owen",
            "deadline_after": "P2D",
            "people": [
                {
                    "key": "owen",
                    "name": "Owen Hart",
                    "email": "owen@example.com",
                    "reply": {"kind": "scripted", "replies": []},
                },
                {"key": "rosa", "name": "Rosa Lind", "email": "rosa@example.com", "reply": {"kind": "silent"}},
            ],
            "provider_seeds": [
                {"provider": "google_cloud_tasks", "body": CloudTasksSeed(queues=[queue]).model_dump_json()}
            ],
            "machine": [
                {
                    "after": "PT1H",
                    "said": "a download appears",
                    "argv": [sys.executable, "-c", f"open({str(downloads / 'new.pkg')!r}, 'wb').write(b'y' * 42)"],
                }
            ],
            "dispatch": [{"wakes": "booked", "fault": "late", "by": "PT1H"}],
            "expect": [
                {"kind": "person_asked", "person": "rosa", "at_least": 2},
                {"kind": "person_asked", "person": "owen", "mentions": ["still waiting"]},
                {"kind": "file_removed", "path": "*/Downloads/old.zip"},
                {"kind": "tool_called", "tool": "create_issue", "mentions": ["venue"], "succeeded": True},
            ],
        }
    )
    command = [
        sys.executable,
        str(AGENTS / "everything_agent.py"),
        "--port",
        str(port),
        "--queue",
        QUEUE,
        "--downloads",
        str(downloads),
    ]

    async with fake_completions(model_rule) as fake:
        model = OpenAICompatible(base_url=fake.base_url, api_key="test", model_id="stand-in")
        listen = session.Listen(capture_unknown=UnknownHosts.MODEL, receive_telemetry=False)
        [outcome] = await session.play(
            written, agent, state=tmp_path / "state", command=command, model=model, listen=listen
        )

    record, result = outcome.record, outcome.result
    found = {(f.check, f.kind) for f in result.findings}
    assert ("expectations", FindingKind.FAIL) not in found, [
        f.message for f in result.findings if f.kind is FindingKind.FAIL
    ]
    assert ("no_passwords_in_messages", FindingKind.INFORMATIONAL) in found, "the agent's own check ran"
    assert not [f for f in result.findings if f.check == "reported_against_world" and f.kind is FindingKind.FAIL]

    held = world(tmp_path / "state", record.run_id)
    events = held.events()
    start = record.started_at
    to = {"rosa@example.com": "rosa", "owen@example.com": "owen"}
    messages = [
        (to[e.after.recipient_emails[0]], e.sim_time - start)
        for e in events
        if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
    ]
    assert messages == [("rosa", timedelta(0)), ("rosa", timedelta(hours=6)), ("owen", timedelta(hours=30))], (
        "asked at once; the Cloud Tasks follow-up made an hour late by the dispatch rule; the sandbox timer at 30 hours"
    )

    dues = due_entries(held)
    [booked] = [d for d in dues if d.source is DueSource.BOOKED and d.asked_for is None]
    assert (booked.closed, booked.fault) == (DueClosed.DELAYED, DispatchFault.LATE)
    [timer] = [d for d in dues if d.source is DueSource.TIMER and d.closed is DueClosed.FIRED]
    assert timer.closed_at is not None and timer.closed_at - start == timedelta(hours=30)

    machine = [
        (e.after.text, e.sim_time - start)
        for e in events
        if isinstance(e.after, RecordSnapshot) and e.after.resource == "machine"
    ]
    assert machine == [("a download appears: exit 0", timedelta(hours=1))]
    files = [
        (e.actor, e.operation, Path(e.entity.external_id).name) for e in events if e.entity.kind is EntityKind.FILE
    ]
    assert files == [(Actor.AGENT, Operation.DELETE, "old.zip"), (Actor.SCENARIO, Operation.CREATE, "new.pkg")]

    [tool] = [e.after for e in events if isinstance(e.after, ToolCallSnapshot)]
    assert (tool.server, tool.tool, tool.result, tool.is_error) == (
        "mcp.example",
        "create_issue",
        "created ISSUE-7",
        False,
    )
    modeled = {
        c.exchange.host
        for c in held.calls()
        if c.exchange.captured and c.exchange.captured.answered_by is AnsweredBy.MODEL
    }
    assert modeled == {"crm.example", "mcp.example"}
    assert len(fake.received) == 2, "the model was asked once for each undeclared write, and nothing else"
