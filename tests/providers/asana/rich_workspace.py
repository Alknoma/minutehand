"""A workspace seeded with everything only Asana has: teams, custom fields of every kind, tags, a private
project, tokens, a refresh token and a throttled stretch, with the Status field as the status source.

It mirrors the fixtures of the emulator this provider replaces (Backend Services with Open through
Cancelled, Status, Priority and Story Points), written as a scenario rather than as files.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.provider import build
from minutehand.adapters.providers.asana.seed import AsanaSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, ProviderSeed, Scenario, SeededTicket, TicketState
from tests.providers.asana.asana_workspace import START, Workspace, served

AGENT_TOKEN = "pat-agent"
ALICE_TOKEN = "pat-alice"
REFRESH_TOKEN = "refresh-alice"
THROTTLED_AFTER = timedelta(days=2)
THROTTLED_FOR = timedelta(seconds=90)

STATUS = ["Open", "In Progress", "In Review", "Done", "Blocked", "Cancelled"]

SEED = AsanaSeed.model_validate(
    {
        "workspace": {"name": "Test Workspace", "organization": True, "premium": True},
        "teams": [{"name": "Design", "members": ["alice"], "agent": False}, {"name": "Engineering"}],
        "custom_fields": [
            {"name": "Status", "kind": "enum", "options": [{"name": n} for n in STATUS]},
            {
                "name": "Priority",
                "kind": "enum",
                "options": [{"name": n} for n in ["Critical", "High", "Medium", "Low"]],
            },
            {"name": "Story Points", "kind": "number", "precision": 1},
            {"name": "Platforms", "kind": "multi_enum", "options": [{"name": "iOS"}, {"name": "Web"}]},
            {"name": "Ticket", "kind": "text"},
            {"name": "Launch", "kind": "date"},
            {"name": "Reviewers", "kind": "people"},
            {"name": "Unused", "kind": "text"},
        ],
        "tags": ["performance", "production"],
        "projects": [
            {
                "name": "Backend Services",
                "team": "Engineering",
                "sections": [{"name": n} for n in STATUS],
                "custom_fields": ["Status", "Priority", "Story Points", "Platforms", "Ticket", "Launch", "Reviewers"],
            },
            {"name": "Private Roadmap", "team": "Design", "private": True, "members": ["alice"], "agent": False},
        ],
        "tasks": [
            {
                "ticket": "API timeout in production",
                "section": "In Progress",
                "tags": ["performance", "production"],
                "due_after": "P3D",
                "values": [
                    {"field": "Status", "option": "In Progress"},
                    {"field": "Priority", "option": "Critical"},
                    {"field": "Story Points", "number": 5},
                    {"field": "Platforms", "options": ["iOS", "Web"]},
                    {"field": "Ticket", "text": "INC-42"},
                    {"field": "Launch", "date_after": "P10D"},
                    {"field": "Reviewers", "people": ["alice", "bob"]},
                ],
                "comments": [{"person": "alice", "text": "Seen again at 9am.", "ago": "PT2H"}],
            },
            {"ticket": "Raise the pool size", "parent": "API timeout in production"},
        ],
        "status": {
            "kind": "custom_field",
            "field": "Status",
            "means": {"Open": "open", "In Progress": "open", "Done": "done", "Cancelled": "cancelled"},
        },
        "tokens": [
            {"token": AGENT_TOKEN},
            {"token": ALICE_TOKEN, "person": "alice"},
        ],
        "refresh_tokens": [{"refresh_token": REFRESH_TOKEN, "person": "alice"}],
        "rate_limits": [{"after": "P2D", "lasts": "PT90S"}],
    }
)

SCENARIO = Scenario(
    name="backend_triage",
    goal="Every production incident has an owner and a priority.",
    owner="sarah",
    starts_at=START,
    people=[
        Person(key="sarah", name="Sarah Williams", email="sarah.williams@company.com"),
        Person(key="alice", name="Alice Chen", email="alice.chen@company.com"),
        Person(key="bob", name="Bob Taylor", email="bob.taylor@company.com"),
    ],
    tickets=[
        SeededTicket(provider="asana", project="Backend Services", title="API timeout in production", assignee="bob"),
        SeededTicket(provider="asana", project="Backend Services", title="Raise the pool size"),
        SeededTicket(provider="asana", project="Backend Services", title="Old migration", state=TicketState.DONE),
        SeededTicket(provider="asana", project="Private Roadmap", title="Pricing page redesign", assignee="alice"),
    ],
    provider_seeds=[ProviderSeed(provider="asana", body=SEED.model_dump_json())],
)

WS = state.WORKSPACE_GID
BACKEND = state.project_gid("Backend Services")
ROADMAP = state.project_gid("Private Roadmap")
ENGINEERING = state.team_gid("Engineering")
DESIGN = state.team_gid("Design")
STATUS_FIELD = state.field_gid("Status")
PRIORITY = state.field_gid("Priority")
POINTS = state.field_gid("Story Points")
UNUSED = state.field_gid("Unused")
INCIDENT = state.task_gid(0)
SUBTASK = state.task_gid(1)
OLD = state.task_gid(2)
PRICING = state.task_gid(3)


def option(field: str, name: str) -> str:
    return state.option_gid(state.field_gid(field), name)


def section(name: str, project: str = BACKEND) -> str:
    return state.section_gid(project, STATUS.index(name))


@pytest.fixture
def rich(tmp_path: Path) -> Workspace:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO, store)
    return Workspace(provider=provider, store=store, clock=clock, path=tmp_path / "world.db")


def client_as(rich: Workspace, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=served(rich.provider, rich.store, rich.clock)),
        base_url="https://app.asana.com",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.fixture
async def agent(rich: Workspace) -> AsyncIterator[httpx.AsyncClient]:
    async with client_as(rich, AGENT_TOKEN) as c:
        yield c


def got(response: httpx.Response, status: int = 200) -> Any:
    """What `data` holds, after checking the status: Asana's JSON, read the way a client reads it."""
    assert response.status_code == status, response.text
    return response.json()["data"]
