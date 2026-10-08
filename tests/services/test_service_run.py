"""A declared service in a whole run (`session.play`): the agent files an order through the real proxy, the
service's timer moves it at its moment in the run loop's table, and the responder owed a move on it then has
nothing left to act on."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from minutehand import session
from minutehand.application.dues import due_entries
from minutehand.domain.agent import AgentUnderTest, Command
from minutehand.domain.clock import DueClosed, DueSource
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import Actor, PendingSnapshot, PendingStatus, TransitionSnapshot
from tests.e2e.support import world
from tests.support.people import people_model

START = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
AGENTS = Path(__file__).parent / "agents"
EXPIRING = {
    "initial": "placed",
    "states": ["placed", "approved", "expired"],
    "transitions": [
        {"name": "approve", "from": ["placed"], "to": "approved", "by": "person"},
        {"name": "expire", "from": ["placed"], "to": "expired", "by": "timer", "after": "PT1H"},
    ],
}


async def test_a_services_timer_fires_in_the_run_loop_and_takes_the_item_from_its_responder(tmp_path: Path) -> None:
    played = Scenario.model_validate(
        {
            "name": "orders",
            "goal": "Order a laptop.",
            "owner": "owen",
            "starts_at": START.isoformat(),
            "deadline_after": "P1D",
            "people": [
                {"key": "owen", "name": "Owen Hart", "email": "owen@example.com", "reply": {"kind": "silent"}},
                {"key": "nadia", "name": "Nadia Ek", "email": "nadia@example.com", "facts": ["I approve this"]},
            ],
            "services": [
                {
                    "host": "api.orders.example",
                    "name": "orders",
                    "responders": ["nadia"],
                    "within": {"min": "PT3H", "max": "PT3H"},
                    "machine": EXPIRING,
                }
            ],
        }
    )
    agent = AgentUnderTest(name="filer", wakes=[Command(argv=[sys.executable, str(AGENTS / "filer.py")])])
    [outcome] = await session.play(
        played, agent, state=tmp_path / "state", model=people_model(), listen=session.Listen(receive_telemetry=False)
    )

    held = world(tmp_path / "state", outcome.record.run_id)
    moves = [
        (t.name, e.actor, e.sim_time - START) for e in held.events() if isinstance(t := e.after, TransitionSnapshot)
    ]
    assert moves == [("create", Actor.AGENT, timedelta(0)), ("expire", Actor.TIMER, timedelta(hours=1))]
    [timer] = [d for d in due_entries(held) if d.source is DueSource.SERVICE]
    assert (timer.closed, timer.due.at) == (DueClosed.FIRED, START + timedelta(hours=1))
    waits = [e.after for e in held.events() if isinstance(e.after, PendingSnapshot)]
    assert [w.status for w in waits] == [PendingStatus.PENDING, PendingStatus.GONE], "expired before her moment"
