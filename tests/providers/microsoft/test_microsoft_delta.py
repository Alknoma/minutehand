"""Delta over a calendar view and over a chat's or a channel's messages, and a folder's delta over a message that was
moved away or deleted for good: a round pages to a `@odata.deltaLink`, and the next round lists what changed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from minutehand.domain.scenario import PersonPosts
from tests.providers.microsoft.outlook import AGENT, OUTLOOK, signed_in
from tests.providers.microsoft.tenant import CONNECTOR, GRAPH, Bot, Intercepted, Tenant, bearer, seeded, token


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", OUTLOOK)


@dataclass
class Session:
    http: httpx.AsyncClient
    me: dict[str, str]
    tenant: Tenant

    async def event(self, subject: str, day: str) -> str:
        made = await self.http.post(
            f"{GRAPH}/me/events",
            json={
                "subject": subject,
                "start": {"dateTime": f"2026-09-{day}T10:00:00", "timeZone": "UTC"},
                "end": {"dateTime": f"2026-09-{day}T11:00:00", "timeZone": "UTC"},
            },
            headers=self.me,
        )
        assert made.status_code == 201, made.text
        found = made.json()["id"]
        assert isinstance(found, str)
        return found

    async def delta(self, url: str, **headers: str) -> dict[str, Any]:
        answered = await self.http.get(url, headers={**self.me, **headers})
        assert answered.status_code == 200, answered.text
        found: dict[str, Any] = answered.json()
        return found


@pytest.fixture
async def session(tenant: Tenant, microsoft: Intercepted) -> AsyncIterator[Session]:
    async with microsoft.http() as http:
        yield Session(http=http, me=bearer(await signed_in(http, tenant, AGENT)), tenant=tenant)


WINDOW = "startDateTime=2026-09-14T00:00:00Z&endDateTime=2026-09-21T00:00:00Z"


async def test_a_calendar_view_delta_pages_to_a_delta_link_then_lists_what_changed(session: Session) -> None:
    http = session.http
    keep = await session.event("Keep", "15")
    drop = await session.event("Drop", "16")
    await session.event("Later", "29")
    first = await session.delta(f"{GRAPH}/me/calendarView/delta?{WINDOW}", Prefer="odata.maxpagesize=1")
    assert len(first["value"]) == 1 and "@odata.nextLink" in first and "@odata.deltaLink" not in first
    assert first["@odata.context"] == f"{GRAPH}/$metadata#Collection(event)"
    assert first["value"][0]["@odata.type"] == "#microsoft.graph.event"
    second = await session.delta(first["@odata.nextLink"])
    assert sorted(e["subject"] for e in first["value"] + second["value"]) == ["Drop", "Keep"]
    assert "@odata.nextLink" not in second and "$deltatoken=" in second["@odata.deltaLink"]
    nothing = await session.delta(second["@odata.deltaLink"])
    assert nothing["value"] == [] and "$deltatoken=" in nothing["@odata.deltaLink"]
    session.tenant.clock.jump(session.tenant.clock.now() + timedelta(minutes=1))
    patched = await http.patch(f"{GRAPH}/me/events/{keep}", json={"subject": "Kept"}, headers=session.me)
    assert patched.status_code == 200
    assert (await http.delete(f"{GRAPH}/me/events/{drop}", headers=session.me)).status_code == 204
    outside = await session.event("Elsewhere", "30")
    changed = (await session.delta(nothing["@odata.deltaLink"]))["value"]
    assert {(e["id"], e["subject"] if "subject" in e else None) for e in changed if "@removed" not in e} == {
        (keep, "Kept")
    }
    assert sorted(e["id"] for e in changed if "@removed" in e) == sorted([drop, outside])
    assert all(e["@removed"] == {"reason": "deleted"} for e in changed if "@removed" in e)


async def test_a_calendar_view_delta_without_its_window_or_with_an_unsupported_option_is_refused_by_name(
    session: Session,
) -> None:
    http = session.http
    bare = await http.get(f"{GRAPH}/me/calendarView/delta", headers=session.me)
    assert bare.status_code == 501 and "startDateTime" in bare.json()["error"]["message"]
    selected = await http.get(f"{GRAPH}/me/calendarView/delta?{WINDOW}&$select=subject", headers=session.me)
    assert selected.status_code == 501 and "$select" in selected.json()["error"]["message"]
    events = await http.get(f"{GRAPH}/me/events/delta", headers=session.me)
    assert events.status_code == 501, "event-delta documents calendarView only; delta on events is beta"
    garbled = await http.get(f"{GRAPH}/me/calendarView/delta?$deltatoken=nope", headers=session.me)
    assert garbled.status_code == 400


async def test_a_chat_messages_delta_lists_all_then_those_posted_or_changed_since(session: Session) -> None:
    http, tenant = session.http, session.tenant
    owen = tenant.world.person("owen")
    assert owen is not None
    chat = tenant.world.personal_with(owen.user.id, tenant.directory.tenant_id)
    assert chat is not None
    owen_auth = bearer(await signed_in(http, tenant, "owen@example.com"))
    bot_auth = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
    posted: list[str] = []
    for n in range(3):
        tenant.clock.jump(tenant.clock.now() + timedelta(minutes=1))
        answer = await http.post(
            f"{GRAPH}/chats/{chat.graph_id}/messages", json={"body": {"content": f"m{n}"}}, headers=owen_auth
        )
        posted.append(answer.json()["id"])
    url = f"{GRAPH}/chats/{chat.graph_id}/messages/delta"
    first = (await http.get(f"{url}?$top=2", headers=owen_auth)).json()
    assert len(first["value"]) == 2 and "$skiptoken=" in first["@odata.nextLink"]
    assert first["@odata.context"] == f"{GRAPH}/$metadata#Collection(microsoft.graph.chatMessage)"
    rest = (await http.get(first["@odata.nextLink"], headers=owen_auth)).json()
    assert sorted(m["id"] for m in first["value"] + rest["value"]) == sorted(posted)
    assert "$deltatoken=" in rest["@odata.deltaLink"] and "@odata.nextLink" not in rest
    assert (await http.get(rest["@odata.deltaLink"], headers=owen_auth)).json()["value"] == []
    tenant.clock.jump(tenant.clock.now() + timedelta(minutes=1))
    answer = await http.post(
        f"{GRAPH}/chats/{chat.graph_id}/messages", json={"body": {"content": "m3"}}, headers=owen_auth
    )
    spoken = await http.post(
        f"{CONNECTOR}v3/conversations/{chat.id}/activities", json={"type": "message", "text": "bot"}, headers=bot_auth
    )
    after = (await http.get(rest["@odata.deltaLink"], headers=owen_auth)).json()
    assert [m["id"] for m in after["value"]] == [answer.json()["id"], spoken.json()["id"]]
    tenant.clock.jump(tenant.clock.now() + timedelta(minutes=1))
    edited = await http.put(
        f"{CONNECTOR}v3/conversations/{chat.id}/activities/{spoken.json()['id']}",
        json={"type": "message", "text": "bot, edited"},
        headers=bot_auth,
    )
    assert edited.status_code == 200
    updated = (await http.get(after["@odata.deltaLink"], headers=owen_auth)).json()["value"]
    assert [(m["id"], m["body"]["content"]) for m in updated] == [(spoken.json()["id"], "bot, edited")]
    options = await http.get(f"{url}?$filter=lastModifiedDateTime gt 2026-01-01T00:00:00Z", headers=owen_auth)
    assert options.status_code == 501 and "$filter" in options.json()["error"]["message"]
    too_many = await http.get(f"{url}?$top=51", headers=owen_auth)
    assert too_many.status_code == 501


async def test_a_channel_messages_delta_leaves_the_replies_out(session: Session, bot: Bot) -> None:
    http, tenant = session.http, session.tenant
    owen_auth = bearer(await signed_in(http, tenant, "owen@example.com"))
    base = f"{GRAPH}/teams/{tenant.directory.team_id}/channels/{tenant.directory.general_channel_id}/messages"
    root = (await http.post(base, json={"body": {"content": "Standup?"}}, headers=owen_auth)).json()
    first = (await http.get(f"{base}/delta", headers=owen_auth)).json()
    await http.post(f"{base}/{root['id']}/replies", json={"body": {"content": "Yes"}}, headers=owen_auth)
    again = (await http.get(first["@odata.deltaLink"], headers=owen_auth)).json()
    assert [m["id"] for m in first["value"]] == [root["id"]] and again["value"] == []
    await tenant.provider.happen(
        PersonPosts(provider="microsoft", person="sofia", channel="general", text="New topic", mentions_agent=False),
        bot.target(),
        tenant.store,
        tenant.clock,
        secret="unused",
    )
    assert [
        m["body"]["content"] for m in (await http.get(again["@odata.deltaLink"], headers=owen_auth)).json()["value"]
    ] == ["New topic"]


async def test_a_folder_delta_reports_a_message_moved_away_and_one_deleted_for_good(session: Session) -> None:
    http = session.http
    link = f"{GRAPH}/me/mailFolders('inbox')/messages/delta"
    first = await session.delta(link)
    ids = {m["subject"]: m["id"] for m in first["value"]}
    moved = await http.post(
        f"{GRAPH}/me/messages/{ids['Lunch']}/move", json={"destinationId": "deleteditems"}, headers=session.me
    )
    assert moved.status_code == 201
    binned = await http.post(
        f"{GRAPH}/me/messages/{ids['Vendor review']}/move", json={"destinationId": "deleteditems"}, headers=session.me
    )
    assert (await http.delete(f"{GRAPH}/me/messages/{binned.json()['id']}", headers=session.me)).status_code == 204
    changed = (await session.delta(first["@odata.deltaLink"]))["value"]
    assert sorted(m["id"] for m in changed if "@removed" in m) == sorted([ids["Lunch"], ids["Vendor review"]])
    there = (await session.delta(f"{GRAPH}/me/mailFolders('deleteditems')/messages/delta"))["value"]
    assert [m["id"] for m in there] == [moved.json()["id"]]
