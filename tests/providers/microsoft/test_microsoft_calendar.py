"""Outlook calendars through Graph, through the real proxy with plain `httpx`: an event with attendees sends a
meeting request that asks each of them, a person's Accept sets their response at its moment and tells the
organizer, a moved event asks again, the calendar view and free/busy read every calendar, and the refusals."""

from __future__ import annotations

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
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", OUTLOOK)


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
        assert "Room Ferris" in request.after.text and "Vendor review" in request.after.text
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
        window = {"startDateTime": "2026-09-14T00:00:00Z", "endDateTime": "2026-09-16T00:00:00Z"}
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
                "schedules": ["sofia@example.com", "dania@example.com", "owen@example.com", "nobody@example.com"],
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
        assert views["nobody@example.com"]["error"]["responseCode"] == "ErrorMailRecipientNotFound"


async def test_a_calendar_view_without_a_window_is_refused(tenant: Tenant, microsoft: Intercepted) -> None:
    async with microsoft.http() as http:
        auth = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        refused = await http.get(f"{GRAPH}/users/{AGENT}/calendarView", headers=auth)
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "ErrorInvalidParameter"


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
