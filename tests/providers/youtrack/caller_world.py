"""An instance shaped like a team's real one, reached through the run's proxy as an agent reaches it.

Two projects with different vocabularies (Field Ops names its priorities `P0 - Outage` and the like), seeded field
values, tags, a comment, a link, an issue with history before the run, tokens for the agent and for Tomas, and a
Hub service that may ask for a token.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.youtrack.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import (
    Person,
    ProviderSeed,
    Scenario,
    SeededComment,
    SeededTicket,
    TicketState,
)
from tests.providers.youtrack.youtrack_instance import START, TOKEN, Instance, proxied

TOMAS_TOKEN = "perm:dG9tYXM=.c2Vjb25k.dG9rZW4="
CLIENT_ID = "planning-service"
CLIENT_SECRET = "s3cr3t-planning"

YOUTRACK_SEED = {
    "users": [{"login": "vendor", "name": "Visiting Vendor", "email": "vendor@example.net"}],
    "tokens": [{"token": TOKEN, "login": "agent-bot"}, {"token": TOMAS_TOKEN, "login": "tomas"}],
    "services": [{"client_id": CLIENT_ID, "secret": CLIENT_SECRET, "login": "agent-bot"}],
    "fields": [{"name": "Sprint", "type": "version[1]"}],
    "projects": [
        {
            "name": "Launch",
            "team": ["iris", "tomas", "noor"],
            "fields": [
                {"name": "Priority"},
                {"name": "Type"},
                {"name": "State"},
                {"name": "Assignee"},
                {"name": "Due Date"},
                {"name": "Estimation"},
                {"name": "Spent time"},
                {"name": "Story Points"},
                {"name": "Sprint", "values": [{"name": "Sprint 1"}, {"name": "Sprint 2"}]},
            ],
        },
        {
            "name": "Field Ops",
            "short_name": "OPS",
            "team": ["iris", "tomas", "noor"],
            "fields": [
                {
                    "name": "Priority",
                    "values": [{"name": "P0 - Outage"}, {"name": "P1 - Urgent"}, {"name": "P2 - Normal"}],
                    "default": "P2 - Normal",
                },
                {"name": "Type"},
                {
                    "name": "State",
                    "values": [
                        {"name": "Backlog"},
                        {"name": "Doing"},
                        {"name": "Shipped", "resolved": True},
                        {"name": "Dropped", "resolved": True, "means": "cancelled"},
                    ],
                    "default": "Backlog",
                },
                {"name": "Assignee"},
            ],
        },
    ],
    "issues": [
        {
            "ticket": "notes",
            "fields": [
                {"field": "Priority", "value": "Critical"},
                {"field": "Type", "value": "Task"},
                {"field": "Due Date", "value": "2026-08-26"},
                {"field": "Story Points", "value": 3},
                {"field": "Estimation", "value": "1d 2h"},
            ],
        },
        {
            "ticket": "venue",
            "fields": [
                {"field": "Priority", "value": "Normal"},
                {"field": "Type", "value": "Feature"},
                {"field": "Due Date", "value": "2026-09-02"},
            ],
        },
        {
            "ticket": "keynote",
            "created_ago": "P3D",
            "fields": [{"field": "Priority", "value": "Major"}, {"field": "Type", "value": "Task"}],
            "links": [{"phrase": "subtask of", "ticket": "notes"}],
            "history": [
                {"ago": "P2D", "by": "tomas", "fields": [{"field": "State", "value": "In Progress"}]},
                {"ago": "P1D", "by": "iris", "fields": [{"field": "Priority", "value": "Show-stopper"}]},
            ],
        },
        {"ticket": "kits", "fields": [{"field": "Priority", "value": "P0 - Outage"}]},
    ],
}

SCENARIO = Scenario(
    name="launch_week",
    goal="Every launch task has an owner and is closed out.",
    owner="iris",
    starts_at=START,
    people=[
        Person(key="iris", name="Iris Calder", email="iris@example.com"),
        Person(key="tomas", name="Tomas Brandt", email="tomas@example.com"),
        Person(key="noor", name="Noor Halvorsen", email="noor@example.com"),
    ],
    tickets=[
        SeededTicket(
            key="notes",
            provider="youtrack",
            project="Launch",
            title="Write the release notes",
            body="Covers the pricing change",
            assignee="tomas",
            labels=["docs"],
            comments=[SeededComment(by="iris", text="Due before the partner call")],
        ),
        SeededTicket(
            key="venue",
            provider="youtrack",
            project="Launch",
            title="Book the venue",
            body="Forty seats",
            assignee="noor",
            state=TicketState.DONE,
            labels=["venue", "big room"],
        ),
        SeededTicket(key="keynote", provider="youtrack", project="Launch", title="Draft the keynote", assignee="iris"),
        SeededTicket(key="kits", provider="youtrack", project="Field Ops", title="Ship the demo kits"),
    ],
    provider_seeds=[ProviderSeed(provider="youtrack", text=json.dumps(YOUTRACK_SEED))],
)


def scenario_with(**changes: object) -> Scenario:
    """The scenario with its YouTrack seed changed: `changes` replace top-level keys of the seed."""
    seed = {**YOUTRACK_SEED, **changes}
    return SCENARIO.model_copy(update={"provider_seeds": [ProviderSeed(provider="youtrack", text=json.dumps(seed))]})


def seeded(tmp_path: Path, scenario: Scenario = SCENARIO) -> Instance:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(scenario, store)
    return Instance(provider=provider, store=store, clock=clock, path=tmp_path / "world.db")


@pytest.fixture
def team(tmp_path: Path) -> Instance:
    return seeded(tmp_path)


@pytest.fixture
async def yt(team: Instance, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The agent's client: its token, through the proxy, at the instance's host."""
    async with proxied(team, tmp_path / "ca") as http:
        yield http


def as_user(token: str) -> dict[str, str]:
    """Headers that make one call as another token's user."""
    return {"Authorization": f"Bearer {token}"}
