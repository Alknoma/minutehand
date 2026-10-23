"""Outlook calendars through Graph, through the real proxy with plain `httpx`: an event with attendees sends a
meeting request that asks each of them, a person's Accept sets their response at its moment and tells the
organizer, a moved event asks again, the calendar view and free/busy read every calendar, and the refusals."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.microsoft.seed import microsoft_seed
from minutehand.domain.scenario import ProviderSeed
from minutehand.domain.world import Actor, InteractionSnapshot, MessageSnapshot, RecordSnapshot
from tests.providers.microsoft.outlook import AGENT, OUTLOOK, reply, sent_by_agent, signed_in
from tests.providers.microsoft.tenant import GRAPH, START, Intercepted, Tenant, Webhook, bearer, seeded, token

"""Where Teams would push; nothing is pushed for an invitation's answer, and a push here would fail."""

REVIEW = {
    "subject": "Vendor review",
    "body": {"contentType": "text", "content": "Shortlist and prices."},
    "start": {"dateTime": "2026-09-15T10:00:00", "timeZone": "Europe/Rome"},
    "end": {"dateTime": "2026-09-15T10:30:00", "timeZone": "Europe/Rome"},
    "location": {"displayName": "Room Ferris"},
    "attendees": [{"emailAddress": {"address": "sofia@example.com", "name": "Sofia"}, "type": "required"}],
}


@pytest.fixture
def tenant(tmp_path: Path) -> Iterator[Tenant]:
    found = seeded(tmp_path / "world.db", OUTLOOK)
    yield found
    found.store.close()


def _at(tenant: Tenant, later: timedelta) -> str:
    return (tenant.clock.now() + later).isoformat().replace("+00:00", "Z")


async def test_an_invitation_asks_each_attendee_and_their_accept_lands_as_their_change(
    tenant: Tenant, microsoft: Intercepted, webhook: Webhook
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await signed_in(http, tenant, AGENT))
        watched = await http.post(
            f"{GRAPH}/subscriptions",
            json={"changeType": "updated", "notificationUrl": webhook.url, "resource": "me/events",
                  "expirationDateTime": _at(tenant, timedelta(days=1))},
            headers=auth,
        )  # fmt: skip
        assert watched.status_code == 201, watched.text
        made = await http.post(f"{GRAPH}/me/events", json=REVIEW, headers=auth)
        assert made.status_code == 201, made.text
        event = made.json()
        assert (event["start"]["dateTime"], event["start"]["timeZone"]) == ("2026-09-15T08:00:00.0000000", "UTC")
        assert event["isOrganizer"] is True and event["attendees"][0]["status"]["response"] == "none"
        [request] = sent_by_agent(tenant)
        assert isinstance(request.after, MessageSnapshot)
        assert request.after.recipient_emails == ["sofia@example.com"]
        assert [a.label for a in request.after.actions] == ["Accept", "Tentative", "Decline"]
        assert request.after.text == "Vendor review\n\nShortlist and prices.", "the request carries the event's body"
        hers = await http.get(f"{GRAPH}/users/sofia@example.com/events/{event['id']}", headers=auth)
        assert hers.status_code == 403, "a user's token reads only their own calendar"
        app = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        seen = (await http.get(f"{GRAPH}/users/sofia@example.com/events/{event['id']}", headers=app)).json()
        assert (seen["isOrganizer"], seen["responseStatus"]["response"]) == (False, "none")

        tenant.clock.jump(START + timedelta(hours=2))
        await tenant.provider.land(
            reply("sofia", request, press="Accept", after=timedelta(hours=2)),
            tenant.store,
            tenant.clock,
        )
        now = (await http.get(f"{GRAPH}/me/events/{event['id']}", headers=auth)).json()
        assert now["attendees"][0]["status"] == {"response": "accepted", "time": "2026-09-14T10:30:00.000Z"}
        [note] = webhook.notifications
        assert note["value"][0]["resourceData"]["id"] == event["id"]
        told = await http.get(
            f"{GRAPH}/me/mailFolders/inbox/messages",
            params={"$filter": f"conversationId eq '{request.after.channel}'"},
            headers=auth,
        )
        [response] = told.json()["value"]
        assert (response["subject"], response["meetingMessageType"]) == ("Accepted: Vendor review", "meetingAccepted")
    by_her = [e for e in tenant.store.events() if e.actor is Actor.PERSON]
    pressed = next(e.after for e in by_her if isinstance(e.after, InteractionSnapshot))
    assert isinstance(pressed, InteractionSnapshot) and (pressed.person, pressed.label) == ("sofia", "Accept")
    changed = next(e.after for e in by_her if isinstance(e.after, RecordSnapshot))
    assert isinstance(changed, RecordSnapshot) and changed.text == "sofia@example.com accepted Vendor review"
    assert all(e.sim_time == START + timedelta(hours=2) for e in by_her)


async def test_a_moved_event_asks_again_and_an_answer_to_the_old_request_changes_nothing(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await signed_in(http, tenant, AGENT))
        event = (await http.post(f"{GRAPH}/me/events", json=REVIEW, headers=auth)).json()
        [first] = sent_by_agent(tenant)
        moved = await http.patch(
            f"{GRAPH}/me/events/{event['id']}",
            json={"start": {"dateTime": "2026-09-16T08:00:00Z"}, "end": {"dateTime": "2026-09-16T08:30:00Z"}},
            headers=auth,
        )
        assert moved.status_code == 200, moved.text
        second = sent_by_agent(tenant)[-1]
        assert second.entity != first.entity and isinstance(second.after, MessageSnapshot)
        assert second.after.channel == first.after.channel, "the new request is in the event's conversation"  # type: ignore[union-attr]
        await tenant.provider.land(
            reply("sofia", first, press="Decline", after=timedelta(hours=1)), tenant.store, tenant.clock
        )
        unchanged = (await http.get(f"{GRAPH}/me/events/{event['id']}", headers=auth)).json()
        assert unchanged["attendees"][0]["status"]["response"] == "none"
        await tenant.provider.land(
            reply("sofia", second, press="Tentative", after=timedelta(hours=1)), tenant.store, tenant.clock
        )
        answered = (await http.get(f"{GRAPH}/me/events/{event['id']}", headers=auth)).json()
        assert answered["attendees"][0]["status"]["response"] == "tentativelyAccepted"


async def test_the_calendar_view_and_free_busy_read_every_calendar_and_its_answers(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        window = {
            "startDateTime": "2026-09-14T00:00:00Z",
            "endDateTime": "2026-09-16T00:00:00Z",
            "$orderby": "start/dateTime",
        }
        dania = (await http.get(f"{GRAPH}/users/dania@example.com/calendarView", params=window, headers=auth)).json()
        assert [(e["subject"], e["isOrganizer"]) for e in dania["value"]] == [
            ("Budget sync", False),
            ("Planning", True),
        ]
        today = {**window, "endDateTime": "2026-09-15T00:00:00Z"}
        sofia = await http.get(f"{GRAPH}/users/sofia@example.com/calendar/calendarView", params=today, headers=auth)
        assert [e["subject"] for e in sofia.json()["value"]] == ["Budget sync"]
        schedule = await http.post(
            f"{GRAPH}/users/{AGENT}/calendar/getSchedule",
            json={
                "schedules": ["sofia@example.com", "dania@example.com", "owen@example.com"],
                "startTime": {"dateTime": "2026-09-14T10:00:00", "timeZone": "UTC"},
                "endTime": {"dateTime": "2026-09-14T12:00:00", "timeZone": "UTC"},
                "availabilityViewInterval": 30,
            },
            headers=auth,
        )
        assert schedule.status_code == 200, schedule.text
        views = {s["scheduleId"]: s for s in schedule.json()["value"]}
        assert views["sofia@example.com"]["availabilityView"] == "0220", "Budget sync, 10:30 to 11:30, hers"
        assert views["dania@example.com"]["availabilityView"] == "0220", "she accepted it"
        assert views["owen@example.com"]["availabilityView"] == "0000", "he declined it"
        nobody = await http.post(
            f"{GRAPH}/users/{AGENT}/calendar/getSchedule",
            json={
                "schedules": ["nobody@example.com"],
                "startTime": {"dateTime": "2026-09-14T10:00:00", "timeZone": "UTC"},
                "endTime": {"dateTime": "2026-09-14T12:00:00", "timeZone": "UTC"},
            },
            headers=auth,
        )
        assert nobody.status_code == 501, "no Graph page or recording shows the answer for an unknown address"


async def test_a_calendar_view_without_a_window_is_refused_by_name(tenant: Tenant, microsoft: Intercepted) -> None:
    """The page names startDateTime and endDateTime required and documents no answer without them: not served."""
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        refused = await http.get(f"{GRAPH}/users/{AGENT}/calendarView", headers=auth)
        assert refused.status_code == 501 and "startDateTime and endDateTime" in refused.json()["error"]["message"]


async def test_an_event_retried_with_its_transaction_id_is_made_once(tenant: Tenant, microsoft: Intercepted) -> None:
    async with microsoft.http() as http:
        auth = bearer(await signed_in(http, tenant, AGENT))
        once = await http.post(f"{GRAPH}/me/events", json={**REVIEW, "transactionId": "t-1"}, headers=auth)
        again = await http.post(f"{GRAPH}/me/events", json={**REVIEW, "transactionId": "t-1"}, headers=auth)
        assert once.json()["id"] == again.json()["id"]
        assert len(sent_by_agent(tenant)) == 1, "one invitation"


def test_a_seeded_event_naming_its_organizer_among_its_attendees_is_refused(tmp_path: Path) -> None:
    body = microsoft_seed(OUTLOOK).model_dump(mode="json")
    body["events"] = [
        {"organizer": "sofia", "subject": "x", "at": "PT1H", "lasts": "PT1H", "attendees": [{"person": "sofia"}]}
    ]
    scenario = OUTLOOK.model_copy(
        update={"provider_seeds": [ProviderSeed.model_validate({"provider": "microsoft", "body": body})]}
    )
    with pytest.raises(ValueError, match="names someone twice"):
        seeded(tmp_path / "world.db", scenario)


AWAY = {
    "subject": "Out of office",
    "isAllDay": True,
    "showAs": "oof",
    "start": {"dateTime": "2026-09-15T00:00:00.0000000", "timeZone": "UTC"},
    "end": {"dateTime": "2026-09-17T00:00:00.0000000", "timeZone": "UTC"},
}


async def test_an_all_day_out_of_office_is_kept_shown_as_oof_and_free_busy_reads_it_away(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    """An Outlook absence is an event shown as `oof`: kept as sent, read back, counted out of office (3) in the
    organizer's free/busy, and shown free once changed to free."""
    async with microsoft.http() as http:
        auth = bearer(await signed_in(http, tenant, AGENT))
        made = await http.post(f"{GRAPH}/me/events", json=AWAY, headers=auth)
        assert made.status_code == 201, made.text
        assert (made.json()["isAllDay"], made.json()["showAs"]) == (True, "oof")
        listed = await http.get(
            f"{GRAPH}/me/calendarView",
            params={"startDateTime": "2026-09-15T00:00:00Z", "endDateTime": "2026-09-16T00:00:00Z"},
            headers=auth,
        )
        [away] = [e for e in listed.json()["value"] if e["subject"] == "Out of office"]
        assert (away["isAllDay"], away["showAs"]) == (True, "oof")

        def schedule() -> dict[str, object]:
            return {
                "schedules": [AGENT],
                "startTime": {"dateTime": "2026-09-15T09:00:00", "timeZone": "UTC"},
                "endTime": {"dateTime": "2026-09-15T10:00:00", "timeZone": "UTC"},
                "availabilityViewInterval": 30,
            }

        app = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        busy = await http.post(f"{GRAPH}/users/{AGENT}/calendar/getSchedule", json=schedule(), headers=app)
        assert busy.json()["value"][0]["availabilityView"] == "33", "the organizer is out of office"
        freed = await http.patch(f"{GRAPH}/me/events/{made.json()['id']}", json={"showAs": "free"}, headers=auth)
        assert freed.status_code == 200, freed.text
        assert freed.json()["showAs"] == "free" and freed.json()["isAllDay"] is True
        free = await http.post(f"{GRAPH}/users/{AGENT}/calendar/getSchedule", json=schedule(), headers=app)
        assert free.json()["value"][0]["availabilityView"] == "00", "an event shown free leaves the time free"


async def test_an_all_day_event_not_at_midnights_is_refused_by_name(tenant: Tenant, microsoft: Intercepted) -> None:
    async with microsoft.http() as http:
        auth = bearer(await signed_in(http, tenant, AGENT))
        off = {**AWAY, "start": {"dateTime": "2026-09-15T09:00:00", "timeZone": "UTC"}}
        refused = await http.post(f"{GRAPH}/me/events", json=off, headers=auth)
        assert refused.status_code == 501 and "midnight" in refused.json()["error"]["message"]


async def test_a_seeded_all_day_out_of_office_is_shown_as_oof(tmp_path: Path, microsoft: Intercepted) -> None:
    body = microsoft_seed(OUTLOOK).model_dump(mode="json")
    body["events"] = [{"organizer": "sofia", "subject": "Leave", "day": 1, "days": 3, "show_as": "oof"}]
    scenario = OUTLOOK.model_copy(
        update={"provider_seeds": [ProviderSeed.model_validate({"provider": "microsoft", "body": body})]}
    )
    seeded_tenant = seeded(tmp_path / "world.db", scenario)
    async with microsoft.http() as http:
        auth = bearer(await token(http, seeded_tenant, "https://graph.microsoft.com/.default"))
        start = seeded_tenant.clock.now().date() + timedelta(days=1)
        listed = await http.get(
            f"{GRAPH}/users/sofia@example.com/calendarView",
            params={
                "startDateTime": f"{start.isoformat()}T00:00:00Z",
                "endDateTime": f"{(start + timedelta(days=3)).isoformat()}T00:00:00Z",
            },
            headers=auth,
        )
        [leave] = [e for e in listed.json()["value"] if e["subject"] == "Leave"]
        assert (leave["isAllDay"], leave["showAs"]) == (True, "oof")
        assert leave["start"]["dateTime"].startswith(f"{start.isoformat()}T00:00:00")
    seeded_tenant.store.close()


def test_a_seeded_event_neither_timed_nor_all_day_is_refused() -> None:
    from minutehand.adapters.providers.microsoft.seed import EventSeed

    with pytest.raises(ValueError, match=r"timed .* or all day"):
        EventSeed.model_validate({"organizer": "sofia", "subject": "x", "at": "PT1H", "day": 1})
