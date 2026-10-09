"""Microsoft tells its own items apart for the assessment of the agent's effects (`TypesItems`): a meeting request is
an email, the event it invites to is a calendar event whose times and attendees are read from the event itself, and
nothing else it keeps as a record is an item."""

from __future__ import annotations

from pathlib import Path

import pytest

from minutehand.adapters.providers.microsoft.manifest import MANIFEST
from minutehand.application.items import typed_items
from minutehand.domain.items import ItemKind
from minutehand.domain.world import Actor
from tests.providers.microsoft.outlook import AGENT, OUTLOOK, signed_in
from tests.providers.microsoft.tenant import GRAPH, Intercepted, Tenant, bearer, seeded

REVIEW = {
    "subject": "Vendor review",
    "body": {"contentType": "text", "content": "Shortlist and prices."},
    "start": {"dateTime": "2026-09-15T10:00:00", "timeZone": "Europe/Rome"},
    "end": {"dateTime": "2026-09-15T10:30:00", "timeZone": "Europe/Rome"},
    "attendees": [{"emailAddress": {"address": "sofia@example.com", "name": "Sofia"}, "type": "required"}],
}


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", OUTLOOK)


async def test_an_event_and_the_meeting_request_it_sends_are_a_calendar_event_and_an_email(
    tenant: Tenant, microsoft: Intercepted
) -> None:
    async with microsoft.http() as http:
        auth = bearer(await signed_in(http, tenant, AGENT))
        made = await http.post(f"{GRAPH}/me/events", json=REVIEW, headers=auth)
        assert made.status_code == 201, made.text

    every = typed_items(
        tenant.store.events(), OUTLOOK, {MANIFEST.key: MANIFEST}, {MANIFEST.key: tenant.provider}, tenant.store
    )
    mine = [t for t in every if t.actor is Actor.AGENT]
    [event] = [t for t in mine if t.kind is ItemKind.CALENDAR_EVENT]
    assert event.starts is not None and event.starts.isoformat() == "2026-09-15T08:00:00+00:00"
    assert event.ends is not None and event.ends.isoformat() == "2026-09-15T08:30:00+00:00"
    assert event.people == ["sofia@example.com"] and event.text.startswith("Vendor review")
    assert ItemKind.EMAIL in {t.kind for t in mine}, "the meeting request is an email"
    assert not {t.kind for t in every} - {
        ItemKind.CALENDAR_EVENT,
        ItemKind.EMAIL,
        ItemKind.CHAT_MESSAGE,
        ItemKind.DOCUMENT,
    }
