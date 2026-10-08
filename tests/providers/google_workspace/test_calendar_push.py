"""Calendar's push notifications, `events.watch` and `channels.stop`, driven by Google's own client (stock
`googleapiclient` over `httplib2`) in a process of its own through the proxy, told to a receiver of the test's own.

A guest's answer is landed beside the client by the provider's `land`, as the run loop does at its moment, after
asking `heard` whether the agent is told of it."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.google_workspace import wire
from minutehand.adapters.providers.google_workspace.state import DriveWorld
from minutehand.domain.people import PersonReply, Press
from minutehand.domain.world import Actor, EntityRef, MessageSnapshot, RecordSnapshot
from tests.providers.google_workspace.proxied import START, Client, Google, serving
from tests.providers.google_workspace.test_calendar_through_proxy import CALENDAR, SCENARIO

pytestmark = pytest.mark.timeout(120)

WEEK_MS = 7 * 24 * 3600 * 1000


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


@dataclass
class Heard:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Receiver:
    """The agent's webhook routes: `/ok` answers 200, `/broken` 500."""

    base: str
    heard: list[Heard] = field(default_factory=list)

    def at(self, path: str) -> list[Heard]:
        return [h for h in self.heard if h.path == path]

    def on(self, channel: str) -> list[Heard]:
        return [h for h in self.heard if h.headers["x-goog-channel-id"] == channel]

    async def until(self, count: int) -> None:
        """Wait until `count` notifications have arrived, however many channels they came on."""
        for _ in range(1000):
            if len(self.heard) >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"heard {len(self.heard)} notifications, waited for {count}")


@pytest.fixture
async def receiver() -> AsyncIterator[Receiver]:
    found: list[Heard] = []

    async def hear(request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items() if k.lower().startswith("x-goog-")}
        found.append(Heard(request.url.path, headers, await request.body()))
        return Response(status_code=500 if request.url.path == "/broken" else 200)

    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[Route("/ok", hear, methods=["POST"]), Route("/broken", hear, methods=["POST"])]),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
        )
    )
    serving_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield Receiver(base=f"http://127.0.0.1:{port}", heard=found)
    server.should_exit = True
    await serving_task


def notifications(google: Google) -> list[str]:
    """Each notification the world recorded, as it reads."""
    return [
        e.after.text
        for e in google.store.events()
        if isinstance(e.after, RecordSnapshot) and e.after.resource == "notification"
    ]


def channel_lines(google: Google) -> list[str]:
    return [
        e.after.text
        for e in google.store.events()
        if isinstance(e.after, RecordSnapshot) and e.after.resource == "channel"
    ]


def invitation(google: Google) -> EntityRef:
    [asked] = [e for e in google.store.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)]
    return asked.entity


def yes(google: Google, person: str = "dov") -> PersonReply:
    return PersonReply(
        person=person,
        in_reply_to=invitation(google),
        text="Yes",
        at=google.clock.now(),
        press=Press(action_id="accepted", label="Yes"),
    )


async def until_recorded(google: Google, count: int) -> None:
    for _ in range(1000):
        if len(notifications(google)) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"recorded {notifications(google)}, waited for {count}")


INVITE = """
def invite(summary, day="15"):
    return calendar.events().insert(calendarId="primary", body={
        "summary": summary, "attendees": [{"email": "dov@example.com"}],
        "start": {"dateTime": f"2026-09-{day}T10:00:00Z"}, "end": {"dateTime": f"2026-09-{day}T10:30:00Z"},
    }).execute()
"""


async def program(google: Google, text: str) -> Client:
    return await google.client(CALENDAR + INVITE + text)


async def test_a_watch_is_told_sync_then_exists_for_the_agents_own_change_and_a_guests_answer(
    tmp_path: Path, receiver: Receiver
) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await program(
            google,
            f"""
channel = calendar.events().watch(calendarId="primary", body={{
    "id": "agenda-1", "type": "web_hook", "address": {receiver.base + "/ok"!r}, "token": "sig=42"}}).execute()
say(channel=channel)
wait()
made = invite("Planning call")
first = calendar.events().list(calendarId="primary").execute()
say(made=made, sync=first["nextSyncToken"])
wait()
since = calendar.events().list(calendarId="primary", syncToken=first["nextSyncToken"]).execute()
say(since=[(e["id"], [a["responseStatus"] for a in e["attendees"]]) for e in since["items"]])
""",
        )
        opened = (await client.heard())["channel"]
        assert isinstance(opened, dict)
        assert opened["kind"] == "api#channel" and opened["id"] == "agenda-1" and opened["token"] == "sig=42"
        assert opened["expiration"] == str(ms(START) + WEEK_MS), "a week, Calendar's default ttl"
        assert opened["resourceUri"] == "https://www.googleapis.com/calendar/v3/calendars/primary/events?alt=json"
        await receiver.until(1)
        await client.go()
        made = (await client.heard())["made"]
        assert isinstance(made, dict)
        await receiver.until(2)

        google.clock.jump(START + timedelta(hours=2))
        reply = yes(google)
        assert google.provider.heard(reply, google.store, google.clock), "a live channel watches the calendar"
        assert not google.provider.watched(google.store, google.clock), "a document happening tells Drive channels only"
        await google.provider.land(reply, google.store, google.clock)
        await client.go()
        since = (await client.heard())["since"]
        await client.finished()

        assert since == [[made["id"], ["accepted"]]]
        heard = receiver.at("/ok")
        assert [h.headers["x-goog-resource-state"] for h in heard] == ["sync", "exists", "exists"]
        assert [h.headers["x-goog-message-number"] for h in heard] == ["1", "2", "3"]
        assert {h.body for h in heard} == {b""}, "a notification only says something changed"
        for h in heard:
            assert h.headers["x-goog-channel-id"] == "agenda-1"
            assert h.headers["x-goog-channel-token"] == "sig=42"
            assert h.headers["x-goog-resource-id"] == opened["resourceId"]
            assert h.headers["x-goog-resource-uri"] == opened["resourceUri"]
            assert h.headers["x-goog-channel-expiration"] == "Mon, 21 Sep 2026 08:30:00 GMT"
        told = [
            (e.sim_time, e.after.text)
            for e in google.store.events()
            if isinstance(e.after, RecordSnapshot) and e.after.resource == "notification"
        ]
        assert told[-1] == (
            START + timedelta(hours=2),
            f"exists 3 on channel agenda-1 to {receiver.base}/ok: answered 200",
        ), "the guest's answer is told at its moment"
        assert channel_lines(google)[0] == (
            f"channel agenda-1: calendar mara@example.com's events to {receiver.base}/ok "
            "until 2026-09-21T08:30:00.000Z, 1 sent"
        )


async def test_after_a_stop_nothing_more_is_told_and_each_api_stops_only_its_own_channels(
    tmp_path: Path, receiver: Receiver
) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await program(
            google,
            f"""
calendar_channel = calendar.events().watch(calendarId="primary", body={{
    "id": "agenda-1", "type": "web_hook", "address": {receiver.base + "/ok"!r}}}).execute()
start = drive.changes().getStartPageToken().execute()["startPageToken"]
drive_channel = drive.changes().watch(pageToken=start, body={{
    "id": "files-1", "type": "web_hook", "address": {receiver.base + "/ok"!r}}}).execute()
stop = lambda api, c: api.channels().stop(body={{"id": c["id"], "resourceId": c["resourceId"]}}).execute()
crossed = [refused(lambda: stop(drive, calendar_channel)), refused(lambda: stop(calendar, drive_channel))]
wrong = refused(lambda: calendar.channels().stop(body={{"id": "agenda-1", "resourceId": "elsewhere"}}).execute())
stopped = stop(calendar, calendar_channel)
again = refused(lambda: stop(calendar, calendar_channel))
unknown = refused(lambda: calendar.channels().stop(body={{"id": "never", "resourceId": "x"}}).execute())
other = calendar.events().watch(calendarId="mara@example.com", body={{
    "id": "agenda-2", "type": "web_hook", "address": {receiver.base + "/ok"!r}}}).execute()
made = invite("After the stop")
say(crossed=crossed, wrong=wrong, stopped=stopped, again=again, unknown=unknown, other=other)
""",
        )
        seen = await client.heard()
        await client.finished()
        assert seen["crossed"] == [[404, "notFound"], [404, "notFound"]]
        assert seen["wrong"] == [404, "notFound"]
        assert seen["stopped"] == ""
        assert seen["again"] == [404, "notFound"] and seen["unknown"] == [404, "notFound"]
        other = seen["other"]
        assert isinstance(other, dict)
        await receiver.until(4)  # two syncs, the Drive sync, and the second channel's exists
        await asyncio.sleep(0.2)
        assert [h.headers["x-goog-resource-state"] for h in receiver.on("agenda-1")] == ["sync"]
        assert [h.headers["x-goog-resource-state"] for h in receiver.on("agenda-2")] == ["sync", "exists"]
        assert receiver.on("agenda-2")[0].headers["x-goog-resource-id"] == other["resourceId"]
        assert "x-goog-channel-token" not in receiver.on("agenda-1")[0].headers, "no token was given"
        first = [line for line in channel_lines(google) if line.startswith("channel agenda-1: ")]
        assert first[-1].endswith(", stopped, 1 sent")
        assert [t for t in notifications(google) if " on channel agenda-1 " in t] == [
            f"sync 1 on channel agenda-1 to {receiver.base}/ok: answered 200"
        ]


async def test_a_channel_past_its_expiry_is_told_nothing_more(tmp_path: Path, receiver: Receiver) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await program(
            google,
            f"""
made = invite("Planning call")
short = calendar.events().watch(calendarId="primary", body={{
    "id": "agenda-1", "type": "web_hook", "address": {receiver.base + "/ok"!r}, "params": {{"ttl": "3600"}}}}).execute()
asked_long = calendar.events().watch(calendarId="primary", body={{
    "id": "agenda-2", "type": "web_hook", "address": {receiver.base + "/ok"!r},
    "expiration": "{ms(START + timedelta(days=30))}"}}).execute()
say(short=short, asked_long=asked_long)
wait()
invite("After the hour", day="16")
say(done=True)
""",
        )
        seen = await client.heard()
        short, asked_long = seen["short"], seen["asked_long"]
        assert isinstance(short, dict) and isinstance(asked_long, dict)
        assert short["expiration"] == str(ms(START + timedelta(hours=1))), "params.ttl, in seconds"
        assert asked_long["expiration"] == str(ms(START) + WEEK_MS), "cut to a week"
        await receiver.until(2)

        google.clock.jump(START + timedelta(hours=1, minutes=1))
        reply = yes(google)
        heard = google.provider.heard(reply, google.store, google.clock)
        await google.provider.land(reply, google.store, google.clock)
        await client.go()
        await client.heard()
        await client.finished()
        await receiver.until(4)
        await asyncio.sleep(0.2)

        assert heard, "the week-long channel still watches"
        assert [h.headers["x-goog-resource-state"] for h in receiver.on("agenda-1")] == ["sync"]
        assert [h.headers["x-goog-message-number"] for h in receiver.on("agenda-2")] == ["1", "2", "3"]


async def test_an_expired_channel_makes_a_guests_answer_unheard(tmp_path: Path, receiver: Receiver) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await program(
            google,
            f"""
made = invite("Planning call")
calendar.events().watch(calendarId="primary", body={{
    "id": "agenda-1", "type": "web_hook", "address": {receiver.base + "/ok"!r}, "params": {{"ttl": "3600"}}}}).execute()
say(done=True)
""",
        )
        await client.heard()
        await client.finished()
        await receiver.until(1)
        google.clock.jump(START + timedelta(hours=1))
        reply = yes(google)
        assert not google.provider.heard(reply, google.store, google.clock), "expired at the hour"
        await google.provider.land(reply, google.store, google.clock)
        assert len(receiver.heard) == 1


async def test_a_reused_channel_id_and_another_accounts_calendar_are_refused(
    tmp_path: Path, receiver: Receiver
) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await program(
            google,
            f"""
body = {{"id": "agenda-1", "type": "web_hook", "address": {receiver.base + "/ok"!r}}}
calendar.events().watch(calendarId="primary", body=body).execute()
start = drive.changes().getStartPageToken().execute()["startPageToken"]
say(
    twice=refused(lambda: calendar.events().watch(calendarId="primary", body=body).execute()),
    across=refused(lambda: drive.changes().watch(pageToken=start, body=body).execute()),
    unshared=refused(lambda: calendar.events().watch(calendarId="dov@example.com",
                                                     body={{**body, "id": "agenda-2"}}).execute()),
    nobody=refused(lambda: calendar.events().watch(calendarId="zed@example.com",
                                                   body={{**body, "id": "agenda-3"}}).execute()),
)
""",
        )
        seen = await client.heard()
        await client.finished()
        assert seen["twice"] == [400, "channelIdNotUnique"]
        assert seen["across"] == [400, "channelIdNotUnique"], "an id is unique within the project, whatever the API"
        assert seen["unshared"] == [404, "notFound"] and seen["nobody"] == [404, "notFound"]
        assert [c.id for c in DriveWorld(google.store).channels()] == ["agenda-1"]


async def test_an_unreachable_or_failing_address_is_recorded_and_the_run_goes_on(
    tmp_path: Path, receiver: Receiver
) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{taken.getsockname()[1]}/hook"
        client = await program(
            google,
            f"""
calendar.events().watch(calendarId="primary", body={{"id": "gone-1", "type": "web_hook", "address": {closed!r}}}).execute()
calendar.events().watch(calendarId="primary", body={{
    "id": "broken-1", "type": "web_hook", "address": {receiver.base + "/broken"!r}}}).execute()
made = invite("Planning call")
say(made=made["status"])
""",
        )
        assert (await client.heard())["made"] == "confirmed"
        await client.finished()
        await until_recorded(google, 4)
        recorded = sorted(
            (d.channel, d.number, d.state, d.status, d.delivered) for d in DriveWorld(google.store).deliveries()
        )
        assert recorded == [
            ("broken-1", 1, "sync", 500, False),
            ("broken-1", 2, "exists", 500, False),
            ("gone-1", 1, "sync", None, False),
            ("gone-1", 2, "exists", None, False),
        ]
        assert all("not reached (ConnectError" in t for t in notifications(google) if "gone-1" in t)

        google.clock.jump(START + timedelta(hours=2))
        await google.provider.land(yes(google), google.store, google.clock)
        assert len([d for d in DriveWorld(google.store).deliveries() if d.channel == "gone-1"]) == 3


async def test_message_numbers_rise_on_each_channel_and_the_token_goes_only_where_it_was_given(
    tmp_path: Path, receiver: Receiver
) -> None:
    async with serving(tmp_path, SCENARIO) as google:
        client = await program(
            google,
            f"""
calendar.events().watch(calendarId="primary", body={{
    "id": "agenda-1", "type": "web_hook", "address": {receiver.base + "/ok"!r}, "token": "first"}}).execute()
made = invite("Planning call")
calendar.events().watch(calendarId="primary", body={{
    "id": "agenda-2", "type": "web_hook", "address": {receiver.base + "/ok"!r}}}).execute()
calendar.events().patch(calendarId="primary", eventId=made["id"], body={{"summary": "Planning call (moved)"}}).execute()
calendar.events().delete(calendarId="primary", eventId=made["id"]).execute()
say(done=True)
""",
        )
        await client.heard()
        await client.finished()
        await receiver.until(7)
        first, second = receiver.on("agenda-1"), receiver.on("agenda-2")
        assert sorted(int(h.headers["x-goog-message-number"]) for h in first) == [1, 2, 3, 4]
        assert sorted(int(h.headers["x-goog-message-number"]) for h in second) == [1, 2, 3]
        assert {h.headers["x-goog-channel-token"] for h in first} == {"first"}
        assert not any("x-goog-channel-token" in h.headers for h in second)
        assert {h.headers["x-goog-resource-id"] for h in first} == {h.headers["x-goog-resource-id"] for h in second}
        kept = {c.id: c for c in DriveWorld(google.store).channels()}
        assert isinstance(kept["agenda-1"], wire.CalendarChannel) and kept["agenda-1"].messages == 4
