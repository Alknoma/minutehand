"""Google Calendar v3 driven by Google's own client, stock `googleapiclient` over `httplib2` with stock `google-auth`,
in a process of its own configured only by the environment Minutehand hands an agent, through the proxy.

What a proactive agent does with a calendar: it finds a free slot, invites people, and reads back who answered
(each guest's answer written beside the client by the provider's `land`, as the run loop does at its moment); then
it moves the meeting, cancels it, and syncs what changed."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.providers.google_workspace.seed import SeededAttendee, SeededEvent, WorkspaceSeed
from minutehand.domain.people import PersonReply, Press
from minutehand.domain.scenario import Person, ProviderSeed, Scenario, Scripted, SignIn, WorkingHours
from minutehand.domain.world import (
    Actor,
    InteractionSnapshot,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
)
from tests.providers.google_workspace.proxied import REFRESH, START, serving

pytestmark = pytest.mark.timeout(120)

SCENARIO = Scenario(
    name="planning_call",
    goal="A planning call with Dov and Rosa is on everyone's calendar.",
    owner="mara",
    starts_at=START,
    people=[
        Person(
            key="mara",
            name="Mara Lindqvist",
            email="mara@example.com",
            reply=Scripted(replies=[]),
            working_hours=WorkingHours(timezone="Europe/Berlin"),
        ),
        Person(key="dov", name="Dov Aranha", email="dov@example.com", reply=Scripted(replies=[])),
        Person(key="rosa", name="Rosa Field", email="rosa@example.com", reply=Scripted(replies=[])),
    ],
    sign_ins=[SignIn(provider="google_workspace", credential=REFRESH, person="mara")],
    provider_seeds=[
        ProviderSeed(
            provider="google_workspace",
            body=WorkspaceSeed(
                events=[
                    SeededEvent(
                        calendar="dov",
                        summary="Dov's supplier review",
                        starts=timedelta(hours=1, minutes=30),
                        lasts=timedelta(hours=1),
                        attendees=[SeededAttendee(person="mara", response="accepted")],
                    ),
                    SeededEvent(
                        calendar="rosa",
                        summary="Focus time",
                        starts=timedelta(hours=2),
                        lasts=timedelta(hours=2),
                        transparent=True,
                    ),
                ]
            ).model_dump_json(),
        )
    ],
)

CALENDAR = """
calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
"""


async def test_an_invitation_asks_its_guests_and_their_answers_land_on_the_event(tmp_path: Path) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await google.client(
            CALENDAR
            + """
listed = calendar.calendarList().list().execute()
busy = calendar.freebusy().query(body={
    "timeMin": "2026-09-14T08:00:00Z", "timeMax": "2026-09-14T18:00:00Z",
    "items": [{"id": "primary"}, {"id": "dov@example.com"}, {"id": "rosa@example.com"}, {"id": "zed@example.com"}],
}).execute()
made = calendar.events().insert(calendarId="primary", sendUpdates="all", body={
    "summary": "Planning call", "location": "Room 4", "description": "Agree the Q4 plan.",
    "start": {"dateTime": "2026-09-14T15:00:00", "timeZone": "Europe/Berlin"},
    "end": {"dateTime": "2026-09-14T15:30:00+02:00"},
    "attendees": [{"email": "dov@example.com"}, {"email": "rosa@example.com"}, {"email": "mara@example.com"}],
}).execute()
day = calendar.events().list(calendarId="primary", timeMin="2026-09-14T00:00:00Z", timeMax="2026-09-15T00:00:00Z",
                             singleEvents=True, orderBy="startTime").execute()
say(listed=listed, busy=busy, made=made, day=[e["summary"] for e in day["items"]], sync=day["nextSyncToken"])
wait()
answered = calendar.events().get(calendarId="primary", eventId=made["id"]).execute()
moved = calendar.events().patch(calendarId="primary", eventId=made["id"], body={
    "start": {"dateTime": "2026-09-14T16:00:00+02:00"}, "end": {"dateTime": "2026-09-14T16:30:00+02:00"},
}).execute()
changes = calendar.events().list(calendarId="primary", syncToken=day["nextSyncToken"]).execute()
calendar.events().delete(calendarId="primary", eventId=made["id"], sendUpdates="all").execute()
gone = refused(lambda: calendar.events().delete(calendarId="primary", eventId=made["id"]).execute())
since = calendar.events().list(calendarId="primary", syncToken=changes["nextSyncToken"]).execute()
say(answered=answered, moved=moved, changes=[e["id"] for e in changes["items"]], gone=gone, since=since["items"])
"""
        )
        first = await client.heard()
        listed = first["listed"]
        assert isinstance(listed, dict)
        [mine] = listed["items"]
        assert mine["id"] == "mara@example.com" and mine["primary"] is True and mine["timeZone"] == "Europe/Berlin"
        busy = first["busy"]
        assert isinstance(busy, dict)
        calendars = busy["calendars"]
        assert calendars["primary"]["busy"] == [{"start": "2026-09-14T10:00:00Z", "end": "2026-09-14T11:00:00Z"}]
        assert calendars["dov@example.com"]["busy"] == calendars["primary"]["busy"]
        assert calendars["rosa@example.com"]["busy"] == []
        assert calendars["zed@example.com"] == {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}
        made = first["made"]
        assert isinstance(made, dict)
        assert made["status"] == "confirmed" and made["organizer"] == {"email": "mara@example.com", "self": True}
        assert made["start"]["dateTime"] == "2026-09-14T15:00:00+02:00"
        assert [(a["email"], a["responseStatus"], "organizer" in a) for a in made["attendees"]] == [
            ("dov@example.com", "needsAction", False),
            ("rosa@example.com", "needsAction", False),
            ("mara@example.com", "accepted", True),
        ]
        assert first["day"] == ["Dov's supplier review", "Planning call"]

        [invited] = [
            e for e in google.store.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
        ]
        assert isinstance(invited.after, MessageSnapshot)
        assert invited.entity.external_id == made["id"] and invited.after.channel == made["id"]
        assert invited.after.recipient_emails == ["dov@example.com", "rosa@example.com"]
        assert [(a.label, a.action_id) for a in invited.after.actions] == [
            ("Yes", "accepted"),
            ("Maybe", "tentative"),
            ("No", "declined"),
        ]
        assert invited.after.text.startswith("Planning call\n2026-09-14T15:00:00+02:00 to 2026-09-14T15:30:00+02:00")

        google.clock.jump(START + timedelta(hours=2))
        yes = invited.after.actions[0]
        for reply in (
            PersonReply(
                person="dov",
                in_reply_to=invited.entity,
                text="Yes",
                at=google.clock.now(),
                press=Press(action_id=yes.action_id, label=yes.label),
            ),
            PersonReply(person="rosa", in_reply_to=invited.entity, text="Could we do 4pm?", at=google.clock.now()),
        ):
            await google.provider.land(reply, google.store, google.clock)
        await client.go()
        second = await client.heard()
        await client.finished()

        answered = second["answered"]
        assert isinstance(answered, dict)
        assert [(a["email"], a["responseStatus"], a.get("comment")) for a in answered["attendees"]] == [
            ("dov@example.com", "accepted", None),
            ("rosa@example.com", "needsAction", "Could we do 4pm?"),
            ("mara@example.com", "accepted", None),
        ]
        assert answered["updated"] == "2026-09-14T10:30:00.000Z"
        moved = second["moved"]
        assert isinstance(moved, dict) and moved["sequence"] == 1
        assert [a["responseStatus"] for a in moved["attendees"]] == ["accepted", "needsAction", "accepted"]
        assert second["changes"] == [made["id"]]
        assert second["gone"] == [410, "deleted"]
        since = second["since"]
        assert isinstance(since, list)
        assert since == [
            {
                "kind": "calendar#event",
                "etag": since[0]["etag"],
                "id": made["id"],
                "status": "cancelled",
            }
        ]

        people = [e for e in google.store.events() if e.actor is Actor.PERSON]
        pressed = [e.after for e in people if isinstance(e.after, InteractionSnapshot)]
        assert [(p.person, p.action_id, p.label) for p in pressed] == [("dov", "accepted", "Yes")]
        said = [e.after for e in people if isinstance(e.after, MessageSnapshot)]
        assert [(s.text, s.thread_of) for s in said] == [("Could we do 4pm?", made["id"])]
        assert all(e.sim_time == START + timedelta(hours=2) for e in people)
        deleted = [e for e in google.store.events() if e.operation is Operation.DELETE]
        assert [(e.actor, e.entity.external_id) for e in deleted] == [(Actor.AGENT, made["id"])]


async def test_an_empty_range_another_calendar_a_guests_change_a_taken_id_and_recurrence_are_refused(
    tmp_path: Path,
) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await google.client(
            CALENDAR
            + """
review = [e for e in calendar.events().list(calendarId="primary").execute()["items"]
          if e["summary"] == "Dov's supplier review"][0]
slot = {"start": {"dateTime": "2026-09-15T10:00:00Z"}, "end": {"dateTime": "2026-09-15T10:30:00Z"}}
calendar.events().insert(calendarId="primary", body={"id": "planning0001", "summary": "One", **slot}).execute()
say(
    empty=refused(lambda: calendar.events().insert(calendarId="primary", body={
        "summary": "Backwards", "start": {"dateTime": "2026-09-15T10:00:00Z"},
        "end": {"dateTime": "2026-09-15T09:00:00Z"}}).execute()),
    insert_elsewhere=refused(lambda: calendar.events().insert(calendarId="dov@example.com", body={"summary": "x", **slot}).execute()),
    elsewhere=refused(lambda: calendar.events().list(calendarId="dov@example.com").execute()),
    not_organizer=refused(lambda: calendar.events().patch(calendarId="primary", eventId=review["id"],
                                                          body={"summary": "Mine now"}).execute()),
    taken=refused(lambda: calendar.events().insert(calendarId="primary",
                                                   body={"id": "planning0001", "summary": "Two", **slot}).execute()),
    recurring=refused(lambda: calendar.events().insert(calendarId="primary", body={
        "summary": "Weekly", "recurrence": ["RRULE:FREQ=WEEKLY"], **slot}).execute()),
    instances=refused(lambda: calendar.events().instances(calendarId="primary", eventId="planning0001").execute()),
    review_self=[a.get("self") for a in review["attendees"]], review_organizer=review["organizer"].get("self"),
)
"""
        )
        seen = await client.heard()
        await client.finished()
        assert seen["empty"] == [400, "timeRangeEmpty"]
        assert seen["insert_elsewhere"] == [404, "notFound"]
        assert seen["elsewhere"] == [404, "notFound"]
        assert seen["not_organizer"] == [403, "forbiddenForNonOrganizer"]
        assert seen["taken"] == [409, "duplicate"]
        assert seen["recurring"] == [501, "notImplemented"]
        assert seen["instances"] == [501, "notImplemented"]
        assert seen["review_self"] == [True] and seen["review_organizer"] is None
        made = [e for e in google.store.events() if e.actor is Actor.AGENT and e.operation is Operation.CREATE]
        assert [e.entity.external_id for e in made] == ["planning0001"]
        assert isinstance(made[0].after, RecordSnapshot)  # an event with no guests asks nobody
