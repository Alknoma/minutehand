"""What a Calendar client relies on beyond events on its own primary, driven by Google's own client (stock
`googleapiclient` over `httplib2`) in a process of its own through the proxy: `calendars.get`, conditional writes with
`If-Match`, deleted events with `showDeleted`, and secondary calendars shared with `acl.insert`. `CLAIMS.md` gives
the page each behaviour is read from."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.domain.people import PersonReply, Press
from minutehand.domain.scenario import SignIn
from minutehand.domain.world import Actor, MessageSnapshot
from tests.providers.google_workspace.proxied import REFRESH, START, serving
from tests.providers.google_workspace.test_calendar_through_proxy import CALENDAR, SCENARIO

pytestmark = pytest.mark.timeout(120)

DOV = "1//dov-refresh-token"
ROSA = "1//rosa-refresh-token"
SHARED = SCENARIO.model_copy(
    update={
        "sign_ins": [
            SignIn(provider="google_workspace", credential=REFRESH, person="mara"),
            SignIn(provider="google_workspace", credential=DOV, person="dov"),
            SignIn(provider="google_workspace", credential=ROSA, person="rosa"),
        ]
    }
)
AS = """
def as_person(refresh):
    found = Credentials(token=None, refresh_token=refresh, token_uri="https://oauth2.googleapis.com/token",
                        client_id="1234.apps.googleusercontent.com", client_secret="client-secret", scopes=SCOPES)
    return build("calendar", "v3", credentials=found, cache_discovery=False)

def conditional(request, etag):
    request.headers["If-Match"] = etag
    return request.execute()

def body_of(call):
    try:
        call()
    except HttpError as error:
        return [error.resp.status, json.loads(error.content)]
    return None

SLOT = {"start": {"dateTime": "2026-09-15T10:00:00Z"}, "end": {"dateTime": "2026-09-15T10:30:00Z"}}
"""


async def test_calendars_get_answers_primary_and_a_calendar_the_caller_cannot_see_is_not_found(tmp_path: Path) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await google.client(
            CALENDAR
            + """
say(
    primary=calendar.calendars().get(calendarId="primary").execute(),
    by_address=calendar.calendars().get(calendarId="mara@example.com").execute(),
    unshared=refused(lambda: calendar.calendars().get(calendarId="dov@example.com").execute()),
    nobody=refused(lambda: calendar.calendars().get(calendarId="zed@example.com").execute()),
    listed=calendar.calendarList().list().execute()["items"][0]["timeZone"],
)
"""
        )
        seen = await client.heard()
        await client.finished()
    primary = seen["primary"]
    assert isinstance(primary, dict)
    assert primary == {
        "kind": "calendar#calendar",
        "etag": primary["etag"],
        "id": "mara@example.com",
        "summary": "mara@example.com",
        "timeZone": "Europe/Berlin",
    }
    assert seen["by_address"] == primary
    assert seen["listed"] == primary["timeZone"], "the zone calendarList answers"
    assert seen["unshared"] == [404, "notFound"] and seen["nobody"] == [404, "notFound"]


async def test_a_write_naming_a_stale_etag_is_refused_412_and_changes_nothing(tmp_path: Path) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await google.client(
            CALENDAR
            + AS
            + """
made = calendar.events().insert(calendarId="primary", body={
    "summary": "Planning call", "attendees": [{"email": "dov@example.com"}], **SLOT}).execute()
events = calendar.events()
moved = conditional(events.patch(calendarId="primary", eventId=made["id"], body={"summary": "Moved"}), made["etag"])
stale = body_of(lambda: conditional(events.patch(calendarId="primary", eventId=made["id"],
                                                 body={"summary": "Stale"}), made["etag"]))
stale_update = body_of(lambda: conditional(events.update(calendarId="primary", eventId=made["id"],
                                                         body={"summary": "Stale", **SLOT}), made["etag"]))
stale_delete = body_of(lambda: conditional(events.delete(calendarId="primary", eventId=made["id"]), made["etag"]))
after = events.get(calendarId="primary", eventId=made["id"]).execute()
say(made=made, moved=moved, stale=stale, stale_update=stale_update, stale_delete=stale_delete, after=after)
wait()
answered = events.get(calendarId="primary", eventId=made["id"]).execute()
gone = conditional(events.delete(calendarId="primary", eventId=made["id"]), answered["etag"])
say(answered=answered, gone=gone, stale_after_answer=after["etag"] == answered["etag"])
"""
        )
        seen = await client.heard()
        made, moved, after = seen["made"], seen["moved"], seen["after"]
        assert isinstance(made, dict) and isinstance(moved, dict) and isinstance(after, dict)
        assert moved["summary"] == "Moved" and moved["etag"] != made["etag"], "a matching etag proceeds"
        refusal = {
            "error": {
                "code": 412,
                "message": "Precondition Failed",
                "errors": [
                    {
                        "domain": "global",
                        "reason": "conditionNotMet",
                        "message": "Precondition Failed",
                        "locationType": "header",
                        "location": "If-Match",
                    }
                ],
            }
        }
        assert seen["stale"] == [412, refusal]
        assert seen["stale_update"] == [412, refusal]
        assert seen["stale_delete"] == [412, refusal]
        assert after["summary"] == "Moved" and after["etag"] == moved["etag"], "nothing changed"

        google.clock.jump(START + timedelta(hours=1))
        asked = next(
            e for e in google.store.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
        )
        await google.provider.land(
            PersonReply(
                person="dov",
                in_reply_to=asked.entity,
                text="Yes",
                at=google.clock.now(),
                press=Press(action_id="accepted", label="Yes"),
            ),
            google.store,
            google.clock,
        )
        await client.go()
        last = await client.heard()
        await client.finished()
    assert last["stale_after_answer"] is False, "a guest's answer moves the etag"
    assert last["gone"] == ""


async def test_show_deleted_answers_a_deleted_event_in_the_window_as_cancelled_and_get_serves_it(
    tmp_path: Path,
) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await google.client(
            CALENDAR
            + AS
            + """
events = calendar.events()
inside = events.insert(calendarId="primary", body={"summary": "Inside", **SLOT}).execute()
outside = events.insert(calendarId="primary", body={"summary": "Outside",
    "start": {"dateTime": "2026-09-20T10:00:00Z"}, "end": {"dateTime": "2026-09-20T10:30:00Z"}}).execute()
kept = events.insert(calendarId="primary", body={"summary": "Kept",
    "start": {"dateTime": "2026-09-15T09:00:00Z"}, "end": {"dateTime": "2026-09-15T09:30:00Z"}}).execute()
say(ready=True)
wait()
events.delete(calendarId="primary", eventId=inside["id"]).execute()
events.delete(calendarId="primary", eventId=outside["id"]).execute()
window = dict(calendarId="primary", timeMin="2026-09-15T00:00:00Z", timeMax="2026-09-16T00:00:00Z",
              singleEvents=True, orderBy="startTime")
say(
    shown=events.list(showDeleted=True, **window).execute()["items"],
    hidden=[e["id"] for e in events.list(**window).execute()["items"]],
    since=[e["id"] for e in events.list(showDeleted=True, updatedMin="2026-09-14T08:00:00Z", **window).execute()["items"]],
    before=[e["id"] for e in events.list(showDeleted=True, updatedMin="2026-09-14T09:00:00Z", **window).execute()["items"]],
    regardless=[e["id"] for e in events.list(updatedMin="2026-09-14T09:00:00Z", **window).execute()["items"]],
    got=events.get(calendarId="primary", eventId=inside["id"]).execute(),
    again=refused(lambda: events.delete(calendarId="primary", eventId=inside["id"]).execute()),
    inside=inside["id"], kept=kept["id"],
)
"""
        )
        await client.heard()
        google.clock.jump(START + timedelta(hours=2))
        await client.go()
        seen = await client.heard()
        await client.finished()
    inside, kept = seen["inside"], seen["kept"]
    shown = seen["shown"]
    assert isinstance(shown, list)
    assert [(e["id"], e["status"]) for e in shown] == [(kept, "confirmed"), (inside, "cancelled")]
    cancelled = shown[1]
    assert cancelled["summary"] == "Inside", "the organizer's calendar keeps a cancelled event's details"
    assert cancelled["updated"] == "2026-09-14T10:30:00.000Z", "updated when it was deleted"
    assert seen["hidden"] == [kept], "without showDeleted it is left out"
    assert seen["since"] == [kept, inside], "updatedMin before every change"
    assert seen["before"] == [inside], "only the deletion is after this updatedMin"
    assert seen["regardless"] == [inside], "with updatedMin a deletion is included regardless of showDeleted"
    got = seen["got"]
    assert isinstance(got, dict) and got["status"] == "cancelled" and got["id"] == inside
    assert seen["again"] == [410, "deleted"]


async def test_a_secondary_calendar_shared_with_a_writer_is_theirs_to_add_and_write_and_hidden_from_others(
    tmp_path: Path,
) -> None:
    async with serving(tmp_path, SHARED) as google:
        client = await google.client(
            CALENDAR
            + AS
            + f"""
dov, rosa = as_person({DOV!r}), as_person({ROSA!r})
made = calendar.calendars().insert(body={{"summary": "Supplier reviews", "timeZone": "Europe/Berlin"}}).execute()
mine = [e for e in calendar.calendarList().list().execute()["items"] if e["id"] == made["id"]]
sync = calendar.events().list(calendarId=made["id"]).execute()["nextSyncToken"]
before = refused(lambda: dov.events().list(calendarId=made["id"]).execute())
rule = calendar.acl().insert(calendarId=made["id"], body={{
    "role": "writer", "scope": {{"type": "user", "value": "dov@example.com"}}}}).execute()
unlisted = [e["id"] for e in dov.calendarList().list().execute()["items"]]
added = dov.calendarList().insert(body={{"id": made["id"]}}).execute()
listed = [(e["id"], e["accessRole"]) for e in dov.calendarList().list().execute()["items"]]
written = dov.events().insert(calendarId=made["id"], body={{"summary": "Acme review", **SLOT}}).execute()
read = calendar.events().list(calendarId=made["id"]).execute()
since = calendar.events().list(calendarId=made["id"], syncToken=sync).execute()
say(
    made=made, mine=mine, before=before, rule=rule, unlisted=unlisted, added=added, listed=listed, written=written,
    read=[e["summary"] for e in read["items"]], since=[e["id"] for e in since["items"]],
    acl=calendar.acl().list(calendarId=made["id"]).execute(),
    rosa_list=refused(lambda: rosa.events().list(calendarId=made["id"]).execute()),
    rosa_get=refused(lambda: rosa.calendars().get(calendarId=made["id"]).execute()),
    rosa_add=refused(lambda: rosa.calendarList().insert(body={{"id": made["id"]}}).execute()),
    rosa_write=refused(lambda: rosa.events().insert(calendarId=made["id"], body={{"summary": "x", **SLOT}}).execute()),
    reader=refused(lambda: calendar.acl().insert(calendarId=made["id"], body={{
        "role": "reader", "scope": {{"type": "user", "value": "rosa@example.com"}}}}).execute()),
)
"""
        )
        seen = await client.heard()
        await client.finished()
    made = seen["made"]
    assert isinstance(made, dict)
    assert made["kind"] == "calendar#calendar" and made["summary"] == "Supplier reviews"
    assert made["timeZone"] == "Europe/Berlin" and made["dataOwner"] == "mara@example.com"
    mine = seen["mine"]
    assert isinstance(mine, list) and [(e["accessRole"], "primary" in e) for e in mine] == [("owner", False)]
    assert seen["before"] == [404, "notFound"], "no grant, no calendar"
    rule = seen["rule"]
    assert isinstance(rule, dict)
    assert (rule["kind"], rule["role"], rule["scope"]) == (
        "calendar#aclRule",
        "writer",
        {"type": "user", "value": "dov@example.com"},
    )
    assert made["id"] not in seen["unlisted"], "sharing does not insert into the grantee's calendar list"  # type: ignore[operator]
    assert seen["listed"] == [["dov@example.com", "owner"], [made["id"], "writer"]]
    written = seen["written"]
    assert isinstance(written, dict)
    assert written["organizer"] == {"email": made["id"], "self": True}
    assert written["creator"] == {"email": "dov@example.com", "self": True}
    assert seen["read"] == ["Acme review"] and seen["since"] == [written["id"]]
    acl = seen["acl"]
    assert isinstance(acl, dict) and acl["kind"] == "calendar#acl" and acl["items"] == [rule]
    assert seen["rosa_list"] == [404, "notFound"] and seen["rosa_get"] == [404, "notFound"]
    assert seen["rosa_add"] == [404, "notFound"] and seen["rosa_write"] == [404, "notFound"]
    assert seen["reader"] == [501, "notImplemented"]


async def test_what_an_event_is_written_with_comes_back_as_written(tmp_path: Path) -> None:
    """The fields Calendar keeps as sent are answered as sent, through insert, get and a full update of what get
    answered (read-only fields in it ignored); a field not served yet is refused by name."""
    written = {
        "summary": "Planning call",
        "description": "<b>Agenda</b>",
        "location": "Room 4",
        "colorId": "5",
        "transparency": "transparent",
        "visibility": "private",
        "guestsCanModify": True,
        "guestsCanInviteOthers": False,
        "guestsCanSeeOtherGuests": False,
        "privateCopy": False,
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]},
        "extendedProperties": {"private": {"ticket": "OPS-12"}, "shared": {"room": "4"}},
        "source": {"url": "https://tracker.example/OPS-12", "title": "OPS-12"},
        "start": {"dateTime": "2026-09-15T10:00:00.250Z", "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": "2026-09-15T12:30:00+02:00"},
        "attendees": [{"email": "dov@example.com", "optional": True, "resource": False, "comment": "maybe"}],
    }
    async with serving(tmp_path, SCENARIO) as google:
        client = await google.client(
            CALENDAR
            + AS
            + f"""
made = calendar.events().insert(calendarId="primary", body={written!r}).execute()
got = calendar.events().get(calendarId="primary", eventId=made["id"]).execute()
again = calendar.events().update(calendarId="primary", eventId=made["id"], body=got).execute()
say(made=made, got=got, again=again, meet=refused(lambda: calendar.events().insert(calendarId="primary", body={{
    **SLOT, "conferenceData": {{"createRequest": {{"requestId": "r1"}}}}}}).execute()))
"""
        )
        seen = await client.heard()
        await client.finished()
    for key in ("made", "got", "again"):
        event = seen[key]
        assert isinstance(event, dict)
        for name, value in written.items():
            if name == "attendees":
                assert [
                    {k: a[k] for k in ("email", "optional", "resource", "comment")} for a in event["attendees"]
                ] == value, key
            else:
                assert event[name] == value, (key, name)
    assert seen["meet"] == [501, "notImplemented"]
