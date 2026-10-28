"""What a calendar-sync client asks of Graph, through the real proxy with plain `httpx`: change notifications on a
mailbox's events for every change, a deleted event among them, with its `clientState`, renewed and deleted; a
calendar's own `calendarView/delta`, whose links stay on that calendar; a subscription to one calendar's events,
which Graph documents nowhere, refused naming the resources it does document; and a calendar made, shared, and
holding its own events, read, changed and deleted through it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from tests.providers.microsoft.outlook import AGENT, OUTLOOK, signed_in
from tests.providers.microsoft.tenant import GRAPH, Intercepted, Tenant, Webhook, bearer, seeded

WINDOW = "startDateTime=2026-09-14T00:00:00Z&endDateTime=2026-09-21T00:00:00Z"


@pytest.fixture
def tenant(tmp_path: Path) -> Iterator[Tenant]:
    found = seeded(tmp_path / "world.db", OUTLOOK)
    yield found
    found.store.close()


def _at(tenant: Tenant, later: timedelta) -> str:
    return (tenant.clock.now() + later).isoformat().replace("+00:00", "Z")


def _event(subject: str, day: str) -> dict[str, object]:
    return {
        "subject": subject,
        "start": {"dateTime": f"2026-09-{day}T10:00:00", "timeZone": "UTC"},
        "end": {"dateTime": f"2026-09-{day}T11:00:00", "timeZone": "UTC"},
    }


async def test_an_event_created_changed_and_deleted_notifies_each_change_with_the_client_state(
    tenant: Tenant, microsoft: Intercepted, webhook: Webhook
) -> None:
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        watched = await http.post(
            f"{GRAPH}/subscriptions",
            json={
                "changeType": "created,updated,deleted",
                "notificationUrl": webhook.url,
                "resource": "me/events",
                "expirationDateTime": _at(tenant, timedelta(hours=1)),
                "clientState": "sync-secret",
            },
            headers=me,
        )
        assert watched.status_code == 201, watched.text
        assert len(webhook.validations) == 1, "the validation handshake reached the notificationUrl"
        made = (await http.post(f"{GRAPH}/me/events", json=_event("Standup", "15"), headers=me)).json()
        patched = await http.patch(f"{GRAPH}/me/events/{made['id']}", json={"subject": "Stand-up"}, headers=me)
        assert patched.status_code == 200
        assert (await http.delete(f"{GRAPH}/me/events/{made['id']}", headers=me)).status_code == 204
        told = [n["value"][0] for n in webhook.notifications]
        assert [n["changeType"] for n in told] == ["created", "updated", "deleted"]
        assert {n["clientState"] for n in told} == {"sync-secret"}
        assert {n["resourceData"]["id"] for n in told} == {made["id"]}
        assert {n["subscriptionId"] for n in told} == {watched.json()["id"]}

        renewed = await http.patch(
            f"{GRAPH}/subscriptions/{watched.json()['id']}",
            json={"expirationDateTime": _at(tenant, timedelta(days=2))},
            headers=me,
        )
        assert renewed.status_code == 200
        assert (await http.delete(f"{GRAPH}/subscriptions/{watched.json()['id']}", headers=me)).status_code == 204
        await http.post(f"{GRAPH}/me/events", json=_event("After", "16"), headers=me)
        assert len(webhook.notifications) == 3, "a deleted subscription is notified of nothing"


async def test_a_cancelled_meeting_notifies_deleted(tenant: Tenant, microsoft: Intercepted, webhook: Webhook) -> None:
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        watched = await http.post(
            f"{GRAPH}/subscriptions",
            json={
                "changeType": "deleted",
                "notificationUrl": webhook.url,
                "resource": "me/events",
                "expirationDateTime": _at(tenant, timedelta(hours=1)),
            },
            headers=me,
        )
        assert watched.status_code == 201, watched.text
        meeting = {**_event("Review", "17"), "attendees": [{"emailAddress": {"address": "sofia@example.com"}}]}
        made = (await http.post(f"{GRAPH}/me/events", json=meeting, headers=me)).json()
        cancelled = await http.post(f"{GRAPH}/me/events/{made['id']}/cancel", json={"comment": "Moved"}, headers=me)
        assert cancelled.status_code == 202
        assert [n["value"][0]["changeType"] for n in webhook.notifications] == ["deleted"]


async def test_a_calendars_own_view_delta_pages_on_that_calendar_and_lists_what_changed(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        calendar = (await http.get(f"{GRAPH}/me/calendar", headers=me)).json()["id"]
        keep = (await http.post(f"{GRAPH}/me/events", json=_event("Keep", "15"), headers=me)).json()["id"]
        drop = (await http.post(f"{GRAPH}/me/events", json=_event("Drop", "16"), headers=me)).json()["id"]
        own = f"{GRAPH}/users/"
        first = await http.get(
            f"{GRAPH}/me/calendars/{calendar}/calendarView/delta?{WINDOW}",
            headers={**me, "Prefer": "odata.maxpagesize=1"},
        )
        assert first.status_code == 200, first.text
        page = first.json()
        assert len(page["value"]) == 1 and page["@odata.nextLink"].startswith(own)
        assert f"/calendars/{calendar}/calendarView/delta?$skiptoken=" in page["@odata.nextLink"]
        second = (await http.get(page["@odata.nextLink"], headers=me)).json()
        assert sorted(e["subject"] for e in page["value"] + second["value"]) == ["Drop", "Keep"]
        assert f"/calendars/{calendar}/calendarView/delta?$deltatoken=" in second["@odata.deltaLink"]
        tenant.clock.jump(tenant.clock.now() + timedelta(minutes=1))
        await http.patch(f"{GRAPH}/me/events/{keep}", json={"subject": "Kept"}, headers=me)
        await http.delete(f"{GRAPH}/me/events/{drop}", headers=me)
        changed = (await http.get(second["@odata.deltaLink"], headers=me)).json()["value"]
        assert [(e["id"], e["subject"]) for e in changed if "@removed" not in e] == [(keep, "Kept")]
        assert [(e["id"], e["@removed"]) for e in changed if "@removed" in e] == [(drop, {"reason": "deleted"})]


async def test_a_view_delta_of_a_calendar_the_mailbox_does_not_hold_is_refused_by_name(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        answered = await http.get(f"{GRAPH}/me/calendars/AAMkAnother/calendarView/delta?{WINDOW}", headers=me)
        assert answered.status_code == 501
        assert "'AAMkAnother', which the mailbox does not hold" in answered.json()["error"]["message"]


async def test_a_subscription_to_one_calendars_events_is_refused_naming_the_documented_resources(
    tenant: Tenant, microsoft: Intercepted, webhook: Webhook
) -> None:
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        calendar = (await http.get(f"{GRAPH}/me/calendar", headers=me)).json()["id"]
        answered = await http.post(
            f"{GRAPH}/subscriptions",
            json={
                "changeType": "created,updated,deleted",
                "notificationUrl": webhook.url,
                "resource": f"me/calendars/{calendar}/events",
                "expirationDateTime": _at(tenant, timedelta(hours=1)),
            },
            headers=me,
        )
        assert answered.status_code == 501
        message = answered.json()["error"]["message"]
        assert "Graph documents event subscriptions on a mailbox's events only, /me/events and /users/{id}/events" in (
            message
        )
        assert webhook.validations == [], "a refused subscription is never validated"


async def test_a_calendar_made_holds_its_own_events_read_changed_and_deleted_through_it(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Documented (user-post-calendars, calendar-post-events, event-get, event-update, event-delete,
    calendar-list-calendarview): a calendar made is listed beside the default one; an event made in it is the same
    event resource, read, changed and deleted through the calendar's path, in that calendar's view and the
    mailbox's events, and not in the default calendar's view."""
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        made = await http.post(f"{GRAPH}/me/calendars", json={"name": "Projects"}, headers=me)
        assert made.status_code == 201, made.text
        assert made.json()["name"] == "Projects" and made.json()["isDefaultCalendar"] is False
        calendar = made.json()["id"]
        listed = (await http.get(f"{GRAPH}/me/calendars", headers=me)).json()["value"]
        assert [(c["name"], c["isDefaultCalendar"]) for c in listed] == [("Calendar", True), ("Projects", False)]

        event = await http.post(f"{GRAPH}/me/calendars/{calendar}/events", json=_event("Kickoff", "15"), headers=me)
        assert event.status_code == 201, event.text
        here = f"{GRAPH}/me/calendars/{calendar}/events/{event.json()['id']}"
        assert (await http.get(here, headers=me)).json()["subject"] == "Kickoff"
        view = await http.get(f"{GRAPH}/me/calendars/{calendar}/calendarView?{WINDOW}", headers=me)
        assert [e["subject"] for e in view.json()["value"]] == ["Kickoff"]
        default_view = (await http.get(f"{GRAPH}/me/calendar/calendarView?{WINDOW}", headers=me)).json()["value"]
        assert "Kickoff" not in [e["subject"] for e in default_view]
        mailbox = (await http.get(f"{GRAPH}/me/events", params={"$top": "100"}, headers=me)).json()["value"]
        assert "Kickoff" in [e["subject"] for e in mailbox]
        own = (await http.get(f"{GRAPH}/me/calendars/{calendar}/events", headers=me)).json()["value"]
        assert [e["subject"] for e in own] == ["Kickoff"]

        patched = await http.patch(here, json={"subject": "Kick-off"}, headers=me)
        assert patched.status_code == 200 and patched.json()["subject"] == "Kick-off"
        assert (await http.delete(here, headers=me)).status_code == 204
        assert (await http.get(here, headers=me)).status_code == 404


async def test_an_event_through_a_calendar_that_does_not_hold_it_is_refused_by_name(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        calendar = (await http.post(f"{GRAPH}/me/calendars", json={"name": "Projects"}, headers=me)).json()["id"]
        elsewhere = (await http.post(f"{GRAPH}/me/events", json=_event("Standup", "15"), headers=me)).json()["id"]
        answered = await http.get(f"{GRAPH}/me/calendars/{calendar}/events/{elsewhere}", headers=me)
        assert answered.status_code == 501
        assert "which does not hold it" in answered.json()["error"]["message"]
        unknown = await http.post(f"{GRAPH}/me/calendars/AAMkAnother/events", json=_event("X", "15"), headers=me)
        assert unknown.status_code == 501
        assert "'AAMkAnother', which the mailbox does not hold" in unknown.json()["error"]["message"]


async def test_a_calendar_made_has_a_view_delta_of_its_own_events(tenant: Tenant, microsoft: Intercepted) -> None:
    """event-delta: a calendar view's delta is one calendar's; the mailbox's own is its primary calendar's."""
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        calendar = (await http.post(f"{GRAPH}/me/calendars", json={"name": "Projects"}, headers=me)).json()["id"]
        await http.post(f"{GRAPH}/me/calendars/{calendar}/events", json=_event("Kickoff", "15"), headers=me)
        await http.post(f"{GRAPH}/me/events", json=_event("Standup", "16"), headers=me)
        own = (await http.get(f"{GRAPH}/me/calendars/{calendar}/calendarView/delta?{WINDOW}", headers=me)).json()
        assert [e["subject"] for e in own["value"]] == ["Kickoff"]
        primary = (await http.get(f"{GRAPH}/me/calendarView/delta?{WINDOW}", headers=me)).json()
        assert "Kickoff" not in [e["subject"] for e in primary["value"]]
        assert "Standup" in [e["subject"] for e in primary["value"]]


async def test_a_calendar_shared_answers_the_permission_as_sent_and_a_second_for_one_person_is_refused_by_name(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """Documented (calendar-post-calendarpermissions, calendarpermission): 200 with the permission, its role and
    address as sent and its id as the page's example answer assigns one; the share is kept in the world."""
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        calendar = (await http.post(f"{GRAPH}/me/calendars", json={"name": "Projects"}, headers=me)).json()["id"]
        shared = await http.post(
            f"{GRAPH}/me/calendars/{calendar}/calendarPermissions",
            json={"emailAddress": {"address": "sofia@example.com", "name": "Sofia Romano"}, "role": "write"},
            headers=me,
        )
        assert shared.status_code == 200, shared.text
        answered = shared.json()
        assert answered["role"] == "write"
        assert answered["emailAddress"] == {"address": "sofia@example.com", "name": "Sofia Romano"}
        assert answered["id"] == "RXhjaGFuZ2VQdWJsaXNoZWRVc2VyLnNvZmlhQGV4YW1wbGUuY29t"
        assert "isInsideOrganization" not in answered and "allowedRoles" not in answered
        assert [p.emailAddress.address for p in tenant.world.calendar_permissions(calendar)] == ["sofia@example.com"]
        again = await http.post(
            f"{GRAPH}/me/calendars/{calendar}/calendarPermissions",
            json={"emailAddress": {"address": "Sofia@Example.com"}, "role": "read"},
            headers=me,
        )
        assert again.status_code == 501 and "a second permission" in again.json()["error"]["message"]


async def test_a_calendar_property_that_would_be_dropped_or_a_name_taken_is_refused_by_name(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        me = bearer(await signed_in(http, tenant, AGENT))
        coloured = await http.post(f"{GRAPH}/me/calendars", json={"name": "Projects", "color": "lightBlue"}, headers=me)
        assert coloured.status_code == 501 and "color" in coloured.json()["error"]["message"]
        taken = await http.post(f"{GRAPH}/me/calendars", json={"name": "calendar"}, headers=me)
        assert taken.status_code == 501 and "already holds" in taken.json()["error"]["message"]
        calendar = (await http.post(f"{GRAPH}/me/calendars", json={"name": "Projects"}, headers=me)).json()["id"]
        allowed = await http.post(
            f"{GRAPH}/me/calendars/{calendar}/calendarPermissions",
            json={"emailAddress": {"address": "sofia@example.com"}, "role": "read", "allowedRoles": ["read"]},
            headers=me,
        )
        assert allowed.status_code == 501 and "allowedRoles" in allowed.json()["error"]["message"]
        assert tenant.world.calendar_permissions(calendar) == []
