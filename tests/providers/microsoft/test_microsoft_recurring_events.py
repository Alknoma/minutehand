"""Recurring events (daily and weekly patterns), their instances, cancelling a meeting, declining it with a comment, and
files attached to an event."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from minutehand.domain.world import Actor, MessageSnapshot
from tests.providers.microsoft.outlook import AGENT, OUTLOOK, sent_by_agent, signed_in
from tests.providers.microsoft.tenant import GRAPH, Intercepted, Tenant, bearer, seeded, token


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", OUTLOOK)


@dataclass
class Calendar:
    http: httpx.AsyncClient
    me: dict[str, str]
    app: dict[str, str]
    tenant: Tenant

    async def make(self, **event: Any) -> httpx.Response:
        body = {
            "subject": "Standup",
            "start": {"dateTime": "2026-09-14T10:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-09-14T10:30:00", "timeZone": "UTC"},
            **event,
        }
        return await self.http.post(f"{GRAPH}/me/events", json=body, headers=self.me)

    async def instances(self, event: str, start: str, end: str) -> list[dict[str, Any]]:
        listed = await self.http.get(
            f"{GRAPH}/me/events/{event}/instances",
            params={"startDateTime": start, "endDateTime": end, "$orderby": "start/dateTime"},
            headers=self.me,
        )
        assert listed.status_code == 200, listed.text
        found: list[dict[str, Any]] = listed.json()["value"]
        return found


@pytest.fixture
async def calendar(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Calendar]:
    async with microsoft.http() as http:
        yield Calendar(
            http=http,
            me=bearer(await signed_in(http, tenant, AGENT)),
            app=bearer(await token(http, tenant, "https://graph.microsoft.com/.default")),
            tenant=tenant,
        )


def _days(instances: list[dict[str, Any]]) -> list[str]:
    return [i["start"]["dateTime"][:10] for i in instances]


async def test_a_weekly_series_is_kept_as_sent_with_the_defaults_the_page_documents_and_lists_its_instances(
    calendar: Calendar,
) -> None:
    made = await calendar.make(
        recurrence={
            "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["Monday", "Wednesday"]},
            "range": {"type": "endDate", "startDate": "2026-09-14", "endDate": "2026-10-02"},
        }
    )
    assert made.status_code == 201, made.text
    series = made.json()
    assert series["type"] == "seriesMaster" and "seriesMasterId" not in series
    assert series["recurrence"]["pattern"] == {
        "type": "weekly",
        "interval": 1,
        "month": 0,
        "dayOfMonth": 0,
        "daysOfWeek": ["monday", "wednesday"],
        "firstDayOfWeek": "sunday",
        "index": "first",
    }
    assert series["recurrence"]["range"] == {
        "type": "endDate",
        "startDate": "2026-09-14",
        "endDate": "2026-10-02",
        "recurrenceTimeZone": "UTC",
        "numberOfOccurrences": 0,
    }
    found = await calendar.instances(series["id"], "2026-09-01T00:00:00Z", "2026-12-01T00:00:00Z")
    assert _days(found) == ["2026-09-14", "2026-09-16", "2026-09-21", "2026-09-23", "2026-09-28", "2026-09-30"]
    assert {i["type"] for i in found} == {"occurrence"} and {i["seriesMasterId"] for i in found} == {series["id"]}
    assert len({i["id"] for i in found}) == 6 and series["id"] not in {i["id"] for i in found}
    assert {i["start"]["dateTime"][11:16] for i in found} == {"10:00"} and found[0]["end"]["dateTime"][11:16] == "10:30"
    assert {i["iCalUId"] for i in found} == {series["iCalUId"]}
    narrow = await calendar.instances(series["id"], "2026-09-16T00:00:00Z", "2026-09-22T00:00:00Z")
    assert _days(narrow) == ["2026-09-16", "2026-09-21"]
    one = await calendar.http.get(f"{GRAPH}/me/events/{found[2]['id']}", headers=calendar.me)
    assert one.status_code == 200 and one.json()["start"] == found[2]["start"] and one.json()["type"] == "occurrence"
    master = (await calendar.http.get(f"{GRAPH}/me/events/{series['id']}", headers=calendar.me)).json()
    assert master["type"] == "seriesMaster" and master["start"]["dateTime"][:10] == "2026-09-14"


async def test_every_other_week_from_a_first_day_and_a_numbered_range_and_a_daily_series_without_end(
    calendar: Calendar,
) -> None:
    biweekly = (
        await calendar.make(
            recurrence={
                "pattern": {
                    "type": "weekly",
                    "interval": 2,
                    "daysOfWeek": ["monday", "friday"],
                    "firstDayOfWeek": "monday",
                },
                "range": {"type": "numbered", "startDate": "2026-09-14", "numberOfOccurrences": 3},
            }
        )
    ).json()
    found = await calendar.instances(biweekly["id"], "2026-09-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert _days(found) == ["2026-09-14", "2026-09-18", "2026-09-28"]
    assert biweekly["recurrence"]["range"]["numberOfOccurrences"] == 3
    daily = (
        await calendar.make(
            recurrence={
                "pattern": {"type": "daily", "interval": 3},
                "range": {"type": "noEnd", "startDate": "2026-09-14"},
            }
        )
    ).json()
    found = await calendar.instances(daily["id"], "2026-09-20T00:00:00Z", "2026-10-03T00:00:00Z")
    assert _days(found) == ["2026-09-20", "2026-09-23", "2026-09-26", "2026-09-29", "2026-10-02"]
    assert "endDate" not in daily["recurrence"]["range"]
    sunday_first = (
        await calendar.make(
            start={"dateTime": "2026-09-16T10:00:00", "timeZone": "UTC"},
            end={"dateTime": "2026-09-16T10:30:00", "timeZone": "UTC"},
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday", "wednesday"]},
                "range": {"type": "numbered", "startDate": "2026-09-16", "numberOfOccurrences": 3},
            },
        )
    ).json()
    found = await calendar.instances(sunday_first["id"], "2026-09-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert _days(found) == ["2026-09-16", "2026-09-21", "2026-09-23"], "the first occurrence is the start day or later"


async def test_a_calendar_view_and_free_busy_hold_the_occurrences_not_the_series(calendar: Calendar) -> None:
    series = (
        await calendar.make(
            recurrence={
                "pattern": {"type": "daily", "interval": 1},
                "range": {"type": "numbered", "startDate": "2026-09-14", "numberOfOccurrences": 3},
            }
        )
    ).json()
    view = await calendar.http.get(
        f"{GRAPH}/me/calendarView",
        params={
            "startDateTime": "2026-09-15T00:00:00Z",
            "endDateTime": "2026-09-17T00:00:00Z",
            "$orderby": "start/dateTime",
        },
        headers=calendar.me,
    )
    inside = [(e["type"], e["start"]["dateTime"][:10]) for e in view.json()["value"] if e["subject"] == "Standup"]
    assert inside == [("occurrence", "2026-09-15"), ("occurrence", "2026-09-16")]
    schedule = await calendar.http.post(
        f"{GRAPH}/me/calendar/getSchedule",
        json={
            "schedules": [AGENT],
            "startTime": {"dateTime": "2026-09-16T10:00:00", "timeZone": "UTC"},
            "endTime": {"dateTime": "2026-09-16T11:00:00", "timeZone": "UTC"},
            "availabilityViewInterval": 30,
        },
        headers=calendar.me,
    )
    assert schedule.json()["value"][0]["availabilityView"] == "20", series["id"]
    plain = await calendar.http.get(f"{GRAPH}/me/events", params={"$orderby": "start/dateTime"}, headers=calendar.me)
    assert [e["id"] for e in plain.json()["value"]] == [series["id"]], "the events list holds the master once"


async def test_what_the_page_does_not_define_about_a_series_is_refused_by_name(calendar: Calendar) -> None:
    def series(pattern: dict[str, Any], **span: Any) -> dict[str, Any]:
        return {"pattern": pattern, "range": {"type": "noEnd", "startDate": "2026-09-14", **span}}

    refused = [
        series({"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 14}),
        series({"type": "daily", "interval": 1}, startDate="2026-09-15"),
        series({"type": "daily", "interval": 1}, recurrenceTimeZone="Pacific Standard Time"),
        series({"type": "weekly", "interval": 1}),
        series({"type": "daily", "interval": 0}),
        series({"type": "daily", "interval": 1, "index": "second"}),
        {"pattern": {"type": "daily", "interval": 1}, "range": {"type": "numbered", "startDate": "2026-09-14"}},
    ]
    for recurrence in refused:
        answered = await calendar.make(recurrence=recurrence)
        assert answered.status_code == 501, recurrence
    listed = await calendar.http.get(f"{GRAPH}/me/events", headers=calendar.me)
    assert listed.json()["value"] == [], "nothing refused was made"
    single = (await calendar.make()).json()
    instances = await calendar.http.get(
        f"{GRAPH}/me/events/{single['id']}/instances",
        params={"startDateTime": "2026-09-01T00:00:00Z", "endDateTime": "2026-10-01T00:00:00Z"},
        headers=calendar.me,
    )
    assert instances.status_code == 501
    made = (
        await calendar.make(
            recurrence=series({"type": "daily", "interval": 1}),
            start={"dateTime": "2026-09-14T10:00:00", "timeZone": "UTC"},
        )
    ).json()
    found = await calendar.instances(made["id"], "2026-09-14T00:00:00Z", "2026-09-16T00:00:00Z")
    for call, path, body in (
        ("patch", f"/me/events/{found[1]['id']}", {"subject": "x"}),
        ("delete", f"/me/events/{found[1]['id']}", None),
        ("post", f"/me/events/{found[1]['id']}/cancel", {}),
        ("patch", f"/me/events/{made['id']}", {"recurrence": made["recurrence"]}),
    ):
        answered = await calendar.http.request(call.upper(), f"{GRAPH}{path}", json=body, headers=calendar.me)
        assert answered.status_code == 501, (call, path)
    without_window = await calendar.http.get(f"{GRAPH}/me/events/{made['id']}/instances", headers=calendar.me)
    assert without_window.status_code == 501 and "startDateTime" in without_window.json()["error"]["message"]


async def test_cancelling_a_meeting_tells_the_attendees_with_the_comment_and_only_its_organizer_may(
    calendar: Calendar,
) -> None:
    http = calendar.http
    sofia = bearer(await signed_in(http, calendar.tenant, "sofia@example.com"))
    made = (
        await calendar.make(
            attendees=[{"emailAddress": {"address": "sofia@example.com", "name": "Sofia Romano"}, "type": "required"}]
        )
    ).json()
    mine = await http.get(f"{GRAPH}/me/events", params={"$orderby": "start/dateTime"}, headers=sofia)
    [theirs] = [e for e in mine.json()["value"] if e["id"] == made["id"]]
    refused = await http.post(f"{GRAPH}/me/events/{theirs['id']}/cancel", json={"comment": "No"}, headers=sofia)
    assert refused.status_code == 400 and "code" not in refused.json()["error"]
    assert refused.json()["error"]["message"] == (
        "Your request can't be completed. You need to be an organizer to cancel a meeting."
    )
    assert (await http.get(f"{GRAPH}/me/events/{made['id']}", headers=calendar.me)).status_code == 200
    sent_before = len(sent_by_agent(calendar.tenant))
    cancelled = await http.post(
        f"{GRAPH}/me/events/{made['id']}/cancel", json={"comment": "The room is gone."}, headers=calendar.me
    )
    assert cancelled.status_code == 202 and cancelled.content == b""
    assert (await http.get(f"{GRAPH}/me/events/{made['id']}", headers=calendar.me)).status_code == 404
    [note] = [
        m
        for m in (
            await http.get(
                f"{GRAPH}/users/sofia@example.com/mailFolders/inbox/messages",
                params={"$orderby": "receivedDateTime desc"},
                headers=calendar.app,
            )
        ).json()["value"]
        if m.get("meetingMessageType") == "meetingCancelled"
    ]
    assert note["@odata.type"] == "#microsoft.graph.eventMessage" and note["from"]["emailAddress"]["address"] == AGENT
    told = sent_by_agent(calendar.tenant)[sent_before:]
    assert [e.after.text for e in told if isinstance(e.after, MessageSnapshot)] == ["The room is gone."]
    assert [e.after.answerable for e in told if isinstance(e.after, MessageSnapshot)] == [False]


async def test_declining_with_a_comment_sends_the_organizer_the_comment(calendar: Calendar) -> None:
    http, tenant = calendar.http, calendar.tenant
    sofia = bearer(await signed_in(http, tenant, "sofia@example.com"))
    made = (
        await calendar.make(
            attendees=[{"emailAddress": {"address": "sofia@example.com", "name": "Sofia Romano"}, "type": "required"}]
        )
    ).json()
    declined = await http.post(
        f"{GRAPH}/me/events/{made['id']}/decline", json={"comment": "I am away.", "sendResponse": True}, headers=sofia
    )
    assert declined.status_code == 202
    response = [
        e
        for e in tenant.store.events()
        if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot) and e.after.text == "I am away."
    ]
    assert len(response) == 1, "the comment is in the run's record of what sofia told the organizer"
    event = (await http.get(f"{GRAPH}/me/events/{made['id']}", headers=calendar.me)).json()
    assert event["attendees"][0]["status"]["response"] == "declined"
    silent = await http.post(
        f"{GRAPH}/me/events/{made['id']}/decline",
        json={"comment": "Really.", "proposedNewTime": {"start": {}, "end": {}}},
        headers=sofia,
    )
    assert silent.status_code == 501, "a proposed time is not served"


async def test_a_file_attached_to_an_event_is_kept_as_sent_and_read_by_its_attendees(calendar: Calendar) -> None:
    http = calendar.http
    sofia = bearer(await signed_in(http, calendar.tenant, "sofia@example.com"))
    made = (
        await calendar.make(
            attendees=[{"emailAddress": {"address": "sofia@example.com", "name": "Sofia Romano"}, "type": "required"}]
        )
    ).json()
    assert made["hasAttachments"] is False
    url = f"{GRAPH}/me/events/{made['id']}/attachments"
    attached = await http.post(
        url,
        json={"@odata.type": "#microsoft.graph.fileAttachment", "name": "menu.txt", "contentType": "text/plain",
              "contentBytes": "bWFjIGFuZCBjaGVlc2UgdG9kYXk="},
        headers=calendar.me,
    )  # fmt: skip
    assert attached.status_code == 201, attached.text
    assert attached.json()["size"] == 20 and attached.json()["contentBytes"] == "bWFjIGFuZCBjaGVlc2UgdG9kYXk="
    assert attached.json()["@odata.context"].endswith(f"events('{made['id']}')/attachments/$entity")
    event = (await http.get(f"{GRAPH}/me/events/{made['id']}", headers=calendar.me)).json()
    assert event["hasAttachments"] is True and event["changeKey"] != made["changeKey"]
    listed = (await http.get(url, headers=sofia)).json()["value"]
    assert [a["name"] for a in listed] == ["menu.txt"]
    one = await http.get(f"{url}/{attached.json()['id']}", headers=sofia)
    assert one.status_code == 200 and one.json()["contentBytes"] == "bWFjIGFuZCBjaGVlc2UgdG9kYXk="
    by_attendee = await http.post(
        url, json={"@odata.type": "#microsoft.graph.fileAttachment", "name": "x", "contentBytes": "aGk="}, headers=sofia
    )
    assert by_attendee.status_code == 501
    item = await http.post(
        url, json={"@odata.type": "#microsoft.graph.itemAttachment", "name": "x"}, headers=calendar.me
    )
    assert item.status_code == 501
