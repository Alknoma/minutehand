"""Notion's integration webhooks: a seeded subscription is verified first, then sent every change its integration
can see, by a person or by the integration itself, each signed with the verification token."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from minutehand.adapters.providers.notion.app import NotionApp, build_app
from minutehand.adapters.providers.notion.seed import object_id
from minutehand.domain.scenario import Commented, DocumentAction, DocumentHappening, Edited, FieldSet, Trashed
from minutehand.ports.provider import ChangesDocuments, NotifiesChanges
from tests.providers.notion.notion_world import API, NOTION, START, VERSION, World, ids, scenario, seeded

TOKEN = "secret_" + "w" * 43
"""A verification token in Notion's documented shape; it keys every signature."""


@dataclass
class Subscriber:
    """The integration's webhook endpoint: it answers the verification request, and records each event."""

    url: str = ""
    verifying: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    signatures: list[str] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)
    refuse_verification: bool = False

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        parsed = json.loads(body)
        if "verification_token" in parsed:
            self.verifying.append(parsed)
            if self.refuse_verification:
                return Response(status_code=500)
            return PlainTextResponse(parsed["verification_token"])
        self.events.append(parsed)
        self.signatures.append(request.headers["x-notion-signature"])
        self.bodies.append(body)
        return Response(status_code=200)

    def types(self) -> list[tuple[str, str]]:
        return [(e["type"], e["entity"]["id"]) for e in self.events]


@pytest.fixture
async def subscriber() -> AsyncIterator[Subscriber]:
    endpoint = Subscriber()
    app = Starlette(routes=[Route("/notion", endpoint.handle, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    endpoint.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/notion"
    yield endpoint
    server.should_exit = True
    await serving


def _subscribed(url: str, *, events: list[str] | None = None, verified: bool = False) -> dict[str, object]:
    notion = json.loads(json.dumps(NOTION))
    hook: dict[str, object] = {"integration": "agent", "url": url, "verification_token": TOKEN, "verified": verified}
    if events is not None:
        hook["events"] = events
    notion["webhooks"] = [hook]
    return notion


@pytest.fixture
def watched(tmp_path: Path, subscriber: Subscriber) -> Iterator[World]:
    world = seeded(tmp_path, scenario(_subscribed(subscriber.url)))
    try:
        yield world
    finally:
        world.store.close()


async def _person(world: World, notion: dict[str, object], person: str, document: str, action: DocumentAction) -> None:
    happening = DocumentHappening(person=person, document=document, after=timedelta(hours=1), action=action)
    world.provider.change(happening, scenario(notion), world.store, world.clock)
    assert world.provider.watched(world.store, world.clock)
    await world.provider.notify(world.store, world.clock)


def _signed(body: bytes) -> str:
    return "sha256=" + hmac.new(TOKEN.encode(), body, hashlib.sha256).hexdigest()


def test_the_provider_changes_documents_and_notifies_changes() -> None:
    from minutehand.adapters.providers.notion.provider import build

    provider = build()
    assert isinstance(provider, ChangesDocuments) and isinstance(provider, NotifiesChanges)


async def test_a_persons_edit_is_verified_for_then_sent_signed_with_who_made_it(
    watched: World, subscriber: Subscriber
) -> None:
    watched.clock.jump(START + timedelta(hours=1))
    await _person(watched, _subscribed(subscriber.url), "dov", "Team Handbook", Edited(append="New rule."))

    assert subscriber.verifying == [{"verification_token": TOKEN}]
    [event] = subscriber.events
    assert subscriber.signatures == [_signed(subscriber.bodies[0])]
    dov = next(u for u in watched_users(watched) if u.email == "dov@example.com")
    assert event["type"] == "page.content_updated"
    assert event["entity"] == {"id": ids("handbook"), "type": "page"}
    assert event["authors"] == [{"id": dov.id, "type": "person"}]
    assert event["timestamp"] == "2026-09-14T09:30:00.000Z"
    assert event["workspace_id"] == ids("acme") and event["workspace_name"] == "Acme"
    assert event["integration_id"] == ids("agent") and event["attempt_number"] == 1
    assert event["data"]["parent"] == {"id": ids("acme"), "type": "space"}
    assert len(event["data"]["updated_blocks"]) == 1

    await watched.provider.notify(watched.store, watched.clock)
    assert len(subscriber.events) == 1 and len(subscriber.verifying) == 1, "each event once; verified once"


async def test_a_row_field_set_and_a_page_trashed_are_properties_updated_and_deleted(
    watched: World, subscriber: Subscriber
) -> None:
    notion = _subscribed(subscriber.url)
    await _person(watched, notion, "mara", "Launch site", FieldSet(field="Status", value="Done"))
    await _person(watched, notion, "mara", "Onboarding", Trashed())
    assert subscriber.types() == [
        ("page.properties_updated", ids("launch")),
        ("page.deleted", ids("onboarding")),
    ]
    properties = subscriber.events[0]["data"]
    assert properties["parent"] == {"id": ids("projects"), "type": "database"}
    assert len(properties["updated_properties"]) == 1


async def test_the_integrations_own_writes_are_sent_too_authored_by_its_bot(
    watched: World, subscriber: Subscriber
) -> None:
    app = build_app(watched.store, watched.clock)
    async with _client(app) as api:
        made = await api.post(
            "/v1/pages",
            json={"parent": {"page_id": ids("handbook")}, "properties": {"title": [{"text": {"content": "Q4"}}]}},
        )
        assert made.status_code == 200
        await api.patch(f"/v1/pages/{made.json()['id']}", json={"archived": True})
    await app.settled()
    assert subscriber.types() == [
        ("page.created", made.json()["id"]),
        ("page.content_updated", ids("handbook")),
        ("page.deleted", made.json()["id"]),
    ], "a new page is also a new child_page block in its parent"
    assert {a["type"] for e in subscriber.events for a in e["authors"]} == {"bot"}
    assert {a["id"] for e in subscriber.events for a in e["authors"]} == {object_id("acme", "agent")}


async def test_a_comment_is_sent_only_to_a_subscription_that_asked_for_comments(
    tmp_path: Path, subscriber: Subscriber
) -> None:
    for events, sent in ((None, []), (["comment.created"], ["comment.created"])):
        notion = _subscribed(subscriber.url, events=events, verified=True)
        (tmp_path / str(len(sent))).mkdir()
        world = seeded(tmp_path / str(len(sent)), scenario(notion))
        subscriber.events.clear()
        await _person(world, notion, "dov", "Team Handbook", Commented(text="Please review."))
        assert [e["type"] for e in subscriber.events] == sent
        world.store.close()
    assert subscriber.verifying == [], "a subscription seeded as verified is never verified again"
    assert subscriber.events[0]["data"]["page_id"] == ids("handbook")


async def test_a_change_the_integration_cannot_see_is_not_sent(tmp_path: Path, subscriber: Subscriber) -> None:
    notion = _subscribed(subscriber.url, verified=True)
    notion["workspaces"][0]["integrations"][0]["shared"] = []  # type: ignore[index]
    world = seeded(tmp_path, scenario(notion))
    await _person(world, notion, "dov", "Team Handbook", Edited(append="Hidden."))
    assert subscriber.events == []
    world.store.close()


async def test_an_endpoint_that_refuses_verification_is_sent_no_events(tmp_path: Path, subscriber: Subscriber) -> None:
    subscriber.refuse_verification = True
    notion = _subscribed(subscriber.url)
    world = seeded(tmp_path, scenario(notion))
    await _person(world, notion, "dov", "Team Handbook", Edited(append="Unheard."))
    subscriber.refuse_verification = False
    await world.provider.notify(world.store, world.clock)
    assert len(subscriber.verifying) == 2 and subscriber.events == [], "what it was owed before is dropped"
    world.store.close()


def watched_users(world: World) -> list[Any]:
    from minutehand.adapters.providers.notion.state import NotionWorld

    return NotionWorld(world.store).users(ids("acme"))


def _client(app: NotionApp) -> httpx.AsyncClient:
    from tests.providers.notion.notion_world import AGENT_TOKEN

    headers = {**VERSION, "Authorization": f"Bearer {AGENT_TOKEN}"}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API, headers=headers)
