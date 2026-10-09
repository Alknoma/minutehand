"""A declared service in a standing world (docs/services.md): a call to its host is answered from the world's own
record of its items, its responders act through the people engine when the clock passes their moment, and every move
is listed on `GET /v1/worlds/{id}/transitions`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import requests

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed
from minutehand.domain.world import Actor, CaptureMode, PendingStatus
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served
from tests.serve.test_written_people import served_with
from tests.services.test_desk import APPROVAL
from tests.support.people import people_environment

START = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
REQUESTS = "https://api.approvals.example/v1/requests"


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    yield from served_with(people_environment(), tmp_path_factory)


def _agent(served: Served, token: str) -> requests.Session:
    """The agent's own HTTP client as Minutehand's environment configures it."""
    session = requests.Session()
    session.proxies = {"https": served.proxy}
    session.verify = served.bundle
    session.trust_env = False
    session.headers["authorization"] = f"Bearer {token}"
    return session


def _seed() -> Seed:
    return Seed.model_validate(
        {
            "starts_at": START.isoformat(),
            "people": [
                {"key": "owen", "name": "Owen Hart", "email": "owen@example.com", "reply": {"kind": "silent"}},
                {
                    "key": "nadia",
                    "name": "Nadia Ek",
                    "email": "nadia@example.com",
                    "facts": ["I approve this", "the Q3 budget covers it"],
                },
            ],
            "services": [
                {
                    "host": "api.approvals.example",
                    "name": "approvals",
                    "responders": ["nadia"],
                    "within": {"min": "PT1H", "max": "PT1H"},
                    "describe": "Purchase approvals.",
                    "ids": {"format": "prefixed", "prefix": "req_"},
                    "machine": APPROVAL,
                }
            ],
        }
    )


def test_a_declared_service_is_answered_from_its_state_and_its_responder_acts_when_the_clock_passes(
    written: Served,
) -> None:
    world = OpenWorld(
        written.client,
        written.client.create_world(
            CreateWorld(seed=_seed(), claims=Claims(tokens=["svc-token"]), scripted_people=True)
        ),
    )
    try:
        agent = _agent(written, "svc-token")
        filed = agent.post(REQUESTS, json={"po": "PO-7731"})
        assert filed.status_code == 201 and filed.json()["status"] == "pending"
        item = filed.json()["id"]

        [held] = world.transitions().items
        assert (held.person, held.state, held.status) == ("nadia", "pending", PendingStatus.PENDING)
        assert held.due_at == START + timedelta(hours=1), "the service's own window of her available time"

        advanced = world.advance(timedelta(hours=2))
        assert [f.what for f in advanced.fired] == [f"nadia acts on approvals {item}"]
        read = agent.get(f"{REQUESTS}/{item}")
        assert read.status_code == 200 and read.json()["status"] == "approved"
        moves = [(t.name, t.by, t.who) for t in world.transitions().transitions]
        assert moves == [("create", Actor.AGENT, None), ("approve", Actor.PERSON, "nadia")]
        captured = [c.exchange.captured for c in world.captured_calls()]
        assert {c.mode for c in captured if c is not None} == {CaptureMode.SERVICE}
        assert {c.declared_as for c in captured if c is not None} == {"api.approvals.example"}
    finally:
        world.close()
