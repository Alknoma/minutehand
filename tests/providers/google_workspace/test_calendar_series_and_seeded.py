"""Calendar's recurring events, out-of-office events, all-day events, calendar time zones, seeded secondary
calendars with readers, and a seeded event's own iCalUID, over a seeded world through the ASGI app."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from minutehand.adapters.providers.google_workspace.provider import GoogleWorkspaceProvider, build
from minutehand.adapters.providers.google_workspace.seed import (
    SeededAttendee,
    SeededCalendar,
    SeededEvent,
    SeededOutOfOffice,
    WorkspaceSeed,
)
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, ProviderSeed, Scenario
from tests.providers.google_workspace.drive_world import AUTH, START, TOKEN, answer, client_for, issue

HOLIDAYS = "en.taiwan#holiday@group.v.calendar.google.com"
WEEKLY = "RRULE:FREQ=WEEKLY;COUNT=6"
EVENTS = "/calendar/v3/calendars/primary/events"
DOV = {"Authorization": "Bearer ya29.dov"}


def seed(**fields: object) -> WorkspaceSeed:
    return WorkspaceSeed.model_validate(
        {
            "time_zones": {"mara": "Asia/Taipei"},
            "calendars": [
                SeededCalendar(
                    id=HOLIDAYS, summary="Holidays in Taiwan", time_zone="Asia/Taipei", readers=["mara"]
                ).model_dump()
            ],
            "events": [
                SeededEvent(
                    calendar="mara",
                    summary="Standup",
                    starts=timedelta(hours=1, minutes=30),
                    lasts=timedelta(minutes=30),
                    recurrence=[WEEKLY],
                    attendees=[SeededAttendee(person="dov")],
                ).model_dump(),
                SeededEvent(calendar=HOLIDAYS, summary="Teachers' Day", day=3, days=1).model_dump(),
                SeededEvent(calendar="mara", summary="Offsite", day=1, days=2).model_dump(),
                SeededEvent(
                    calendar="mara",
                    summary="Imported review",
                    starts=timedelta(hours=5),
                    lasts=timedelta(hours=1),
                    ical_uid="040000008200E00074C5B7101A82E008@example.com",
                ).model_dump(),
                SeededEvent(
                    calendar="dov",
                    summary="Dov away",
                    starts=timedelta(days=2),
                    lasts=timedelta(hours=8),
                    out_of_office=SeededOutOfOffice(
                        auto_decline="declineOnlyNewConflictingInvitations", decline_message="Back on Thursday."
                    ),
                ).model_dump(),
                SeededEvent(
                    calendar="dov",
                    summary="Dov's review",
                    starts=timedelta(days=3, hours=1),
                    lasts=timedelta(hours=1),
                    attendees=[SeededAttendee(person="mara", response="accepted")],
                ).model_dump(),
            ],
            **fields,
        }
    )


def scenario(own: WorkspaceSeed) -> Scenario:
    return Scenario(
        name="taipei_planning",
        goal="The week is planned.",
        owner="mara",
        starts_at=START,
        people=[
            Person(key="mara", name="Mara Lin", email="mara@example.com"),
            Person(key="dov", name="Dov Aranha", email="dov@example.com"),
        ],
        provider_seeds=[ProviderSeed(provider="google_workspace", body=own.model_dump_json())],
    )


@dataclass
class World:
    provider: GoogleWorkspaceProvider
    store: SqliteStore
    clock: RunClock


@pytest.fixture
def world(tmp_path: Path) -> World:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(scenario(seed()), store)
    issue(store, TOKEN, "mara@example.com")
    issue(store, "ya29.dov", "dov@example.com")
    return World(provider, store, clock)


@pytest.fixture
async def api(world: World) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(world.provider, world.store, world.clock) as client:
        yield client


def read(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    return answer(response, status)


def items(listed: dict[str, Any]) -> list[dict[str, Any]]:
    found = listed["items"]
    assert isinstance(found, list)
    return found


def named(listed: dict[str, Any], summary: str) -> list[dict[str, Any]]:
    return [e for e in items(listed) if e["summary"] == summary]


# ---------------------------------------------------------------------- recurring events


async def test_a_weekly_series_of_six_is_expanded_into_instances_with_singleevents_and_answered_as_one_without(
    api: httpx.AsyncClient,
) -> None:
    window = {"timeMin": "2026-09-14T00:00:00Z", "timeMax": "2026-11-30T00:00:00Z"}
    single = read(
        await api.get(EVENTS, params={**window, "singleEvents": "true", "orderBy": "startTime"}, headers=AUTH)
    )
    [master] = named(read(await api.get(EVENTS, headers=AUTH)), "Standup")
    instances = named(single, "Standup")
    assert master["recurrence"] == [WEEKLY] and "recurringEventId" not in master
    assert master["start"] == {"dateTime": "2026-09-14T18:00:00+08:00", "timeZone": "Asia/Taipei"}
    assert len(instances) == 6, [i["start"] for i in instances]
    starts = [i["start"] for i in instances]
    assert starts[0] == {"dateTime": "2026-09-14T18:00:00+08:00", "timeZone": "Asia/Taipei"}
    assert starts[5] == {"dateTime": "2026-10-19T18:00:00+08:00", "timeZone": "Asia/Taipei"}
    assert [i["id"] for i in instances][:2] == [f"{master['id']}_20260914T100000Z", f"{master['id']}_20260921T100000Z"]
    assert {i["recurringEventId"] for i in instances} == {master["id"]}
    assert all(i["originalStartTime"] == i["start"] and "recurrence" not in i for i in instances)
    assert {i["iCalUID"] for i in instances} == {master["iCalUID"]}, "one iCalUID across the series"
    got = read(await api.get(f"{EVENTS}/{instances[2]['id']}", headers=AUTH))
    assert got["recurringEventId"] == master["id"] and got["start"] == starts[2]
    later = read(
        await api.get(EVENTS, params={"timeMin": "2026-10-20T00:00:00Z", "singleEvents": "true"}, headers=AUTH)
    )
    assert named(later, "Standup") == [], "a series of six has none after its sixth"
    meeting = read(await api.get(EVENTS, params={"timeMin": "2026-10-01T00:00:00Z"}, headers=AUTH))
    assert [e["id"] for e in named(meeting, "Standup")] == [master["id"]], "an instance in the window lists the series"
    past = read(await api.get(EVENTS, params={"timeMin": "2026-10-20T00:00:00Z"}, headers=AUTH))
    assert named(past, "Standup") == []
    busy = read(
        await api.post(
            "/calendar/v3/freeBusy",
            json={
                "timeMin": "2026-09-21T00:00:00Z",
                "timeMax": "2026-09-22T00:00:00Z",
                "items": [{"id": "dov@example.com"}],
            },
            headers=AUTH,
        )
    )
    assert busy["calendars"]["dov@example.com"]["busy"] == [
        {"start": "2026-09-21T10:00:00Z", "end": "2026-09-21T10:30:00Z"}
    ], "a guest is busy at each instance"


async def test_a_recurring_event_is_inserted_patched_updated_and_read_and_one_instance_is_not_changed(
    api: httpx.AsyncClient,
) -> None:
    body = {
        "summary": "Review",
        "start": {"dateTime": "2026-09-15T09:00:00", "timeZone": "Asia/Taipei"},
        "end": {"dateTime": "2026-09-15T10:00:00", "timeZone": "Asia/Taipei"},
        "recurrence": [WEEKLY],
    }
    made = read(await api.post(EVENTS, json=body, headers=AUTH))
    assert made["recurrence"] == [WEEKLY] and made["iCalUID"] == f"{made['id']}@google.com"
    patched = read(await api.patch(f"{EVENTS}/{made['id']}", json={"summary": "Weekly review"}, headers=AUTH))
    assert patched["recurrence"] == [WEEKLY] and patched["summary"] == "Weekly review"
    biweekly = "RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=3"
    updated = read(await api.put(f"{EVENTS}/{made['id']}", json={**body, "recurrence": [biweekly]}, headers=AUTH))
    assert updated["recurrence"] == [biweekly]
    got = read(await api.get(f"{EVENTS}/{made['id']}", headers=AUTH))
    assert got["recurrence"] == [biweekly]
    listed = read(await api.get(EVENTS, params={"timeMin": "2026-09-14T00:00:00Z", "timeMax": "2026-12-01T00:00:00Z",
                                                  "singleEvents": "true"}, headers=AUTH))  # fmt: skip
    assert [i["start"]["dateTime"] for i in named(listed, "Review")] == [
        "2026-09-15T09:00:00+08:00",
        "2026-09-29T09:00:00+08:00",
        "2026-10-13T09:00:00+08:00",
    ]
    instance = f"{made['id']}_20260929T010000Z"
    assert read(await api.get(f"{EVENTS}/{instance}", headers=AUTH))["recurringEventId"] == made["id"]
    assert (await api.patch(f"{EVENTS}/{instance}", json={"summary": "Moved"}, headers=AUTH)).status_code == 501
    assert (await api.delete(f"{EVENTS}/{instance}", headers=AUTH)).status_code == 501


async def test_a_recurrence_line_not_served_and_an_unbounded_series_with_no_timemax_are_refused_by_name(
    api: httpx.AsyncClient,
) -> None:
    slot = {"start": {"dateTime": "2026-09-15T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-09-15T10:00:00", "timeZone": "UTC"}}  # fmt: skip
    exdate = await api.post(
        EVENTS, json={**slot, "summary": "x", "recurrence": [WEEKLY, "EXDATE:20260922T090000Z"]}, headers=AUTH
    )
    assert exdate.status_code == 501 and "EXDATE" in exdate.text
    read(await api.post(EVENTS, json={**slot, "summary": "Forever", "recurrence": ["RRULE:FREQ=DAILY"]}, headers=AUTH))
    unbounded = await api.get(EVENTS, params={"singleEvents": "true"}, headers=AUTH)
    assert unbounded.status_code == 501 and "timeMax" in unbounded.text
    bounded = read(await api.get(EVENTS, params={"singleEvents": "true", "timeMin": "2026-09-15T00:00:00Z",
                                                   "timeMax": "2026-09-18T00:00:00Z"}, headers=AUTH))  # fmt: skip
    assert len(named(bounded, "Forever")) == 3


# ---------------------------------------------------------------------- out of office


async def test_an_out_of_office_event_is_inserted_read_listed_and_patched_and_declines_what_it_meets(
    api: httpx.AsyncClient,
) -> None:
    review = named(read(await api.get(EVENTS, headers=AUTH)), "Dov's review")[0]
    away = {
        "summary": "Out of office",
        "eventType": "outOfOffice",
        "transparency": "opaque",
        "start": {"dateTime": "2026-09-17T08:00:00Z"},
        "end": {"dateTime": "2026-09-17T12:00:00Z"},
        "outOfOfficeProperties": {"autoDeclineMode": "declineAllConflictingInvitations", "declineMessage": "On leave."},
    }
    made = read(await api.post(EVENTS, json=away, headers=AUTH))
    assert made["eventType"] == "outOfOffice"
    assert made["outOfOfficeProperties"] == away["outOfOfficeProperties"]
    got = read(await api.get(f"{EVENTS}/{made['id']}", headers=AUTH))
    assert got["outOfOfficeProperties"] == away["outOfOfficeProperties"]
    only = read(await api.get(EVENTS, params={"eventTypes": "outOfOffice"}, headers=AUTH))
    assert [e["id"] for e in items(only)] == [made["id"]]
    every = read(await api.get(EVENTS, headers=AUTH))
    assert made["id"] in [e["id"] for e in items(every)], "a list with no eventTypes answers every type"
    declined = read(await api.get(f"{EVENTS}/{review['id']}", headers=AUTH))
    [mine] = [a for a in declined["attendees"] if a["email"] == "mara@example.com"]
    assert (mine["responseStatus"], mine["comment"]) == ("declined", "On leave."), "an existing invitation declined"
    patched = read(
        await api.patch(
            f"{EVENTS}/{made['id']}",
            json={"outOfOfficeProperties": {"autoDeclineMode": "declineNone", "declineMessage": "Later."}},
            headers=AUTH,
        )
    )
    assert patched["outOfOfficeProperties"]["autoDeclineMode"] == "declineNone"


async def test_a_new_invitation_meeting_a_guests_out_of_office_event_is_declined_with_its_message(
    api: httpx.AsyncClient,
) -> None:
    asked = {
        "summary": "Budget",
        "start": {"dateTime": "2026-09-16T10:00:00Z"},
        "end": {"dateTime": "2026-09-16T11:00:00Z"},
        "attendees": [{"email": "dov@example.com"}],
    }
    made = read(await api.post(EVENTS, json=asked, headers=AUTH))
    [dov] = made["attendees"]
    assert (dov["responseStatus"], dov["comment"]) == ("declined", "Back on Thursday.")
    clear = read(await api.post(EVENTS, json={**asked, "start": {"dateTime": "2026-09-16T20:00:00Z"},
                                                "end": {"dateTime": "2026-09-16T21:00:00Z"}}, headers=AUTH))  # fmt: skip
    assert clear["attendees"][0]["responseStatus"] == "needsAction", "outside the absence the invitation stands"
    seeded = named(read(await api.get("/calendar/v3/calendars/primary/events", headers=DOV)), "Dov away")[0]
    assert seeded["eventType"] == "outOfOffice"
    assert seeded["outOfOfficeProperties"]["autoDeclineMode"] == "declineOnlyNewConflictingInvitations"


async def test_an_out_of_office_event_all_day_or_on_a_secondary_calendar_or_with_guests_is_refused_by_name(
    api: httpx.AsyncClient,
) -> None:
    away = {
        "eventType": "outOfOffice",
        "start": {"dateTime": "2026-09-17T08:00:00Z"},
        "end": {"dateTime": "2026-09-17T12:00:00Z"},
        "outOfOfficeProperties": {"autoDeclineMode": "declineNone"},
    }
    all_day = await api.post(
        EVENTS, json={**away, "start": {"date": "2026-09-17"}, "end": {"date": "2026-09-18"}}, headers=AUTH
    )
    assert all_day.status_code == 501 and "all-day" in all_day.text
    guests = await api.post(EVENTS, json={**away, "attendees": [{"email": "dov@example.com"}]}, headers=AUTH)
    assert guests.status_code == 501 and "attendees" in guests.text
    team = read(await api.post("/calendar/v3/calendars", json={"summary": "Team", "timeZone": "UTC"}, headers=AUTH))
    secondary = await api.post(f"/calendar/v3/calendars/{team['id']}/events", json=away, headers=AUTH)
    assert secondary.status_code == 501 and "primary" in secondary.text
    unpropertied = await api.post(
        EVENTS, json={k: v for k, v in away.items() if k != "outOfOfficeProperties"}, headers=AUTH
    )
    assert unpropertied.status_code == 501


# ---------------------------------------------------------------------- all day, time zones, secondary calendars


async def test_a_primary_calendar_is_in_its_seeded_time_zone_in_its_list_entry_resource_events_and_times(
    api: httpx.AsyncClient,
) -> None:
    listed = read(await api.get("/calendar/v3/users/me/calendarList", headers=AUTH))
    [primary] = [e for e in items(listed) if e.get("primary")]
    assert primary["timeZone"] == "Asia/Taipei"
    assert read(await api.get("/calendar/v3/calendars/primary", headers=AUTH))["timeZone"] == "Asia/Taipei"
    events = read(await api.get(EVENTS, headers=AUTH))
    assert events["timeZone"] == "Asia/Taipei"
    [imported] = named(events, "Imported review")
    assert imported["start"] == {"dateTime": "2026-09-14T21:30:00+08:00"}, "a seeded time in the calendar's zone"
    dov = read(await api.get("/calendar/v3/calendars/primary", headers=DOV))
    assert dov["timeZone"] == "UTC", "a person the seed gives no zone, and no working hours, is in UTC"


async def test_a_seeded_all_day_event_is_dated_in_its_calendars_zone_with_an_exclusive_end(
    api: httpx.AsyncClient,
) -> None:
    [offsite] = named(read(await api.get(EVENTS, headers=AUTH)), "Offsite")
    assert (offsite["start"], offsite["end"]) == ({"date": "2026-09-15"}, {"date": "2026-09-17"})
    taipei_day = {"timeMin": "2026-09-16T00:00:00+08:00", "timeMax": "2026-09-17T00:00:00+08:00"}
    assert named(read(await api.get(EVENTS, params=taipei_day, headers=AUTH)), "Offsite")
    after = {"timeMin": "2026-09-16T16:00:00Z"}
    assert named(read(await api.get(EVENTS, params=after, headers=AUTH)), "Offsite") == [], (
        "its end is midnight of the 17th in Taipei, 16:00 UTC on the 16th"
    )


async def test_a_seeded_holidays_calendar_is_on_its_readers_list_as_reader_with_its_events_and_hidden_from_others(
    api: httpx.AsyncClient,
) -> None:
    listed = read(await api.get("/calendar/v3/users/me/calendarList", headers=AUTH))
    [holidays] = [e for e in items(listed) if e["id"] == HOLIDAYS]
    assert holidays["accessRole"] == "reader" and holidays["summary"] == "Holidays in Taiwan"
    assert "dataOwner" not in holidays and "primary" not in holidays
    path = f"/calendar/v3/calendars/{quote(HOLIDAYS, safe='')}"
    events = read(await api.get(f"{path}/events", headers=AUTH))
    [day] = items(events)
    assert (day["summary"], day["start"], day["end"]) == (
        "Teachers' Day",
        {"date": "2026-09-17"},
        {"date": "2026-09-18"},
    )
    assert day["organizer"]["email"] == HOLIDAYS
    written = await api.post(f"{path}/events", json={"summary": "x", "start": {"date": "2026-09-20"},
                                                     "end": {"date": "2026-09-21"}}, headers=AUTH)  # fmt: skip
    assert written.status_code == 501, "a reader does not write"
    theirs = read(await api.get("/calendar/v3/users/me/calendarList", headers=DOV))
    assert HOLIDAYS not in [e["id"] for e in items(theirs)]
    assert (await api.get(f"{path}/events", headers=DOV)).status_code == 404


async def test_a_seeded_events_own_icaluid_is_answered_as_given(api: httpx.AsyncClient) -> None:
    [imported] = named(read(await api.get(EVENTS, headers=AUTH)), "Imported review")
    assert imported["iCalUID"] == "040000008200E00074C5B7101A82E008@example.com"
    got = read(await api.get(f"{EVENTS}/{imported['id']}", headers=AUTH))
    assert got["iCalUID"] == "040000008200E00074C5B7101A82E008@example.com"


# ---------------------------------------------------------------------- the seed's own refusals


@pytest.mark.parametrize(
    ("fields", "says"),
    [
        ({"time_zones": {"zed": "UTC"}}, "time_zones names 'zed'"),
        ({"time_zones": {"mara": "Mars/Olympus"}}, "no IANA time zone"),
        ({"calendars": [{"id": "x@group.calendar.google.com", "summary": "X", "readers": ["zed"]}]}, "read by 'zed'"),
    ],
)
def test_a_seed_naming_nobody_or_no_zone_is_refused(tmp_path: Path, fields: dict[str, object], says: str) -> None:
    store = SqliteStore(tmp_path / "world.db", "root", RunClock(START))
    with pytest.raises(ValueError, match=says):
        build().seed(scenario(seed(**fields)), store)


def test_a_seeded_event_neither_timed_nor_all_day_is_refused() -> None:
    with pytest.raises(ValueError, match=r"timed \(starts and lasts\) or all day"):
        SeededEvent(calendar="mara", summary="Both", starts=timedelta(0), lasts=timedelta(hours=1), day=1, days=1)


async def test_a_series_keeps_its_wall_clock_time_in_its_zone_across_a_change_of_offset(api: httpx.AsyncClient) -> None:
    body = {
        "summary": "Across the change",
        "start": {"dateTime": "2026-10-26T09:00:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-10-26T09:30:00", "timeZone": "America/New_York"},
        "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"],
    }
    made = read(await api.post(EVENTS, json=body, headers=AUTH))
    listed = read(await api.get(EVENTS, params={"singleEvents": "true", "timeMin": "2026-10-20T00:00:00Z",
                                                  "timeMax": "2026-11-20T00:00:00Z"}, headers=AUTH))  # fmt: skip
    instances = [i for i in items(listed) if i.get("recurringEventId") == made["id"]]
    assert [i["start"]["dateTime"] for i in instances] == [
        "2026-10-26T09:00:00-04:00",
        "2026-11-02T09:00:00-05:00",
        "2026-11-09T09:00:00-05:00",
    ]
    assert [i["id"].rsplit("_", 1)[1] for i in instances] == [
        "20261026T130000Z",
        "20261102T140000Z",
        "20261109T140000Z",
    ]


async def test_changing_an_events_type_after_it_is_made_is_refused_by_name(api: httpx.AsyncClient) -> None:
    made = read(await api.post(EVENTS, json={"summary": "Plain", "start": {"dateTime": "2026-09-18T08:00:00Z"},
                                               "end": {"dateTime": "2026-09-18T09:00:00Z"}}, headers=AUTH))  # fmt: skip
    changed = await api.patch(
        f"{EVENTS}/{made['id']}",
        json={"eventType": "outOfOffice", "outOfOfficeProperties": {"autoDeclineMode": "declineNone"}},
        headers=AUTH,
    )
    assert changed.status_code == 501 and "eventType" in changed.text
