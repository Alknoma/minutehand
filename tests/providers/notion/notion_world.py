"""A seeded Notion over a real `SqliteStore` and `RunClock`: driven directly over its ASGI app, or through the
real proxy over TLS with the official `notion-client` SDK, sync and async."""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from notion_client import AsyncClient, Client

from minutehand.adapters.providers.notion.manifest import MANIFEST
from minutehand.adapters.providers.notion.provider import NotionProvider, build
from minutehand.adapters.providers.notion.seed import object_id
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, ProviderSeed, Scenario, SeededDocument

START = datetime(2026, 9, 14, 8, 30, 0, tzinfo=UTC)
AGENT_TOKEN = "ntn_agent_secret_for_tests_0001"
OTHER_TOKEN = "ntn_other_secret_for_tests_0002"
READER_TOKEN = "ntn_reader_secret_for_tests_0003"
CLIENT_ID = "client-connector-0001"
CLIENT_SECRET = "secret_connector_client_0001"
CODE = "code-granted-by-dov-0001"
REDIRECT = "https://service.example.com/oauth/notion/callback"
API = "https://api.notion.com"
VERSION = {"Notion-Version": "2022-06-28"}


def ids(key: str) -> str:
    """The id the seed gives a page, database, row or integration of the `acme` workspace."""
    return object_id("acme", key)


def blocks_of_every_kind() -> list[dict[str, object]]:
    return [
        {"type": "heading_1", "text": "Welcome"},
        {"type": "paragraph", "text": "Read this first."},
        {"type": "bulleted_list_item", "text": "Be kind", "children": [{"type": "paragraph", "text": "Always."}]},
        {"type": "numbered_list_item", "text": "Ask early"},
        {"type": "to_do", "text": "Sign the policy", "checked": True},
        {"type": "toggle", "text": "Details", "children": [{"type": "paragraph", "text": "Hidden inside."}]},
        {"type": "code", "text": "print('hi')", "language": "python"},
        {"type": "quote", "text": "Ship small."},
        {"type": "callout", "text": "Mind the gap", "emoji": "!"},
        {"type": "divider"},
        {"type": "table", "rows": [["Team", "Lead"], ["Web", "Mara"]], "header": True},
        {"type": "bookmark", "url": "https://example.com/guide"},
        {"type": "link_preview", "url": "https://example.com/preview"},
        {"type": "image", "url": "https://example.com/logo.png"},
    ]


NOTION: dict[str, object] = {
    "workspaces": [
        {
            "key": "acme",
            "name": "Acme",
            "integrations": [
                {"key": "agent", "name": "Planning bot", "tokens": [AGENT_TOKEN], "shared": ["handbook"]},
                {"key": "other", "name": "Other bot", "tokens": [OTHER_TOKEN], "shared": ["private"]},
                {
                    "key": "reader",
                    "name": "Reader",
                    "tokens": [READER_TOKEN],
                    "capabilities": ["read_content"],
                    "shared": ["handbook"],
                },
                {
                    "key": "connector",
                    "name": "Connector",
                    "type": "public",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uris": [REDIRECT],
                    "installed_by": "dov",
                    "authorizations": [{"code": CODE, "redirect_uri": REDIRECT}],
                    "shared": ["handbook"],
                },
            ],
            "pages": [
                {"key": "handbook", "title": "Team Handbook", "blocks": blocks_of_every_kind()},
                {
                    "key": "onboarding",
                    "title": "Onboarding",
                    "parent": "handbook",
                    "blocks": [{"type": "paragraph", "text": f"Step {n}"} for n in range(150)],
                },
                {"key": "private", "title": "Salary Bands", "blocks": [{"type": "paragraph", "text": "Confidential"}]},
                {"key": "private_child", "title": "Band Notes", "parent": "private"},
            ],
            "databases": [
                {
                    "key": "projects",
                    "title": "Projects",
                    "parent": "handbook",
                    "properties": [
                        {"name": "Name", "type": "title"},
                        {"name": "Status", "type": "status", "choices": ["Not started", "In progress", "Done"]},
                        {"name": "Priority", "type": "select", "choices": ["Low", "High"]},
                        {"name": "Tags", "type": "multi_select", "choices": ["ops", "web"]},
                        {"name": "Due", "type": "date"},
                        {"name": "Owner", "type": "people"},
                        {"name": "Signed off", "type": "checkbox"},
                        {"name": "Estimate", "type": "number"},
                        {"name": "Link", "type": "url"},
                        {"name": "Notes", "type": "rich_text"},
                        {"name": "Depends on", "type": "relation", "relates_to": "projects"},
                        {"name": "Created", "type": "created_time"},
                    ],
                    "rows": [
                        {
                            "key": "launch",
                            "values": {
                                "Name": "Launch site",
                                "Status": "In progress",
                                "Priority": "High",
                                "Tags": ["web"],
                                "Due": "2026-09-20",
                                "Owner": ["mara"],
                                "Signed off": False,
                                "Estimate": 5,
                                "Link": "https://example.com/launch",
                                "Notes": "Ship it",
                                "Depends on": ["audit"],
                            },
                        },
                        {
                            "key": "audit",
                            "values": {
                                "Name": "Security audit",
                                "Status": "Not started",
                                "Priority": "Low",
                                "Tags": ["ops"],
                                "Due": "2026-10-01",
                                "Owner": ["dov"],
                                "Signed off": True,
                                "Estimate": 2,
                            },
                        },
                    ],
                }
            ],
        }
    ]
}

MARA = Person(key="mara", name="Mara Lindqvist", email="mara@example.com")
DOV = Person(key="dov", name="Dov Aranha", email="dov@example.com")


def scenario(notion: dict[str, object] | None = None, documents: list[SeededDocument] | None = None) -> Scenario:
    return Scenario(
        name="handbook_upkeep",
        goal="The handbook is current.",
        owner="mara",
        starts_at=START,
        people=[MARA, DOV],
        documents=documents or [],
        provider_seeds=[ProviderSeed(provider="notion", body=json.dumps(notion if notion is not None else NOTION))],
    )


@dataclass
class World:
    provider: NotionProvider
    store: SqliteStore
    clock: RunClock


def seeded(tmp_path: Path, played: Scenario | None = None) -> World:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(played or scenario(), store)
    return World(provider=provider, store=store, clock=clock)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    found = seeded(tmp_path)
    try:
        yield found
    finally:
        found.store.close()


def direct(world: World, token: str | None = AGENT_TOKEN) -> httpx.AsyncClient:
    """The app over ASGI, with the SDK's headers."""
    headers = {**VERSION, **({"Authorization": f"Bearer {token}"} if token else {})}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.provider.app(world.store, world.clock)), base_url=API, headers=headers
    )


@pytest.fixture
async def api(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with direct(world) as client:
        yield client


Answer = dict[str, Any]


def answer(response: httpx.Response, status: int = 200) -> Answer:
    assert response.status_code == status, response.text
    found = json.loads(response.text)
    assert isinstance(found, dict)
    return found


def refusal(response: httpx.Response, status: int, code: str) -> str:
    """The message of a Notion error, after checking the envelope is Notion's."""
    body = answer(response, status)
    assert body["object"] == "error" and body["status"] == status and body["code"] == code, body
    assert isinstance(body["request_id"], str) and isinstance(body["message"], str)
    return body["message"]


# --------------------------------------------------------------------------- through the proxy


Call = Callable[[Any], Any]


@dataclass
class Sdk:
    """The official SDK as a service builds it, through the proxy, sync (run off the loop) or async."""

    flavour: str
    sync: Client
    asynchronous: AsyncClient
    proxy: Proxy
    store: SqliteStore
    clock: RunClock

    async def __call__(self, call: Call) -> Any:
        if self.flavour == "sync":
            return await asyncio.to_thread(call, self.sync)
        found = call(self.asynchronous)
        assert isinstance(found, Awaitable)
        return await found

    def other(self, token: str) -> tuple[Client, AsyncClient]:
        trust = ssl.create_default_context(cafile=str(self.proxy.ca_cert))
        return (
            Client(auth=token, client=httpx.Client(proxy=self.proxy.url, verify=trust, trust_env=False)),
            AsyncClient(auth=token, client=httpx.AsyncClient(proxy=self.proxy.url, verify=trust, trust_env=False)),
        )

    async def as_token(self, token: str, call: Call) -> Any:
        sync, asynchronous = self.other(token)
        try:
            if self.flavour == "sync":
                return await asyncio.to_thread(call, sync)
            found = call(asynchronous)
            assert isinstance(found, Awaitable)
            return await found
        finally:
            sync.close()
            await asynchronous.aclose()


async def through_proxy(tmp_path: Path, world: World, flavour: str) -> AsyncIterator[Sdk]:
    registry = Registry()
    registry.register(MANIFEST, lambda: world.provider)
    async with Proxy(Routing(registry), world.store, world.clock, confdir=tmp_path / "ca") as proxy:
        trust = ssl.create_default_context(cafile=str(proxy.ca_cert))
        sync = Client(auth=AGENT_TOKEN, client=httpx.Client(proxy=proxy.url, verify=trust, trust_env=False))
        asynchronous = AsyncClient(
            auth=AGENT_TOKEN, client=httpx.AsyncClient(proxy=proxy.url, verify=trust, trust_env=False)
        )
        try:
            yield Sdk(
                flavour=flavour, sync=sync, asynchronous=asynchronous, proxy=proxy, store=world.store, clock=world.clock
            )
        finally:
            sync.close()
            await asynchronous.aclose()


@pytest.fixture(params=["sync", "async"])
async def sdk(request: pytest.FixtureRequest, tmp_path: Path, world: World) -> AsyncIterator[Sdk]:
    async for found in through_proxy(tmp_path, world, str(request.param)):
        yield found


@pytest.fixture
async def async_sdk(tmp_path: Path, world: World) -> AsyncIterator[Sdk]:
    async for found in through_proxy(tmp_path, world, "async"):
        yield found
