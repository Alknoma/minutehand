"""A seeded Jira site behind the real proxy, over TLS: a client reaches `lanternworks.atlassian.net` and
`api.atlassian.com` exactly as it would in production, with no base URL changed."""

from __future__ import annotations

import base64
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from minutehand.adapters.providers.jira.manifest import MANIFEST
from minutehand.adapters.providers.jira.provider import JiraProvider, build
from minutehand.adapters.providers.jira.seed import account_id
from minutehand.adapters.providers.jira.state import JiraWorld
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Scenario

START = datetime(2026, 8, 24, 10, 50, 3, tzinfo=UTC)
SITE = "https://lanternworks.atlassian.net"
API = f"{SITE}/rest/api/3"
AGILE = f"{SITE}/rest/agile/1.0"
AGENT_EMAIL = "agent@lanternworks.example"
AGENT_TOKEN = "ATATT3x-agent-token-7f3a"
IRIS_TOKEN = "ATATT3x-iris-token-1c2d"
OAUTH_ACCESS = "oauth-access-iris-0001"
OAUTH_REFRESH = "oauth-refresh-iris-0001"
CLIENT_ID = "lantern-client"
CLIENT_SECRET = "lantern-secret"
CLOUD_ID = "4f0e8a63-2d0f-4c55-9a21-6a1d1b2c3e4f"

AGENT = account_id(AGENT_EMAIL)
IRIS = account_id("iris@example.com")
TOMAS = account_id("tomas@example.com")
NOOR = account_id("noor@example.com")

JIRA_SEED: dict[str, Any] = {
    "site": "lanternworks",
    "cloud_id": CLOUD_ID,
    "agent_email": AGENT_EMAIL,
    "agent_name": "Lantern Agent",
    "accounts": [
        {"key": "automation", "name": "Automation for Jira", "kind": "app"},
        {"key": "former", "name": "Former Colleague", "email": "former@example.com", "active": False},
        {"key": "portal", "name": "Portal Customer", "email": "portal@example.com", "kind": "customer"},
        {"key": "quiet", "name": "Quiet Person", "email": "quiet@example.com", "email_visible": False},
    ],
    "credentials": [
        {"account": "agent", "api_token": AGENT_TOKEN},
        {"account": "iris", "api_token": IRIS_TOKEN},
        {
            "account": "iris",
            "oauth": {
                "access_token": OAUTH_ACCESS,
                "refresh_token": OAUTH_REFRESH,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        },
    ],
    "fields": [{"id": "customfield_10050", "name": "Team", "kind": "select", "options": ["Platform", "Field"]}],
    "projects": [
        {
            "name": "Launch",
            "key": "LAUNCH",
            "lead": "iris",
            "statuses": [
                {"name": "To Do", "category": "new"},
                {"name": "In Progress", "category": "indeterminate"},
                {"name": "In Review", "category": "indeterminate"},
                {"name": "Done", "category": "done"},
                {"name": "Won't Do", "category": "done", "outcome": "cancelled"},
            ],
            "transitions": [
                {"id": "11", "name": "Start work", "to": "In Progress", "sources": ["To Do"]},
                {"id": "21", "name": "Submit for review", "to": "In Review", "sources": ["In Progress"]},
                {"id": "31", "name": "Approve", "to": "Done", "sources": ["In Review"], "screen": ["resolution"]},
                {"id": "41", "name": "Back to work", "to": "In Progress", "sources": ["In Review", "Done"]},
                {"id": "51", "name": "Drop", "to": "Won't Do"},
            ],
            "boards": [
                {
                    "name": "Launch board",
                    "sprints": [
                        {"name": "Launch 1", "state": "closed"},
                        {"name": "Launch 2", "state": "active", "goal": "Ship the beta"},
                        {"name": "Launch 3", "state": "future"},
                    ],
                }
            ],
        },
        {"name": "Field Ops", "key": "FIELD"},
        {"name": "Vault", "key": "VAULT", "lead": "iris", "administrators": ["iris"], "members": ["iris"]},
    ],
    "issues": [
        {
            "ticket": "notes",
            "issue_type": "Story",
            "priority": "High",
            "due": "2026-08-28",
            "sprint": "Launch 2",
            "estimate_seconds": 28800,
            "spent_seconds": 10800,
            "fields": [{"field": "Team", "value": "Platform"}, {"field": "customfield_10016", "value": 5}],
            "comments": [{"by": "iris", "text": "Draft is in the shared folder.", "at": "-P1D"}],
            "history": [{"by": "iris", "at": "-P2D", "field": "priority", "from": "Medium", "to": "High"}],
            "links": [{"type": "Blocks", "to": "venue"}],
        },
        {"ticket": "venue", "created": "-P3D"},
        {"ticket": "kits", "issue_type": "Bug", "priority": "Lowest"},
        {"ticket": "vault", "status": "In Progress"},
    ],
    "rate_limits": [{"method": "POST", "path": "/rest/api/3/issue/LAUNCH-2/comment", "times": 1, "retry_after": 7}],
}

SCENARIO = Scenario.model_validate(
    {
        "name": "launch_checklist",
        "goal": "Every launch task has an owner and is closed out.",
        "owner": "iris",
        "starts_at": START,
        "people": [
            {"key": "iris", "name": "Iris Calder", "email": "iris@example.com"},
            {"key": "tomas", "name": "Tomas Brandt", "email": "tomas@example.com"},
            {"key": "noor", "name": "Noor Halvorsen", "email": "noor@example.com"},
        ],
        "tickets": [
            {"key": "notes", "provider": "jira", "project": "Launch", "title": "Write the release notes", "assignee": "tomas", "labels": ["docs", "beta"],
             "body": "Cover the new export.\n\nLink the changelog."},
            {"key": "venue", "provider": "jira", "project": "Launch", "title": "Book the venue", "assignee": "noor", "state": "done"},
            {"key": "kits", "provider": "jira", "project": "Field Ops", "title": "Ship the demo kits"},
            {"key": "vault", "provider": "jira", "project": "Vault", "title": "Rotate the vault keys", "assignee": "iris"},
            {"provider": "asana", "project": "Launch", "title": "Not a Jira issue"},
        ],
        "provider_seeds": [{"provider": "jira", "body": JIRA_SEED}],
    }
)  # fmt: skip


def basic(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


@dataclass
class Site:
    provider: JiraProvider
    store: SqliteStore
    clock: RunClock
    http: httpx.AsyncClient
    """Signed in as the agent, with Basic, at the site's own host."""
    proxy: Proxy

    @property
    def jira(self) -> JiraWorld:
        return JiraWorld(self.store)

    def client(self, authorization: str | None) -> httpx.AsyncClient:
        """Another client through the same proxy, with its own Authorization (or none)."""
        headers = {"Accept": "application/json"}
        if authorization is not None:
            headers["Authorization"] = authorization
        return httpx.AsyncClient(
            proxy=self.proxy.url,
            verify=ssl.create_default_context(cafile=str(self.proxy.ca_cert)),
            trust_env=False,
            headers=headers,
            timeout=30,
        )


@pytest.fixture
async def site(tmp_path: Path) -> AsyncIterator[Site]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO, store)
    registry = Registry()
    registry.register(MANIFEST, lambda: provider)
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        found = Site(provider=provider, store=store, clock=clock, http=httpx.AsyncClient(), proxy=proxy)
        async with found.client(basic(AGENT_EMAIL, AGENT_TOKEN)) as http:
            found.http = http
            yield found


def ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, f"{response.request.method} {response.request.url}: {response.text}"
    return response.json() if response.content else None


def refused(response: httpx.Response, status: int) -> dict[str, Any]:
    """Jira's error body: `errorMessages` and `errors`, and nothing else."""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"errorMessages", "errors"}, body
    return body


async def key_of(site: Site, summary: str) -> str:
    found = ok(
        await site.http.post(f"{API}/search/jql", json={"jql": f'summary ~ "\\"{summary}\\""', "fields": ["summary"]})
    )
    keys = [i["key"] for i in found["issues"] if i["fields"]["summary"] == summary]
    assert len(keys) == 1, found
    return keys[0]
