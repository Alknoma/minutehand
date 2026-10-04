"""A seeded Slack workspace over a real `SqliteStore` and `RunClock`, driven through the ASGI app."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.slack import state, wire
from minutehand.adapters.providers.slack.provider import SlackProvider, build
from minutehand.adapters.providers.slack.state import BOT_USER_ID, SlackWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, Scenario, Silent, WorkingHours
from minutehand.domain.world import Actor, Operation

START = datetime(2026, 8, 24, 10, 50, 3, tzinfo=UTC)
TOKEN = "xoxb-simulated-run-token"

SCENARIO = Scenario(
    name="launch_checklist",
    goal="The launch checklist is signed off by everyone on it.",
    owner="iris",
    starts_at=START,
    people=[
        Person(key="iris", name="Iris Calder", email="iris@example.com", title="Programme lead"),
        Person(
            key="tomas",
            name="Tomas Brandt",
            email="tomas@example.com",
            working_hours=WorkingHours(timezone="Europe/Lisbon"),
        ),
        Person(key="noor", name="Noor Halvorsen", email="noor@example.com", reply=Silent()),
    ],
)


@dataclass
class Workspace:
    provider: SlackProvider
    store: SqliteStore
    clock: RunClock
    path: Path

    @property
    def slack(self) -> SlackWorld:
        return SlackWorld(self.store)

    def dm(self, person: str) -> str:
        return state.conversation_id([BOT_USER_ID, state.user_id(person)])

    def channel_without_the_app(self, name: str, *, members: list[str]) -> str:
        """A public channel the scenario made and never invited the app to."""
        channel = wire.SlackChannel(
            id=state.named_channel_id(name),
            name=name,
            is_channel=True,
            created=int(START.timestamp()),
            creator=state.user_id(members[0]),
        )
        self.slack.write(
            state.channel_ref(channel.id),
            channel,
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=state.TEAM_ID,
        )
        for key in members:
            uid = state.user_id(key)
            self.slack.write(
                state.membership_ref(channel.id, uid),
                wire.SlackMembership(channel=channel.id, user=uid),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                parent=channel.id,
            )
        return channel.id

    def public_channel(self, name: str, *, archived: bool = False) -> str:
        """A public channel the app is in."""
        channel = wire.SlackChannel(
            id=state.named_channel_id(name),
            name=name,
            is_channel=True,
            is_archived=archived,
            created=int(START.timestamp()),
            creator=BOT_USER_ID,
        )
        self.slack.write(
            state.channel_ref(channel.id),
            channel,
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=state.TEAM_ID,
        )
        self.slack.write(
            state.membership_ref(channel.id, BOT_USER_ID),
            wire.SlackMembership(channel=channel.id, user=BOT_USER_ID),
            operation=Operation.CREATE,
            actor=Actor.SCENARIO,
            parent=channel.id,
        )
        return channel.id


GENERAL = state.named_channel_id(state.GENERAL)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO, store)
    return Workspace(provider=provider, store=store, clock=clock, path=tmp_path / "world.db")


def client_for(provider: SlackProvider, store: SqliteStore, clock: RunClock) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider.app(store, clock)), base_url="https://slack.com"
    )


@pytest.fixture
async def client(workspace: Workspace) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(workspace.provider, workspace.store, workspace.clock) as c:
        yield c


Answer = dict[str, object]


async def form(client: httpx.AsyncClient, method: str, token: str | None = TOKEN, **params: str) -> Answer:
    """A method the way slack_sdk sends `params=`: a form-encoded POST body."""
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    response = await client.post(f"/api/{method}", data=params, headers=headers)
    assert response.status_code == 200
    return json.loads(response.text)


async def body(client: httpx.AsyncClient, method: str, token: str = TOKEN, **payload: object) -> Answer:
    """A method the way slack_sdk sends `json=`."""
    response = await client.post(f"/api/{method}", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return json.loads(response.text)


def messages_of(answer: Answer) -> list[dict[str, object]]:
    found = answer["messages"]
    assert isinstance(found, list)
    return found


def text_of(answer: Answer) -> list[object]:
    return [m["text"] for m in messages_of(answer)]
