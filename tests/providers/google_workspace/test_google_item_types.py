"""Google Workspace tells its own items apart for the assessment of the agent's effects (`TypesItems`): an email and a
calendar event are both messages in its log, and an event's times and guests are read from the event itself."""

from __future__ import annotations

import base64
from email.message import EmailMessage

from minutehand.adapters.providers.google_workspace.manifest import MANIFEST
from minutehand.application.items import typed_items
from minutehand.domain.items import ItemKind
from minutehand.domain.world import Actor, Operation
from tests.providers.google_workspace.drive_world import AUTH, SCENARIO, Drive, answer, client_for


def _raw(to: str, subject: str, text: str) -> str:
    message = EmailMessage()
    message["To"], message["Subject"] = to, subject
    message.set_content(text)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


async def test_an_email_and_a_calendar_event_are_told_apart_and_the_events_times_are_read(drive: Drive) -> None:
    async with client_for(drive.provider, drive.store, drive.clock, "gmail.googleapis.com") as mail:
        sent = await mail.post(
            "/gmail/v1/users/me/messages/send",
            json={"raw": _raw("dov@example.com", "Quote", "Any news?")},
            headers=AUTH,
        )
        answer(sent)
    async with client_for(drive.provider, drive.store, drive.clock) as api:
        invited = await api.post(
            "/calendar/v3/calendars/primary/events",
            json={
                "summary": "Supplier call",
                "start": {"dateTime": "2026-09-15T10:00:00Z"},
                "end": {"dateTime": "2026-09-15T11:00:00Z"},
                "attendees": [{"email": "dov@example.com"}],
            },
            headers=AUTH,
        )
        answer(invited)
    every = typed_items(
        drive.store.events(), SCENARIO, {MANIFEST.key: MANIFEST}, {MANIFEST.key: drive.provider}, drive.store
    )
    typed = [t for t in every if t.actor is Actor.AGENT and t.operation is Operation.CREATE]
    kinds = [t.kind for t in typed]
    assert ItemKind.EMAIL in kinds and ItemKind.CALENDAR_EVENT in kinds
    [event] = [t for t in typed if t.kind is ItemKind.CALENDAR_EVENT]
    assert event.people == ["dov@example.com"]
    assert event.starts is not None and event.starts.isoformat() == "2026-09-15T10:00:00+00:00"
    [email] = [t for t in typed if t.kind is ItemKind.EMAIL]  # Dov's copy is logged with no snapshot: not an item
    assert email.starts is None and email.people == ["dov@example.com"]
