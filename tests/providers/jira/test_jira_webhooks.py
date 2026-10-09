"""Jira's admin webhooks: a webhook set up on the site is sent `jira:issue_created`, `jira:issue_updated` and
`comment_created` as they happen, by the agent or by a person, signed with its secret when it has one."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.jira.app import JiraApp
from minutehand.adapters.providers.jira.manifest import MANIFEST
from minutehand.adapters.providers.jira.provider import build
from minutehand.adapters.providers.jira.seed import account_id
from minutehand.adapters.providers.jira.state import issue_ref
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, Scenario
from minutehand.domain.world import Actor
from minutehand.ports.provider import DeliversInBackground
from tests.providers.jira.jira_site import (
    AGENT_EMAIL,
    AGENT_TOKEN,
    API,
    JIRA_SEED,
    SCENARIO,
    START,
    TOMAS,
    Site,
    basic,
    ok,
)

SECRET = "hook-secret"


@dataclass
class Receiver:
    """The agent's webhook endpoint."""

    url: str = ""
    bodies: list[bytes] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    status: int = 200

    async def handle(self, request: Request) -> Response:
        self.bodies.append(await request.body())
        self.headers.append(dict(request.headers))
        return Response(status_code=self.status)

    @property
    def events(self) -> list[dict[str, Any]]:
        return [json.loads(b) for b in self.bodies]


@pytest.fixture
async def receiver() -> AsyncIterator[Receiver]:
    endpoint = Receiver()
    app = Starlette(routes=[Route("/jira", endpoint.handle, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    endpoint.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/jira"
    yield endpoint
    server.should_exit = True
    await serving


async def _site(tmp_path: Path, hooks: list[dict[str, Any]]) -> AsyncIterator[Site]:
    body = SCENARIO.model_dump(mode="json")
    seed = json.loads(json.dumps(JIRA_SEED)) | {"webhooks": hooks}
    body["provider_seeds"] = [{"provider": "jira", "body": seed}]
    scenario = Scenario.model_validate(body)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(scenario, store)
    registry = Registry()
    registry.register(MANIFEST, lambda: provider)
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        found = Site(provider=provider, store=store, clock=clock, http=httpx.AsyncClient(), proxy=proxy)
        async with found.client(basic(AGENT_EMAIL, AGENT_TOKEN)) as http:
            found.http = http
            yield found


@pytest.fixture
async def hooked(tmp_path: Path, receiver: Receiver) -> AsyncIterator[Site]:
    async for found in _site(tmp_path, [{"url": receiver.url, "secret": SECRET}]):
        yield found


async def _settled(site: Site) -> None:
    app = site.provider.app(site.store, site.clock)
    assert isinstance(app, DeliversInBackground)
    for _ in range(200):
        if not app.delivering():
            return
        await asyncio.sleep(0.01)


async def _heard(site: Site, receiver: Receiver, count: int) -> list[dict[str, Any]]:
    for _ in range(300):
        if len(receiver.bodies) >= count:
            break
        await asyncio.sleep(0.01)
    return receiver.events


def _doc(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


async def test_the_app_is_one_that_delivers_in_the_background(tmp_path: Path) -> None:
    from minutehand.adapters.providers.jira.app import build_app

    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "root", clock)
    app = build_app(store, clock)
    assert isinstance(app, JiraApp) and isinstance(app, DeliversInBackground) and app.delivering() == 0
    store.close()


async def test_an_issue_the_agent_creates_is_sent_as_issue_created_signed(hooked: Site, receiver: Receiver) -> None:
    made = ok(
        await hooked.http.post(
            f"{API}/issue",
            json={"fields": {"project": {"key": "LAUNCH"}, "issuetype": {"name": "Task"}, "summary": "From the agent"}},
        ),
        201,
    )
    [event] = await _heard(hooked, receiver, 1)
    assert event["webhookEvent"] == "jira:issue_created" and event["timestamp"] == 1787568603000
    assert "issue_event_type_name" not in event and "changelog" not in event and "comment" not in event
    assert event["issue"]["key"] == made["key"] and event["issue"]["fields"]["summary"] == "From the agent"
    assert event["user"]["accountId"] == account_id(AGENT_EMAIL) and "emailAddress" not in event["user"]
    assert "locale" not in event["user"]
    body, headers = receiver.bodies[0], receiver.headers[0]
    assert headers["x-hub-signature"] == "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert headers["content-type"] == "application/json" and headers["x-atlassian-webhook-identifier"]


async def test_an_edit_is_sent_as_issue_updated_with_the_changelog_of_what_changed(
    hooked: Site, receiver: Receiver
) -> None:
    ok(await hooked.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Renamed"}}), 204)
    [event] = await _heard(hooked, receiver, 1)
    assert event["webhookEvent"] == "jira:issue_updated" and event["issue_event_type_name"] == "issue_generic"
    log = ok(await hooked.http.get(f"{API}/issue/LAUNCH-1/changelog"))["values"][-1]
    assert event["changelog"]["id"] == int(log["id"])
    assert [i["field"] for i in event["changelog"]["items"]] == ["summary"]
    assert event["changelog"]["items"][0]["toString"] == "Renamed"
    assert event["issue"]["fields"]["summary"] == "Renamed"


async def test_a_comment_is_sent_as_comment_created_with_the_comment(hooked: Site, receiver: Receiver) -> None:
    made = ok(await hooked.http.post(f"{API}/issue/LAUNCH-1/comment", json={"body": _doc("Hello")}), 201)
    [event] = await _heard(hooked, receiver, 1)
    assert event["webhookEvent"] == "comment_created" and event["comment"] == made
    assert event["issue"]["key"] == "LAUNCH-1"


async def test_a_transition_the_agent_makes_is_issue_updated_and_its_comment_is_comment_created(
    hooked: Site, receiver: Receiver
) -> None:
    body = {"transition": {"id": "11"}, "update": {"comment": [{"add": {"body": _doc("Going")}}]}}
    ok(await hooked.http.post(f"{API}/issue/LAUNCH-1/transitions", json=body), 204)
    events = await _heard(hooked, receiver, 2)
    assert [e["webhookEvent"] for e in events] == ["jira:issue_updated", "comment_created"]
    assert events[0]["changelog"]["items"][0]["toString"] == "In Progress"


async def test_each_delivery_has_an_identifier_of_its_own(hooked: Site, receiver: Receiver) -> None:
    ok(await hooked.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "One"}}), 204)
    ok(await hooked.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Two"}}), 204)
    await _heard(hooked, receiver, 2)
    ids = [h["x-atlassian-webhook-identifier"] for h in receiver.headers]
    assert len(set(ids)) == 2


async def test_a_read_and_a_call_that_changes_nothing_send_nothing(hooked: Site, receiver: Receiver) -> None:
    ok(await hooked.http.get(f"{API}/issue/LAUNCH-1"))
    ok(await hooked.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Write the release notes"}}), 204)
    await _settled(hooked)
    assert receiver.bodies == []


async def test_a_failed_delivery_is_not_retried(hooked: Site, receiver: Receiver) -> None:
    receiver.status = 500
    ok(await hooked.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Renamed"}}), 204)
    await _heard(hooked, receiver, 1)
    await asyncio.sleep(0.2)
    assert len(receiver.bodies) == 1


async def test_a_webhook_with_no_secret_is_not_signed(tmp_path: Path, receiver: Receiver) -> None:
    async for site in _site(tmp_path, [{"url": receiver.url}]):
        ok(await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Renamed"}}), 204)
        await _heard(site, receiver, 1)
    assert "x-hub-signature" not in receiver.headers[0]


async def test_a_webhook_is_sent_only_the_events_it_names(tmp_path: Path, receiver: Receiver) -> None:
    async for site in _site(tmp_path, [{"url": receiver.url, "events": ["comment_created"]}]):
        ok(await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Renamed"}}), 204)
        ok(await site.http.post(f"{API}/issue/LAUNCH-1/comment", json={"body": _doc("Hi")}), 201)
        events = await _heard(site, receiver, 1)
        await _settled(site)
        assert [e["webhookEvent"] for e in events] == ["comment_created"] and len(receiver.bodies) == 1


async def test_a_webhook_with_a_jql_filter_is_sent_only_the_issues_it_matches(
    tmp_path: Path, receiver: Receiver
) -> None:
    hook = {"url": receiver.url, "jql": 'project = LAUNCH AND status = "To Do"'}
    async for site in _site(tmp_path, [hook]):
        ok(await site.http.put(f"{API}/issue/FIELD-1", json={"fields": {"summary": "Elsewhere"}}), 204)
        ok(await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Here"}}), 204)
        [event] = await _heard(site, receiver, 1)
        await _settled(site)
        assert event["issue"]["key"] == "LAUNCH-1" and len(receiver.bodies) == 1


async def test_two_webhooks_are_each_sent_the_event(tmp_path: Path, receiver: Receiver) -> None:
    async for site in _site(tmp_path, [{"url": receiver.url}, {"url": receiver.url, "secret": "second"}]):
        ok(await site.http.put(f"{API}/issue/LAUNCH-1", json={"fields": {"summary": "Renamed"}}), 204)
        await _heard(site, receiver, 2)
    assert sorted("x-hub-signature" in h for h in receiver.headers) == [False, True]


@pytest.mark.parametrize(
    "hook",
    [
        {"url": "http://x", "events": ["jira:issue_deleted"]},
        {"url": "http://x", "events": ["worklog_created"]},
        {"url": "http://x", "events": ["nonsense"]},
        {"url": "http://x", "jql": "labels = beta"},
        {"url": "http://x", "jql": "project > LAUNCH"},
        {"url": "http://x", "jql": "project = LAUNCH ORDER BY created"},
    ],
)
def test_a_webhook_event_or_filter_the_reference_does_not_allow_is_refused_at_seeding(hook: dict[str, Any]) -> None:
    body = SCENARIO.model_dump(mode="json")
    body["provider_seeds"] = [{"provider": "jira", "body": json.loads(json.dumps(JIRA_SEED)) | {"webhooks": [hook]}}]
    with pytest.raises((ValidationError, ValueError)):
        build().seed(Scenario.model_validate(body), SqliteStore(Path(":memory:"), "root", RunClock(START)))


def _tomas() -> Person:
    return next(p for p in SCENARIO.people if p.key == "tomas")


async def test_a_persons_transition_is_sent_to_the_webhook_and_the_agent_hears_of_it(
    hooked: Site, receiver: Receiver
) -> None:
    ref = issue_ref("1000")
    assert hooked.provider.heard_of(ref, _tomas(), hooked.store, hooked.clock) is True
    moved = await hooked.provider.apply(
        ref, "Start work", Actor.PERSON, _tomas(), json.dumps({"comment": "On it"}), hooked.store, hooked.clock
    )
    assert moved.to_state == "In Progress"
    events = await _heard(hooked, receiver, 2)
    assert [e["webhookEvent"] for e in events] == ["jira:issue_updated", "comment_created"]
    assert events[0]["user"]["accountId"] == TOMAS
    assert events[0]["changelog"]["items"][0]["toString"] == "In Progress"
    assert events[1]["comment"]["author"]["accountId"] == TOMAS


async def test_the_agent_does_not_hear_of_a_move_no_webhook_is_sent(site: Site) -> None:
    assert site.provider.heard_of(issue_ref("1000"), _tomas(), site.store, site.clock) is False


async def test_the_agent_does_not_hear_of_a_move_the_webhooks_filter_leaves_out(
    tmp_path: Path, receiver: Receiver
) -> None:
    async for site in _site(tmp_path, [{"url": receiver.url, "jql": "project = FIELD"}]):
        assert site.provider.heard_of(issue_ref("1000"), _tomas(), site.store, site.clock) is False
        await site.provider.apply(issue_ref("1000"), "Start work", Actor.PERSON, _tomas(), "{}", site.store, site.clock)
        assert receiver.bodies == []
