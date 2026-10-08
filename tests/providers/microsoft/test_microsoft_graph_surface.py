"""Graph `v1.0`'s own surface, held against this provider: every operation in the subset of Microsoft's published
OpenAPI description for the resources the provider claims (`tests/data/microsoft_graph_v1/`, with its source and
date) is either served or refused by name, and nothing is served that Microsoft does not publish.

Each operation is called through the proxy as a service calls it, its path filled with ids the world holds (a
user, a message in the folder named, an event they attend, the team, its General channel, a chat, a message in each,
a drive, a file, a permission on it, a subscription), a `/me` path with that user's own token. One in
`surface.SERVED` must answer anything but 501; any other must answer 501 Minutehand's `not_implemented`, naming its
method and path. Unserved calls go first (they change nothing), then the served ones, deletions last and deepest
first, so no call finds what it names already gone."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
import pytest

from minutehand.adapters.providers.microsoft.surface import SERVED
from tests.providers.microsoft.outlook import OUTLOOK, signed_in
from tests.providers.microsoft.tenant import (
    CONNECTOR,
    GRAPH,
    Intercepted,
    Tenant,
    Webhook,
    bearer,
    seeded,
    token,
)

SUBSET = (
    Path(__file__).resolve().parents[2] / "data" / "microsoft_graph_v1" / "openapi-subset-retrieved-2026-10-08.json"
)
OPERATIONS = sorted(
    (method, path) for path, methods in json.loads(SUBSET.read_text())["paths"].items() for method in methods
)


@pytest.fixture
def tenant(tmp_path: Path) -> Tenant:
    return seeded(tmp_path / "world.db", OUTLOOK)


@dataclass
class Surface:
    http: httpx.AsyncClient
    app: dict[str, str]
    me: dict[str, str]
    ids: dict[str, str]
    chat_message: str
    channel_message: str
    attended: str
    webhook: str

    def concrete(self, template: str) -> str:
        """The template with the world's ids in place of its parameters."""
        path = template
        if template.endswith(("/accept", "/tentativelyAccept", "/decline")):
            path = path.replace("{event-id}", quote(self.attended, safe=":@,!"))
        message = self.channel_message if "/channels/" in template else self.chat_message
        path = path.replace("{chatMessage-id}", message).replace("{chatMessage-id1}", message)
        for name, value in self.ids.items():
            path = path.replace("{" + name + "}", quote(value, safe=":@,!"))
        return re.sub(r"\{[^}]+\}", "unknown", path)


@pytest.fixture
async def surface(tenant: Tenant, microsoft: Intercepted, webhook: Webhook) -> AsyncIterator[Surface]:
    d = tenant.directory
    owen = tenant.world.person("owen")
    assert owen is not None
    async with microsoft.http() as http:
        app = bearer(await token(http, tenant, "https://graph.microsoft.com/.default"))
        bot = bearer(await token(http, tenant, "https://api.botframework.com/.default"))
        me = bearer(await signed_in(http, tenant, "owen@example.com"))
        chat = tenant.world.personal_with(owen.user.id, d.tenant_id)
        assert chat is not None
        chat_message = (
            await http.post(f"{CONNECTOR}v3/conversations/{chat.id}/activities", json={"text": "hi"}, headers=bot)
        ).json()["id"]
        channel_message = (
            await http.post(
                f"{CONNECTOR}v3/conversations/{d.general_channel_id}/activities", json={"text": "all"}, headers=bot
            )
        ).json()["id"]
        site = (await http.get(f"{GRAPH}/sites/{d.sharepoint_host}:/sites/OutlookCaseTeam", headers=app)).json()
        drive = (await http.get(f"{GRAPH}/sites/{site['id']}/drive", headers=app)).json()["id"]
        item = (
            await http.put(f"{GRAPH}/drives/{drive}/root:/brief.txt:/content", content=b"Brief", headers=app)
        ).json()["id"]
        permission = (
            await http.post(
                f"{GRAPH}/drives/{drive}/items/{item}/invite",
                json={"recipients": [{"email": "sofia@example.com"}], "roles": ["read"]},
                headers=app,
            )
        ).json()["value"][0]["id"]
        sent = (
            await http.get(
                f"{GRAPH}/me/mailFolders/sentitems/messages",
                headers=me,
                params={"$top": "1", "$orderby": "receivedDateTime desc"},
            )
        ).json()["value"][0]["id"]
        event = (
            await http.post(
                f"{GRAPH}/me/events",
                json={
                    "subject": "Review",
                    "start": {"dateTime": "2026-09-15T10:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-09-15T11:00:00", "timeZone": "UTC"},
                },
                headers=me,
            )
        ).json()["id"]
        listed = await http.get(f"{GRAPH}/me/events", params={"$orderby": "start/dateTime"}, headers=me)
        attended = next(e["id"] for e in listed.json()["value"] if not e["isOrganizer"])
        expires = (tenant.clock.now() + timedelta(minutes=30)).isoformat()
        subscription = (
            await http.post(
                f"{GRAPH}/subscriptions",
                json={
                    "changeType": "created",
                    "notificationUrl": webhook.url,
                    "resource": f"/drives/{drive}/root",
                    "expirationDateTime": expires,
                },
                headers=app,
            )
        ).json()["id"]
        yield Surface(
            http=http,
            app=app,
            me=me,
            ids={
                "user-id": owen.user.id,
                "presence-id": owen.user.id,
                "team-id": d.team_id,
                "channel-id": d.general_channel_id,
                "chat-id": chat.graph_id,
                "site-id": site["id"],
                "drive-id": drive,
                "driveItem-id": item,
                "driveItem-id1": item,
                "permission-id": permission,
                "message-id": sent,
                "mailFolder-id": "sentitems",
                "event-id": event,
                "subscription-id": subscription,
                "q": "Brief",
            },
            chat_message=chat_message,
            channel_message=channel_message,
            attended=attended,
            webhook=webhook.url,
        )


def _order(operation: tuple[str, str]) -> tuple[int, int, str]:
    method, path = operation
    served = operation in SERVED
    deleting = method == "DELETE"  # enum-lint: exempt HTTP's method name
    return (int(served) + int(served and deleting), -path.count("/") if deleting else 0, path)


BODIES: dict[str, object] = {
    "/sendMail": {
        "message": {"subject": "Hello", "toRecipients": [{"emailAddress": {"address": "sofia@example.com"}}]}
    },
    "/events": {
        "subject": "Sync",
        "start": {"dateTime": "2026-09-16T10:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-16T11:00:00", "timeZone": "UTC"},
    },
    "/getPresencesByUserId": {"ids": ["00000000-0000-0000-0000-000000000000"]},
    "/getSchedule": {
        "schedules": ["owen@example.com"],
        "startTime": {"dateTime": "2026-09-14T10:00:00", "timeZone": "UTC"},
        "endTime": {"dateTime": "2026-09-14T12:00:00", "timeZone": "UTC"},
    },
}
"""A served create sent with what its page names required, where an empty body is refused by name."""

ASKED_AS_DOCUMENTED = {
    "/chats": "a signed-in user's token: https://learn.microsoft.com/en-us/graph/api/chat-list",
    "/sites": "?search=: https://learn.microsoft.com/en-us/graph/api/site-search",
    "/drives/{drive-id}/items/{driveItem-id}/delta()": "the root: https://learn.microsoft.com/en-us/graph/api/driveitem-delta",
    "/drives/{drive-id}/items/{driveItem-id}/children": "a folder: https://learn.microsoft.com/en-us/graph/api/driveitem-list-children",
    "/me/calendarView": "a window: https://learn.microsoft.com/en-us/graph/api/user-list-calendarview",
}
"""Served operations called the way their page documents them, where calling them otherwise is refused by name."""


async def _call(surface: Surface, method: str, template: str) -> tuple[str, httpx.Response]:
    path = surface.concrete(template)
    if template.endswith(("/delta()", "/children")) and template.startswith("/drives/"):
        path = path.replace(f"/items/{surface.ids['driveItem-id']}/", "/items/root/")
    if template.endswith("calendarView"):
        path += "?startDateTime=2026-09-14T00:00:00Z&endDateTime=2026-09-21T00:00:00Z&$orderby=start/dateTime"
    elif method == "GET" and template.endswith("/events"):
        path += "?$orderby=start/dateTime"
    elif method == "GET" and template.endswith("/messages") and template.startswith(("/me", "/users")):
        path += "?$orderby=receivedDateTime desc"
    if template == "/sites" and method == "GET":
        path += "?search=*"
    headers = surface.me if template.startswith("/me") or template == "/chats" else surface.app
    content: bytes | None = None
    if method in ("POST", "PATCH", "PUT"):
        sent = next((body for end, body in BODIES.items() if method == "POST" and template.endswith(end)), {})
        if template.startswith("/subscriptions"):
            later = "2026-09-14T09:00:00Z"
            sent = (
                {"expirationDateTime": later}
                if method == "PATCH"
                else {
                    "changeType": "updated",
                    "notificationUrl": surface.webhook,
                    "resource": f"/drives/{surface.ids['drive-id']}/root",
                    "expirationDateTime": later,
                }
            )
        content = b"x" if template.endswith("/content") else json.dumps(sent).encode()
        headers = {**headers, "Content-Type": "text/plain" if content == b"x" else "application/json"}
    answered = await surface.http.request(method, f"{GRAPH}{path}", content=content, headers=headers)
    return path, answered


def test_what_is_served_is_on_graphs_published_surface() -> None:
    """Scope comes from the vendor: every served operation is one Microsoft publishes, in the subset kept."""
    assert set(SERVED) - set(OPERATIONS) == set()


async def test_every_published_operation_is_served_or_refused_by_name(surface: Surface) -> None:
    wrong: list[str] = []
    counts: Counter[str] = Counter()
    for method, template in sorted(OPERATIONS, key=_order):
        path, answered = await _call(surface, method, template)
        served = (method, template) in SERVED
        counts["served" if served else "refused"] += 1
        if served and answered.status_code == 501:
            wrong.append(f"served but refused: {method} {template}: {answered.text[:200]}")
        if served and method == "GET" and answered.status_code >= 400:
            wrong.append(f"served but not read: {method} {template}: {answered.status_code} {answered.text[:200]}")
        if not served:
            body = answered.json() if answered.headers.get("content-type", "").startswith("application/json") else {}
            error = body["error"] if isinstance(body, dict) and "error" in body else {}
            named = f"{method} /v1.0{unquote(path).split('?')[0]}" in str(error.get("message", ""))
            if answered.status_code != 501 or error.get("code") != "not_implemented" or not named:
                wrong.append(f"not refused by name: {method} {template}: {answered.status_code} {answered.text[:200]}")
    assert not wrong, "\n".join(wrong)
    assert counts["served"] == len(SERVED)
    assert counts["served"] + counts["refused"] == len(OPERATIONS)
