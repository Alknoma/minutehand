"""Google's push notification channels: one machinery for Drive's `changes.watch` and Calendar's `events.watch`, each
stopped by its own API's `channels.stop`. `CLAIMS.md` gives the source of each rule.

**Opening.** A watch's body names `id`, `type` (`web_hook`, or `webhook`), `address`, and optionally `token` and
`expiration` (milliseconds since the epoch). A channel id is taken once in a run, whichever API took it and
whether or not it was stopped since: Google takes an id once "within your project", and a run is one project.
The address must be HTTPS: an `http://` one is a 400 "WebHook callback must be HTTPS: <address>". The receiver's
certificate is never checked: Minutehand simulates the service and does no transport authentication, so an agent's
receiver may serve any certificate, self-signed included (docs/design.md, "Authentication is out of scope").

**Lifetime**, read on the run's clock. A Drive channel lives an hour unless it asks for longer, and a week at most,
as Drive documents. A Calendar channel lives `params.ttl` seconds, 604800 (a week) unless it says; a longer one is
cut to 30 days, as the service was seen to cut it. Where a watch asks both an expiration and a `ttl`, the more
restrictive holds. An expiration at or before the run's now, and a `ttl` that is not a whole number of seconds,
are refused 501 by name: Google's answer to either is not recorded. A stopped or expired channel is told nothing
more.

**Delivery** is a POST with no body whose `X-Goog-*` headers name the channel, the watched resource, its state
(`sync` first, then `change` for Drive, `exists` for Calendar) and the message's number on the channel: 1 for
`sync`, one more for each message after. `X-Goog-Channel-Token` is sent only when the watch set one. Each push is
recorded as a `Delivery` with how the address answered, or why it was not reached. Google retries a push answered
500, 502, 503 or 504 "with exponential backoff" and documents no schedule; this fake does not retry, and records
the failed push.

**Stopping** takes the channel's `id` and `resourceId`. Each API stops only its own channels: a Drive channel
named to Calendar's `channels.stop`, or the other way, is not found, as are an unknown channel, a `resourceId`
that is not the channel's, and a channel already stopped.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx

from minutehand.adapters.providers.google_workspace import wire
from minutehand.adapters.providers.google_workspace.state import DriveWorld
from minutehand.domain.scenario import Model
from minutehand.domain.world import Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

DELIVERY_TIMEOUT_SECONDS = 30


class Lifetime(Model):
    """How long a channel lives when it asks for nothing, the longest it may, and whether it reads `params.ttl`."""

    default: timedelta
    longest: timedelta
    reads_ttl: bool


DRIVE_LIFETIME = Lifetime(default=timedelta(hours=1), longest=timedelta(days=7), reads_ttl=False)
"""Drive's push guide: an hour unless asked, 604800 seconds at most for changes."""
CALENDAR_LIFETIME = Lifetime(default=timedelta(seconds=604800), longest=timedelta(days=30), reads_ttl=True)
"""Calendar's `params.ttl` defaults to 604800 seconds (the events.watch reference); a larger one comes back cut to
30 days (https://stackoverflow.com/q/64986662, https://stackoverflow.com/a/65001852)."""


def not_served(what: str) -> wire.Refusal:
    return wire.not_implemented(
        f"minutehand's Google push channels do not serve {what}: Google's answer is not recorded"
    )


def lives(channel: wire.DriveChannel | wire.CalendarChannel, now: datetime) -> bool:
    return not channel.stopped and wire.moment(channel.expiration) > now


def answer(channel: wire.DriveChannel | wire.CalendarChannel) -> wire.ChannelAnswer:
    """The Channel resource a watch answers."""
    return wire.ChannelAnswer(
        id=channel.id,
        resourceId=channel.resourceId,
        resourceUri=channel.resourceUri,
        expiration=str(int(wire.moment(channel.expiration).timestamp() * 1000)),
        token=channel.token,
    )


class Channels:
    """Every channel of the run, and the pushes still on their way to an address."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = DriveWorld(store)
        self._clock = clock
        self._background: set[asyncio.Task[None]] = set()

    def delivering(self) -> int:
        """Notifications pushed to a channel's address that have not had its answer yet."""
        return len(self._background)

    async def settled(self) -> None:
        """Wait for every push already started."""
        while self._background:
            await asyncio.gather(*list(self._background))

    # ------------------------------------------------------------------ opening and stopping

    def asked(self, body: bytes, lifetime: Lifetime) -> tuple[wire.ChannelWrite, datetime]:
        """The watch's body, checked, and when the channel it opens expires."""
        found = wire.read_object(body)
        if "payload" in found and found["payload"] is not None:
            raise not_served("payload")
        asked = wire.read_body(wire.ChannelWrite, found)
        if not asked.id:
            raise wire.required("channel.id", "Required: channel.id")
        if asked.type not in ("web_hook", "webhook"):
            raise wire.drive_refusal(
                400, "push.channelTypeNotSupported", f"Channel type '{asked.type}' is not supported.", domain="push"
            )
        if not asked.address.lower().startswith("https://"):
            # The status and message as the service answers them (https://stackoverflow.com/q/43484709,
            # https://github.com/janeczku/calibre-web/issues/502); no answer seen names a reason, so none is given.
            message = f"WebHook callback must be HTTPS: {asked.address}"
            raise wire.Refusal(wire.GoogleError(error=wire.ErrorBody(code=400, message=message)))
        if self._world.channel(asked.id) is not None:
            raise wire.drive_refusal(400, "channelIdNotUnique", f"Channel id {asked.id} not unique", domain="push")
        return asked, self._expires(asked, lifetime)

    def _expires(self, asked: wire.ChannelWrite, lifetime: Lifetime) -> datetime:
        now = self._clock.now()
        wanted: list[datetime] = []
        if asked.expiration is not None:
            if not str(asked.expiration).isdigit():
                raise wire.invalid("channel.expiration")
            at = datetime.fromtimestamp(int(asked.expiration) / 1000, tz=now.tzinfo)
            if at <= now:
                raise not_served(f"an expiration at or before the run's now ({wire.rfc3339(now)})")
            wanted.append(at)
        if lifetime.reads_ttl and asked.params is not None and "ttl" in asked.params:
            ttl = str(asked.params["ttl"])
            if not ttl.isdigit() or int(ttl) < 1:
                raise not_served(f"params.ttl {ttl!r}, not a whole number of seconds")
            wanted.append(now + timedelta(seconds=int(ttl)))
        return min(min(wanted) if wanted else now + lifetime.default, now + lifetime.longest)

    def open(self, channel: wire.DriveChannel | wire.CalendarChannel) -> wire.ChannelAnswer:
        """Keep the channel and send its `sync` message, after the watch is answered."""
        self._world.keep_channel(channel, operation=Operation.CREATE)
        self._later(channel, "sync", 1)
        return answer(channel)

    def stop(self, asked: wire.ChannelStop, kind: type[wire.DriveChannel] | type[wire.CalendarChannel]) -> None:
        channel = self._world.channel(asked.id)
        if (
            channel is None
            or not isinstance(channel, kind)
            or channel.resourceId != asked.resourceId
            or channel.stopped
        ):
            raise wire.drive_refusal(
                404, "notFound", f"Channel '{asked.id}' not found for project 'minutehand'", domain="global"
            )
        self._world.keep_channel(channel.model_copy(update={"stopped": True}))

    # ------------------------------------------------------------------ telling

    def drive(self) -> list[wire.DriveChannel]:
        """Every live Drive channel."""
        now = self._clock.now()
        return [c for c in self._world.channels() if isinstance(c, wire.DriveChannel) and lives(c, now)]

    def calendars(self, calendars: set[str]) -> list[wire.CalendarChannel]:
        """Every live Calendar channel on one of `calendars` (addresses, any case)."""
        now = self._clock.now()
        wanted = {c.lower() for c in calendars}
        return [
            c
            for c in self._world.channels()
            if isinstance(c, wire.CalendarChannel) and c.calendar.lower() in wanted and lives(c, now)
        ]

    async def tell_calendars(self, calendars: set[str]) -> None:
        """Tell every live channel on `calendars` that their events changed, now: a person's change, at its moment."""
        for channel in self.calendars(calendars):
            await self.tell(channel, "exists")

    def tell_calendars_later(self, calendars: set[str]) -> None:
        """Tell every live channel on `calendars` that their events changed, once the call that changed them is
        answered: the agent's own change."""
        for channel in self.calendars(calendars):
            self._later(self._next(channel), "exists", channel.messages + 1)

    async def tell(
        self, channel: wire.DriveChannel | wire.CalendarChannel, state: str, *, update: dict[str, object] | None = None
    ) -> None:
        """Send the channel its next message and record it; `update` is kept on the channel with its number."""
        kept = self._next(channel, update)
        await self._push(kept, state, kept.messages)

    def _next(
        self, channel: wire.DriveChannel | wire.CalendarChannel, update: dict[str, object] | None = None
    ) -> wire.DriveChannel | wire.CalendarChannel:
        kept = channel.model_copy(update={**(update or {}), "messages": channel.messages + 1})
        self._world.keep_channel(kept)
        return kept

    def _later(self, channel: wire.DriveChannel | wire.CalendarChannel, state: str, number: int) -> None:
        task = asyncio.create_task(self._push(channel, state, number))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _push(self, channel: wire.DriveChannel | wire.CalendarChannel, state: str, number: int) -> None:
        headers = {
            "X-Goog-Channel-ID": channel.id,
            "X-Goog-Channel-Expiration": wire.rfc1123(wire.moment(channel.expiration)),
            "X-Goog-Resource-State": state,
            "X-Goog-Message-Number": str(number),
            "X-Goog-Resource-ID": channel.resourceId,
            "X-Goog-Resource-URI": channel.resourceUri,
            "Content-Length": "0",
        }
        if channel.token is not None:
            headers["X-Goog-Channel-Token"] = channel.token
        delivery = wire.Delivery(channel=channel.id, number=number, state=state, address=channel.address)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=DELIVERY_TIMEOUT_SECONDS, verify=False) as client:
                answered = await client.post(channel.address, headers=headers)
        except httpx.HTTPError as error:
            delivery = delivery.model_copy(update={"failure": f"{type(error).__name__}: {error}".rstrip(": ")})
        else:
            delivery = delivery.model_copy(update={"status": answered.status_code})
        self._world.delivered(delivery)
