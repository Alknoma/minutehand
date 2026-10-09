"""The simulated world's health over a whole run with a declared service (`checks.health`): a responder whose script
ends in silence can never act on what waits on them, so the run is `SIMULATION_INCOMPLETE`, naming the item, the
person and since when; the same responder declared `silent` is the author's word that nobody decides, and the run
is judged as it stands."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from minutehand import session
from minutehand.adapters.query.reader import open_model, query
from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import FindingsResponse
from minutehand.domain.agent import AgentUnderTest, Command
from minutehand.domain.checks import HealthKind
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import EntityKind, PendingSnapshot
from tests.e2e.support import world
from tests.support.people import people_model

START = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
AGENTS = Path(__file__).parent / "agents"
APPROVALS = {
    "initial": "pending",
    "states": ["pending", "approved", "rejected"],
    "transitions": [
        {"name": "approve", "from": ["pending"], "to": "approved", "by": "person"},
        {"name": "reject", "from": ["pending"], "to": "rejected", "by": "person"},
    ],
}


def _scenario(nadia: dict[str, object]) -> Scenario:
    return Scenario.model_validate(
        {
            "name": "orders",
            "goal": "Order a laptop once it is approved.",
            "owner": "owen",
            "starts_at": START.isoformat(),
            "deadline_after": "P2D",
            "people": [
                {"key": "owen", "name": "Owen Hart", "email": "owen@example.com", "reply": {"kind": "silent"}},
                {"key": "nadia", "name": "Nadia Ek", "email": "nadia@example.com", "reply": nadia},
            ],
            "services": [
                {
                    "host": "api.orders.example",
                    "name": "orders",
                    "responders": ["nadia"],
                    "within": {"min": "PT3H", "max": "P1D"},
                    "machine": APPROVALS,
                }
            ],
        }
    )


async def _play(tmp_path: Path, scenario: Scenario) -> session.Outcome:
    agent = AgentUnderTest(name="filer", wakes=[Command(argv=[sys.executable, str(AGENTS / "filer.py")])])
    [outcome] = await session.play(
        scenario, agent, state=tmp_path / "state", model=people_model(), listen=session.Listen(receive_telemetry=False)
    )
    return outcome


async def test_a_responder_scripted_to_silence_makes_the_run_simulation_incomplete(tmp_path: Path) -> None:
    outcome = await _play(tmp_path, _scenario({"kind": "scripted", "then": "silent"}))

    held = world(tmp_path / "state", outcome.record.run_id)
    [pending] = [e for e in held.events() if isinstance(e.after, PendingSnapshot)]
    assert isinstance(pending.after, PendingSnapshot) and pending.after.due_at is None, "B2: never booked"
    [found] = [f for f in outcome.result.simulation if f.incomplete]
    assert found.kind is HealthKind.RESPONDER_NEVER_ACTS
    assert (found.person, found.since, found.evidence) == ("nadia", START, [pending.seq])
    assert found.entity is not None and found.entity.kind is EntityKind.SERVICE_ITEM
    assert outcome.result.verdict.kind is VerdictKind.SIMULATION_INCOMPLETE
    assert outcome.result.verdict.on_what_happened is VerdictKind.NOT_JUDGED
    assert outcome.result.exit_code == 6
    assert outcome.result.verdict.words.startswith("Simulation incomplete: 1 thing")
    assert any(n.startswith("simulation: nadia is a responder of service orders") for n in outcome.result.notes)
    assert not any(f.check == "simulation" for f in outcome.result.findings), "kept apart from the agent's findings"
    read = query(
        open_model(tmp_path / "state", outcome.record.run_id),
        "SELECT kind, incomplete, person, entity_kind, since, evidence FROM simulation_health WHERE incomplete = 1",
    )
    assert [list(r) for r in read.rows] == [
        ["responder_never_acts", 1, "nadia", "service_item", "2026-08-24T09:00:00.000Z", f"[{pending.seq}]"]
    ]
    [verdict] = query(open_model(tmp_path / "state", outcome.record.run_id), "SELECT verdict FROM run").rows
    assert list(verdict) == ["simulation_incomplete"]
    transport = httpx.ASGITransport(app=create_app(tmp_path / "state"))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as viewer:
        answered = await viewer.get(f"/api/runs/{outcome.record.run_id}/findings")
    shown = FindingsResponse.model_validate_json(answered.content)
    assert [(h.kind, h.person) for h in shown.simulation if h.incomplete] == [
        (HealthKind.RESPONDER_NEVER_ACTS, "nadia")
    ]
    assert shown.verdict is not None and shown.verdict.kind is VerdictKind.SIMULATION_INCOMPLETE


async def test_a_responder_declared_silent_is_the_authors_word_and_leaves_the_run_whole(tmp_path: Path) -> None:
    outcome = await _play(tmp_path, _scenario({"kind": "silent"}))

    kinds = [(f.kind, f.person) for f in outcome.result.simulation]
    assert kinds == [(HealthKind.WAITS_BY_DECLARATION, "nadia"), (HealthKind.NEVER_EXERCISED, "owen")]
    assert not any(f.incomplete for f in outcome.result.simulation)
    assert outcome.result.verdict.kind is VerdictKind.NOT_JUDGED
