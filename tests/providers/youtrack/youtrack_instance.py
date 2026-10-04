"""A seeded YouTrack instance over a real `SqliteStore` and `RunClock`, driven through the ASGI app."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.youtrack import state, wire
from minutehand.adapters.providers.youtrack.provider import YouTrackProvider, build
from minutehand.adapters.providers.youtrack.state import YouTrackWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, Scenario, SeededTicket, TicketState
from minutehand.domain.world import Actor

START = datetime(2026, 8, 24, 10, 50, 3, tzinfo=UTC)
TOKEN = "perm:c2ltdWxhdGVk.cnVu.dG9rZW4="
HOST = "https://lanternworks.youtrack.cloud"

SCENARIO = Scenario(
    name="launch_checklist",
    goal="Every launch task has an owner and is closed out.",
    owner="iris",
    starts_at=START,
    people=[
        Person(key="iris", name="Iris Calder", email="iris@example.com", title="Programme lead"),
        Person(key="tomas", name="Tomas Brandt", email="tomas@example.com"),
        Person(key="noor", name="Noor Halvorsen", email="noor@example.com"),
    ],
    tickets=[
        SeededTicket(provider="youtrack", project="Launch", title="Write the release notes", assignee="tomas"),
        SeededTicket(
            provider="youtrack",
            project="Launch",
            title="Book the venue",
            body="Forty seats",
            assignee="noor",
            state=TicketState.DONE,
        ),
        SeededTicket(provider="youtrack", project="Field Ops", title="Ship the demo kits"),
        SeededTicket(provider="asana", project="Launch", title="Not a YouTrack task"),
    ],
)

LAUNCH = "0-0"
FIELD_OPS = "0-1"
AGENT_ID = "1-0"
IRIS, TOMAS, NOOR = "1-1", "1-2", "1-3"


@dataclass
class Instance:
    provider: YouTrackProvider
    store: SqliteStore
    clock: RunClock
    path: Path

    @property
    def youtrack(self) -> YouTrackWorld:
        return YouTrackWorld(self.store)

    def outsider(self) -> wire.StoredUser:
        """A user the instance has who is on no project's team."""
        user = wire.StoredUser(
            id="1-99",
            login="vendor",
            fullName="Visiting Vendor",
            email="vendor@example.net",
            ringId="00000000-0000-4000-8000-000000000099",
        )
        self.youtrack.write_user(user, actor=Actor.SCENARIO)
        return user


@pytest.fixture
def instance(tmp_path: Path) -> Instance:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO, store)
    return Instance(provider=provider, store=store, clock=clock, path=tmp_path / "world.db")


def client_for(
    provider: YouTrackProvider,
    store: SqliteStore,
    clock: RunClock,
    base_url: str = HOST,
    token: str | None = TOKEN,
) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider.app(store, clock)),
        base_url=base_url,
        headers=headers,
    )


@pytest.fixture
async def client(instance: Instance) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(instance.provider, instance.store, instance.clock) as c:
        yield c


Json = dict[str, object]


def entity(response: httpx.Response, status: int = 200) -> Json:
    assert response.status_code == status, response.text
    found = json.loads(response.text)
    assert isinstance(found, dict)
    return found


def entities(response: httpx.Response) -> list[Json]:
    assert response.status_code == 200, response.text
    found = json.loads(response.text)
    assert isinstance(found, list)
    return found


def refusal(response: httpx.Response, status: int) -> Json:
    assert response.status_code == status, response.text
    found = entity(response, status)
    assert set(found) >= {"error", "error_description"}, found
    return found


async def create(
    client: httpx.AsyncClient, summary: str, *, project: str = LAUNCH, fields: str = "id,idReadable", **extra: object
) -> Json:
    payload: Json = {"project": {"id": project}, "summary": summary, **extra}
    return entity(await client.post("/api/issues", params={"fields": fields}, json=payload))


def readable_ids(issues: list[Json]) -> list[object]:
    return [i["idReadable"] for i in issues]


def state_field(name: str) -> Json:
    return {"name": "State", "$type": "StateIssueCustomField", "value": {"name": name}}


def assignee_field(login: str | None) -> Json:
    value: Json | None = None if login is None else {"login": login}
    return {"name": "Assignee", "$type": "SingleUserIssueCustomField", "value": value}


def millis_now(clock: RunClock) -> int:
    return state.millis(clock.now())
