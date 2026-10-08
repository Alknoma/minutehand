"""Graph subscriptions: the validation handshake, renewal, deletion, and expiry read on the run's clock."""

from __future__ import annotations

from datetime import timedelta

from minutehand.domain.scenario import Edited
from tests.providers.microsoft.tenant import GRAPH, Intercepted, Tenant, Webhook, bearer, person_does, token


def _at(tenant: Tenant, later: timedelta) -> str:
    return (tenant.clock.now() + later).isoformat().replace("+00:00", "Z")


async def test_a_subscription_expires_on_the_runs_clock_and_notifies_nothing_after(
    tenant: Tenant, microsoft: Intercepted, webhook: Webhook
) -> None:
    drive = tenant.world.drives()[0].drive.id
    root = tenant.world.drives()[0].root_id
    item = next(c.item.id for c in tenant.world.children(root) if c.item.name == "notes.txt")
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        made = await http.post(
            f"{GRAPH}/subscriptions",
            json={
                "changeType": "updated",
                "notificationUrl": webhook.url,
                "resource": f"/drives/{drive}/root",
                "expirationDateTime": _at(tenant, timedelta(hours=1)),
                "clientState": "c",
            },
            headers=auth,
        )
        sub = made.json()["id"]
        renewed = await http.patch(
            f"{GRAPH}/subscriptions/{sub}", json={"expirationDateTime": _at(tenant, timedelta(days=2))}, headers=auth
        )
        assert renewed.status_code == 200 and renewed.json()["expirationDateTime"].startswith("2026-09-16")
        await person_does(tenant, "sofia", "notes.txt", Edited(append="edited once"))
        assert len(webhook.notifications) == 1
        assert webhook.notifications[0]["value"][0]["resourceData"]["id"] == item
        tenant.clock.jump(tenant.clock.now() + timedelta(days=3))
        assert not tenant.provider.watched(tenant.store, tenant.clock), "an expired subscription watches nothing"
        await person_does(tenant, "sofia", "notes.txt", Edited(append="edited after expiry"))
        assert len(webhook.notifications) == 1
        gone = await http.patch(
            f"{GRAPH}/subscriptions/{sub}", json={"expirationDateTime": _at(tenant, timedelta(days=1))}, headers=auth
        )
        assert gone.status_code == 404 and "code" not in gone.json()["error"], "subscription-update: 404, no code"
        read = await http.get(f"{GRAPH}/subscriptions/{sub}", headers=auth)
        assert read.status_code == 501, "reading an expired one: not documented"


async def test_a_subscription_unvalidated_or_repeated_is_refused_and_one_too_long_or_unwatchable_is_refused_by_name(
    tenant: Tenant, microsoft: Intercepted, webhook: Webhook
) -> None:
    drive = tenant.world.drives()[0].drive.id
    base = {"changeType": "updated", "notificationUrl": webhook.url, "resource": f"/drives/{drive}/root"}
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        too_long = await http.post(
            f"{GRAPH}/subscriptions", json={**base, "expirationDateTime": _at(tenant, timedelta(days=40))}, headers=auth
        )
        assert too_long.status_code == 501
        silent = await http.post(
            f"{GRAPH}/subscriptions",
            json={
                **base,
                "notificationUrl": "http://127.0.0.1:9/nobody",
                "expirationDateTime": _at(tenant, timedelta(hours=1)),
            },
            headers=auth,
        )
        assert silent.status_code == 400 and "validation" in silent.json()["error"]["message"]
        assert "code" not in silent.json()["error"], "subscription-post documents the 400, not a code"
        webhook.echo = False
        unechoed = await http.post(
            f"{GRAPH}/subscriptions", json={**base, "expirationDateTime": _at(tenant, timedelta(hours=1))}, headers=auth
        )
        assert unechoed.status_code == 400
        webhook.echo = True
        unwatchable = await http.post(
            f"{GRAPH}/subscriptions",
            json={**base, "resource": "/users", "expirationDateTime": _at(tenant, timedelta(hours=1))},
            headers=auth,
        )
        assert unwatchable.status_code == 501
        made = await http.post(
            f"{GRAPH}/subscriptions", json={**base, "expirationDateTime": _at(tenant, timedelta(hours=1))}, headers=auth
        )
        again = await http.post(
            f"{GRAPH}/subscriptions", json={**base, "expirationDateTime": _at(tenant, timedelta(hours=1))}, headers=auth
        )
        assert again.status_code == 409 and "code" not in again.json()["error"]
        assert again.json()["error"]["message"] == (
            f"Subscription Id {made.json()['id']} already exists for the requested combination"
        )
        assert (await http.delete(f"{GRAPH}/subscriptions/{made.json()['id']}", headers=auth)).status_code == 204
        assert (await http.delete(f"{GRAPH}/subscriptions/{made.json()['id']}", headers=auth)).status_code == 501
