"""Calendar's events list over a seeded world, through the ASGI app: what it says of the calendar itself."""

from __future__ import annotations

from minutehand.adapters.providers.google_workspace import wire
from tests.providers.google_workspace.drive_world import AUTH, LATER, START, Drive, answer, client_for

EVENTS = "/calendar/v3/calendars/primary/events"


async def test_an_events_list_answers_the_calendars_last_change_and_the_same_until_it_changes(drive: Drive) -> None:
    """`updated` is "the last modification time of the calendar", and the list's etag and sync token follow it: the
    clock moving and other calls made leave a list read again as it was; an event added moves all three."""
    async with client_for(drive.provider, drive.store, drive.clock) as api:
        first = answer(await api.get(EVENTS, headers=AUTH))
        drive.clock.jump(LATER)
        answer(await api.get("/drive/v3/about", params={"fields": "user"}, headers=AUTH))
        again = answer(await api.get(EVENTS, headers=AUTH))
        event = {
            "summary": "Supplier call",
            "start": {"dateTime": "2026-09-15T10:00:00Z"},
            "end": {"dateTime": "2026-09-15T11:00:00Z"},
        }
        answer(await api.post(EVENTS, json=event, headers=AUTH))
        changed = answer(await api.get(EVENTS, headers=AUTH))
    assert first["updated"] == wire.rfc3339(START), first
    assert again == first, "the list read again with the calendar unchanged answers otherwise"
    assert changed["updated"] == wire.rfc3339(LATER), changed
    assert changed["etag"] != first["etag"] and changed["nextSyncToken"] != first["nextSyncToken"], changed
