"""A seeded Asana workspace over a real `SqliteStore` and `RunClock`, driven through the ASGI app."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.provider import AsanaProvider, build
from minutehand.adapters.providers.asana.state import AsanaWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, Scenario, SeededTicket, TicketState

START = datetime(2026, 8, 24, 10, 50, 3, 250000, tzinfo=timezone.utc)
TOKEN = "simulated-personal-access-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

SCENARIO = Scenario(
    name="venue_handover",
    goal="Every handover task for the venue move has an owner who has finished it.",
    owner="iris",
    starts_at=START,
    people=[
        Person(key="iris", name="Iris Calder", email="iris@example.com"),
        Person(key="tomas", name="Tomas Brandt", email="tomas@example.com"),
        Person(key="noor", name="Noor Halvorsen", email="noor@example.com"),
    ],
    tickets=[
        SeededTicket(provider="asana", project="Venue Move", title="Book the freight lift", assignee="tomas"),
        SeededTicket(provider="asana", project="Venue Move", title="Return the old keys", assignee="noor",
                     state=TicketState.DONE),
        SeededTicket(provider="asana", project="Catering", title="Confirm the menu", body="Vegetarian count first."),
        SeededTicket(provider="tracker", project="Elsewhere", title="Not an asana ticket"),
    ],
)

VENUE = state.project_gid("Venue Move")
CATERING = state.project_gid("Catering")
WS = state.WORKSPACE_GID


@dataclass
class Workspace:
    provider: AsanaProvider
    store: SqliteStore
    clock: RunClock
    path: Path

    @property
    def asana(self) -> AsanaWorld:
        return AsanaWorld(self.store)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO, store)
    return Workspace(provider=provider, store=store, clock=clock, path=tmp_path / "world.db")


def client_for(provider: AsanaProvider, store: SqliteStore, clock: RunClock) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=provider.app(store, clock)),
                             base_url="https://app.asana.com", headers=AUTH)


@pytest.fixture
async def client(workspace: Workspace) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(workspace.provider, workspace.store, workspace.clock) as c:
        yield c


Answer = dict[str, object]


def body(response: httpx.Response, status: int = 200) -> Answer:
    assert response.status_code == status, response.text
    found = json.loads(response.text)
    assert isinstance(found, dict)
    return found


def data(response: httpx.Response, status: int = 200) -> dict[str, object]:
    found = body(response, status)["data"]
    assert isinstance(found, dict)
    return found


def items(response: httpx.Response) -> list[dict[str, object]]:
    found = body(response)["data"]
    assert isinstance(found, list)
    return found


def error(response: httpx.Response, status: int) -> str:
    """The message of a refusal, after checking Asana's envelope around it."""
    found = body(response, status)
    errors = found["errors"]
    assert isinstance(errors, list) and len(errors) == 1 and set(errors[0]) == {"message", "help"}
    message = errors[0]["message"]
    assert isinstance(message, str)
    return message


async def create(client: httpx.AsyncClient, **fields: object) -> dict[str, object]:
    return data(await client.post("/tasks", json={"data": fields}), 201)
