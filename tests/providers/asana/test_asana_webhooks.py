"""Webhooks (https://developers.asana.com/docs/webhooks-guide, `createWebhook`): the `X-Hook-Secret` handshake while
the creating request is in flight, signed deliveries, filters, and the failure of a target."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest

from minutehand.adapters.answering import Guarded
from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.app import AsanaApp
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.domain.transitions import Transition
from minutehand.domain.world import Actor, EntityKind, EntityRef
from tests.providers.asana.asana_workspace import (
    AUTH,
    SCENARIO,
    VENUE,
    Workspace,
    body,
    create,
    error,
    items,
    unserved,
    workspace,
)
from tests.providers.asana.hooks import target
from tests.providers.asana.rich_workspace import got

__all__ = ["workspace"]

WS = state.WORKSPACE_GID
TOMAS = SCENARIO.people[1]


@dataclass
class Wired:
    client: httpx.AsyncClient
    app: AsanaApp
    ws: Workspace

    async def settle(self) -> None:
        await self.app.settled()


@pytest.fixture
async def wired(workspace: Workspace) -> AsyncIterator[Wired]:
    app = workspace.provider.app(workspace.store, workspace.clock)
    assert isinstance(app, AsanaApp)
    guarded = Guarded(app, workspace.provider, provider=MANIFEST.key, clock=workspace.clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=guarded), base_url="https://app.asana.com", headers=AUTH
    ) as client:
        yield Wired(client, app, workspace)


async def subscribe(wired: Wired, resource: object, url: str, **fields: object) -> httpx.Response:
    return await wired.client.post("/webhooks", json={"data": {"resource": str(resource), "target": url, **fields}})


def signed(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


async def test_creating_a_webhook_completes_the_handshake_and_answers_with_the_secret(wired: Wired) -> None:
    with target() as hook:
        answered = await subscribe(wired, VENUE, hook.url)
        assert answered.status_code == 201
        found = answered.json()
        secret = found["X-Hook-Secret"]
        assert hook.handshakes == [secret] and len(secret) == 64
        shown = found["data"]
        assert (shown["resource"], shown["target"], shown["active"]) == (
            {"gid": VENUE, "resource_type": "project", "name": "Venue Move"},
            hook.url,
            True,
        )
        assert answered.headers["location"].endswith(f"/webhooks/{shown['gid']}")
        await wired.settle()


async def test_the_first_delivery_is_an_empty_signed_heartbeat_that_sets_last_success_at(wired: Wired) -> None:
    with target() as hook:
        made = (await subscribe(wired, VENUE, hook.url)).json()
        await wired.settle()
        [beat] = hook.deliveries
        assert beat.events() == []
        assert beat.headers["X-Hook-Signature"] == signed(made["X-Hook-Secret"], beat.body)
        read = got(await wired.client.get(f"/webhooks/{made['data']['gid']}"))
        assert read["active"] is True and read["last_success_at"] is not None, "active from the handshake on"
        assert read["delivery_retry_count"] == 0


async def test_a_change_is_delivered_signed_as_events_with_gids_and_types_only(wired: Wired) -> None:
    with target() as hook:
        made = (await subscribe(wired, VENUE, hook.url)).json()
        await wired.settle()
        task = await create(wired.client, name="Pour", projects=[VENUE])
        await wired.settle()
        await wired.client.put(f"/tasks/{task['gid']}", json={"data": {"assignee": state.user_gid("tomas")}})
        await wired.settle()
        first, second = hook.deliveries[1:]
        assert first.headers["X-Hook-Signature"] == signed(made["X-Hook-Secret"], first.body)
        assert first.headers["Content-Type"] == "application/json"
        [added] = first.events()
        assert added["action"] == "added" and added["parent"] == {"gid": VENUE, "resource_type": "project"}
        assert added["resource"] == {"gid": task["gid"], "resource_type": "task", "resource_subtype": "default_task"}
        assert added["user"] == {"gid": state.AGENT_GID, "resource_type": "user"}
        [changed] = second.events()
        assert changed["change"] == {
            "field": "assignee",
            "action": "changed",
            "new_value": {"gid": state.user_gid("tomas"), "resource_type": "user"},
        }


async def test_a_delivery_carries_every_event_owed_since_the_last(wired: Wired) -> None:
    with target() as hook:
        await subscribe(wired, VENUE, hook.url)
        await wired.settle()
        task = await create(wired.client, name="Pour", projects=[VENUE])
        await wired.client.put(f"/tasks/{task['gid']}", json={"data": {"name": "Pour 2"}})
        await wired.settle()
        total = sum(len(d.events()) for d in hook.deliveries)
        assert total == 2 and [e["action"] for d in hook.deliveries for e in d.events()] == ["added", "changed"]


async def test_a_webhook_with_filters_is_sent_only_what_passes_any_of_them(wired: Wired) -> None:
    with target() as hook:
        filters = [{"resource_type": "task", "action": "changed", "fields": ["completed"]}]
        made = (await subscribe(wired, VENUE, hook.url, filters=filters)).json()
        await wired.settle()
        task = await create(wired.client, name="Pour", projects=[VENUE])
        await wired.client.put(f"/tasks/{task['gid']}", json={"data": {"name": "Pour 2"}})
        await wired.client.put(f"/tasks/{task['gid']}", json={"data": {"completed": True}})
        await wired.settle()
        sent = [e for d in hook.deliveries for e in d.events()]
        assert [e["change"]["field"] for e in sent] == ["completed"]
        assert got(await wired.client.get(f"/webhooks/{made['data']['gid']}"))["filters"] == [
            {"resource_type": "task", "resource_subtype": None, "action": "changed", "fields": ["completed"]}
        ]


async def test_an_event_passing_only_one_of_two_filters_is_delivered(wired: Wired) -> None:
    with target() as hook:
        filters = [{"action": "added"}, {"action": "deleted"}]
        await subscribe(wired, VENUE, hook.url, filters=filters)
        await wired.settle()
        task = await create(wired.client, name="Pour", projects=[VENUE])
        await wired.client.put(f"/tasks/{task['gid']}", json={"data": {"name": "Pour 2"}})
        await wired.client.delete(f"/tasks/{task['gid']}")
        await wired.settle()
        assert [e["action"] for d in hook.deliveries for e in d.events()] == ["added", "deleted"]


async def test_updating_a_webhook_overwrites_its_filters(wired: Wired) -> None:
    with target() as hook:
        made = (await subscribe(wired, VENUE, hook.url, filters=[{"resource_type": "task", "action": "added"}])).json()
        gid = made["data"]["gid"]
        await wired.settle()
        updated = got(await wired.client.put(f"/webhooks/{gid}", json={"data": {"filters": [{"action": "deleted"}]}}))
        assert updated["filters"] == [
            {"resource_type": None, "resource_subtype": None, "action": "deleted", "fields": None}
        ]
        task = await create(wired.client, name="Pour", projects=[VENUE])
        await wired.settle()
        assert [e for d in hook.deliveries for e in d.events()] == []
        await wired.client.delete(f"/tasks/{task['gid']}")
        await wired.settle()
        assert [e["action"] for d in hook.deliveries for e in d.events()] == ["deleted"]


async def test_a_target_that_does_not_answer_the_handshake_makes_no_webhook_it_is_refused_by_name(
    wired: Wired,
) -> None:
    with target() as hook:
        hook.echo = False
        answered = await subscribe(wired, VENUE, hook.url)
        assert "does not answer the X-Hook-Secret handshake" in unserved(answered)
        assert items(await wired.client.get("/webhooks", params={"workspace": WS})) == []


async def test_a_target_on_localhost_and_a_second_webhook_on_the_same_target_are_refused_by_name(wired: Wired) -> None:
    refused = await subscribe(wired, VENUE, "http://localhost:9/hook")
    assert "a webhook target whose host is `localhost`" in unserved(refused)
    with target() as hook:
        assert (await subscribe(wired, VENUE, hook.url)).status_code == 201
        twice = await subscribe(wired, VENUE, hook.url)
        assert "a second webhook on the same resource and target" in unserved(twice)
        await wired.settle()


async def test_webhooks_are_listed_for_a_workspace_by_resource_read_and_deleted(wired: Wired) -> None:
    with target() as hook:
        task = await create(wired.client, name="Pour", projects=[VENUE])
        mine = (await subscribe(wired, VENUE, hook.url)).json()["data"]
        theirs = (await subscribe(wired, task["gid"], hook.url + "?task")).json()["data"]
        await wired.settle()
        assert error(await wired.client.get("/webhooks"), 400) == "workspace: Missing input"
        listed = items(await wired.client.get("/webhooks", params={"workspace": WS}))
        assert [h["gid"] for h in listed] == [mine["gid"], theirs["gid"]]
        assert set(listed[0]) == {"gid", "resource_type", "active", "resource", "target"}
        only = items(await wired.client.get("/webhooks", params={"workspace": WS, "resource": VENUE}))
        assert [h["gid"] for h in only] == [mine["gid"]]
        assert body(await wired.client.delete(f"/webhooks/{mine['gid']}")) == {"data": {}}
        assert (await wired.client.get(f"/webhooks/{mine['gid']}")).status_code == 404
        before = len(hook.deliveries)
        await create(wired.client, name="Another", projects=[VENUE])
        await wired.settle()
        assert len(hook.deliveries) == before


async def test_a_task_subscription_hears_its_own_changes_and_a_project_subscription_its_tasks(wired: Wired) -> None:
    with target() as hook:
        task = await create(wired.client, name="Pour", projects=[VENUE])
        other = await create(wired.client, name="Other", projects=[VENUE])
        await subscribe(wired, task["gid"], hook.url)
        await wired.settle()
        await wired.client.put(f"/tasks/{other['gid']}", json={"data": {"name": "Other 2"}})
        await wired.client.put(f"/tasks/{task['gid']}", json={"data": {"name": "Pour 2"}})
        await wired.settle()
        assert [e["resource"]["gid"] for d in hook.deliveries for e in d.events()] == [task["gid"]]


async def test_a_target_that_fails_is_recorded_and_sent_the_same_events_again_next_time(wired: Wired) -> None:
    with target() as hook:
        made = (await subscribe(wired, VENUE, hook.url)).json()["data"]
        await wired.settle()
        hook.status = 500
        await create(wired.client, name="Pour", projects=[VENUE])
        await wired.settle()
        failing = got(await wired.client.get(f"/webhooks/{made['gid']}"))
        assert failing["delivery_retry_count"] == 1 and failing["last_failure_at"] is not None
        assert failing["last_failure_content"].startswith("500")
        hook.status = 200
        await create(wired.client, name="Pour 2", projects=[VENUE])
        await wired.settle()
        failed, *later = hook.deliveries[1:]
        retried = [e for d in later for e in d.events()]
        assert len(retried) == 2 and retried[0] == failed.events()[0]  # the failed event once more, then the new one
        healed = got(await wired.client.get(f"/webhooks/{made['gid']}"))
        assert healed["delivery_retry_count"] == 0 and healed["last_failure_at"] is not None


async def test_an_approvers_decision_is_pushed_and_the_move_is_heard_of(wired: Wired) -> None:
    with target() as hook:
        approval = await create(
            wired.client,
            name="Sign off",
            projects=[VENUE],
            resource_subtype="approval",
            assignee="tomas@example.com",
        )
        await subscribe(wired, VENUE, hook.url)
        await wired.settle()
        item = EntityRef(provider="asana", kind=EntityKind.TICKET, external_id=str(approval["gid"]))
        assert wired.ws.provider.heard_of(item, TOMAS, wired.ws.store, wired.ws.clock)
        done = await wired.ws.provider.apply(
            item, "approved", Actor.PERSON, TOMAS, "{}", wired.ws.store, wired.ws.clock
        )
        assert isinstance(done, Transition)
        sent = [e for d in hook.deliveries for e in d.events()]
        assert [(e["action"], e["change"]["field"], e["user"]["gid"]) for e in sent if e["action"] == "changed"][
            :2
        ] == [
            ("changed", "completed", state.user_gid("tomas")),
            ("changed", "approval_status", state.user_gid("tomas")),
        ]
        assert json.loads(hook.deliveries[-1].body)["events"]


async def test_a_person_move_nobody_subscribed_to_is_not_heard_of(wired: Wired) -> None:
    task = await create(wired.client, name="Pour", projects=[VENUE], assignee="tomas@example.com")
    item = EntityRef(provider="asana", kind=EntityKind.TICKET, external_id=str(task["gid"]))
    assert not wired.ws.provider.heard_of(item, TOMAS, wired.ws.store, wired.ws.clock)
