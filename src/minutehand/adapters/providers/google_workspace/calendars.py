"""Google Calendar v3 over the run's store and clock: each account's primary calendar, events with guests, free/busy.

**Calendars.** Every Google account in the world has one, its primary, whose id is its address; its time zone is
the person's working hours' zone (UTC without). A call names it as `primary` or by its address; another account's
calendar is a 404, as a calendar nobody shared is. Free/busy reads anyone's.

**An event is one entity** (`EntityKind.MESSAGE`) in its organizer's calendar, and is seen on each guest's calendar
under the same id, as in Calendar. Only the organizer changes or deletes it (403 `forbiddenForNonOrganizer`).

**An invitation is the agent asking each guest.** The event's `MessageSnapshot` is addressed to every attendee but
the organizer and offers what Calendar's invitation offers, Yes, Maybe and No, each a control whose id is the
`responseStatus` it sets. A person's scripted press lands at its moment (`land`) as their response; text they write
back lands as their response comment. An event with no guests asks nobody: its snapshot is a `RecordSnapshot`.
`sendUpdates` is checked and otherwise changes nothing: a guest in the world sees the event on their calendar
whatever it says.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.google_workspace import calendar_wire as cal
from minutehand.adapters.providers.google_workspace import gmail_wire as mail
from minutehand.adapters.providers.google_workspace import wire
from minutehand.adapters.providers.google_workspace.access import Caller, bearer, due_fault, signed_in
from minutehand.adapters.providers.google_workspace.gmail import EVERY_METHOD
from minutehand.adapters.providers.google_workspace.manifest import MANIFEST
from minutehand.adapters.providers.google_workspace.state import DriveWorld
from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import Model
from minutehand.domain.world import (
    Actor,
    Change,
    EntityKind,
    EntityRef,
    InteractionKind,
    InteractionSnapshot,
    MessageAction,
    MessageSnapshot,
    Operation,
    RecordSnapshot,
    Stored,
)
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

JSON = "application/json; charset=UTF-8"
CALENDARS = "calendars"
_SCAN = 1000

OPERATIONS = frozenset(
    {
        "calendarList.list",
        "events.list",
        "events.get",
        "events.insert",
        "events.patch",
        "events.update",
        "events.delete",
        "freebusy.query",
    }
)
"""Every Calendar call a fault may name, by Google's own method name."""


class CalendarRecord(Model):
    """An account's primary calendar, seeded for each person."""

    id: str
    timeZone: str


def calendar_parent(address: str) -> str:
    return f"calendar:{address.lower()}"


def event_ref(event: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.MESSAGE, external_id=event)


def record_ref(external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=external_id)


def event_id(seq: int) -> str:
    """An id Calendar would mint: base32hex, from the event that made it."""
    digest = hashlib.sha256(f"event\x1f{seq}".encode()).digest()[:15]
    return base64.b32hexencode(digest).decode().lower().rstrip("=")


def html_link(event: str, calendar: str) -> str:
    eid = base64.urlsafe_b64encode(f"{event} {calendar}".encode()).decode().rstrip("=")
    return f"https://www.google.com/calendar/event?eid={eid}"


def invitation(event: cal.StoredEvent) -> MessageSnapshot | RecordSnapshot:
    """The event as its guests are asked by it: what, when, where, who else, and Yes, Maybe or No."""
    guests = [a.email for a in event.attendees or [] if a.email.lower() != event.organizer.email.lower()]
    when = f"{event.start.dateTime or event.start.date} to {event.end.dateTime or event.end.date}"
    lines = [event.summary or "(No title)", when]
    if event.location:
        lines.append(event.location)
    if guests:
        lines.append("Guests: " + ", ".join(guests))
    if event.description:
        lines += ["", event.description]
    text = "\n".join(lines)
    if not guests:
        return RecordSnapshot(resource="event", text=text)
    return MessageSnapshot(
        text=text,
        channel=event.id,
        recipient_emails=guests,
        actions=[MessageAction(action_id=status, label=label) for status, label in cal.ANSWERS.items()],
    )


class CalendarWorld:
    """Typed reads and writes of the run's calendars and events."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._drive = DriveWorld(store)

    @property
    def store(self) -> Store:
        return self._store

    def calendar(self, address: str) -> cal.CalendarListEntry | None:
        """An account's primary calendar; None when nobody in the world has that address."""
        user = self._drive.user(address)
        if user is None or user.emailAddress is None:
            return None
        held = self._store.get(record_ref(calendar_parent(user.emailAddress)))
        zone = CalendarRecord.model_validate_json(held.body).timeZone if held is not None else "UTC"
        return cal.CalendarListEntry(
            etag=f'"{held.seq if held is not None else 0}"',
            id=user.emailAddress,
            summary=user.emailAddress,
            timeZone=zone,
        )

    def keep_calendar(self, address: str, time_zone: str) -> None:
        self._store.apply(
            Change(
                entity=record_ref(calendar_parent(address)),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                body=wire.dump(CalendarRecord(id=address, timeZone=time_zone)),
                parent=CALENDARS,
            )
        )

    def event(self, event: str) -> tuple[cal.StoredEvent, Stored] | None:
        stored = self._store.get(event_ref(event))
        if stored is None:
            return None
        kept = cal.KEPT.validate_json(stored.body)
        return (kept, stored) if isinstance(kept, cal.StoredEvent) else None

    def was(self, event: str) -> bool:
        """Whether an event by this id was ever written, deleted or not."""
        return any(
            isinstance(cal.KEPT.validate_json(v.body), cal.StoredEvent) for v in self._store.versions(event_ref(event))
        )

    def taken(self, event: str) -> bool:
        """Whether anything of this provider's was ever written under this id."""
        return bool(self._store.versions(event_ref(event)))

    def organized(self, address: str) -> list[tuple[cal.StoredEvent, Stored]]:
        found: list[tuple[cal.StoredEvent, Stored]] = []
        after: str | None = None
        while True:
            page = self._store.children(
                MANIFEST.key, EntityKind.MESSAGE, calendar_parent(address), after=after, limit=_SCAN
            )
            for stored in page:
                kept = cal.KEPT.validate_json(stored.body)
                if isinstance(kept, cal.StoredEvent):
                    found.append((kept, stored))
            if len(page) < _SCAN:
                return found
            after = page[-1].entity.external_id

    def on(self, address: str) -> list[tuple[cal.StoredEvent, Stored]]:
        """Every event on an account's calendar: those it organizes and those it is a guest of."""
        wanted = address.lower()
        found: list[tuple[cal.StoredEvent, Stored]] = []
        for user in self._drive.users():
            if user.emailAddress is None:
                continue
            for event, stored in self.organized(user.emailAddress):
                if user.emailAddress.lower() == wanted or any(a.email.lower() == wanted for a in event.attendees or []):
                    found.append((event, stored))
        return found

    def write(self, event: cal.StoredEvent, *, operation: Operation, actor: Actor, snapshot: bool) -> int:
        return self._store.apply(
            Change(
                entity=event_ref(event.id),
                operation=operation,
                actor=actor,
                body=wire.dump(event),
                parent=calendar_parent(event.organizer.email),
                after=invitation(event) if snapshot else None,
            )
        ).seq

    def delete(self, event: cal.StoredEvent, *, actor: Actor) -> None:
        self._store.apply(
            Change(
                entity=event_ref(event.id),
                operation=Operation.DELETE,
                actor=actor,
                parent=calendar_parent(event.organizer.email),
            )
        )

    def land(self, reply: PersonReply, clock: Clock) -> None:
        """A guest answers an invitation: a press sets their `responseStatus`, written text their response comment.
        An event deleted since, or a guest no longer on it, is left alone."""
        found = self.event(reply.in_reply_to.external_id)
        person = self._drive.person(reply.person)
        if found is None or person is None or person.emailAddress is None:
            return
        event, _ = found
        address = person.emailAddress.lower()
        attendees = list(event.attendees or [])
        place = next((n for n, a in enumerate(attendees) if a.email.lower() == address), None)
        if place is None:
            return
        if reply.press is not None and reply.press.action_id not in cal.ANSWERS:
            raise ValueError(
                f"{reply.person} presses {reply.press.label!r} on an invitation, which offers "
                + ", ".join(cal.ANSWERS.values())
            )
        attendee = attendees[place]
        if reply.press is not None:
            attendees[place] = attendee.model_copy(update={"responseStatus": reply.press.action_id})
        else:
            attendees[place] = attendee.model_copy(update={"comment": reply.text})
        seq = self._store.head() + 1
        answered = event.model_copy(
            update={"attendees": attendees, "updated": wire.rfc3339(clock.now()), "etag": f'"{seq}"'}
        )
        self.write(answered, operation=Operation.UPDATE, actor=Actor.PERSON, snapshot=False)
        response = record_ref(f"{event.id}.response.{address}")
        after: InteractionSnapshot | MessageSnapshot = (
            InteractionSnapshot(
                interaction=InteractionKind.PRESS,
                person=reply.person,
                on=reply.in_reply_to,
                action_id=reply.press.action_id,
                label=reply.press.label,
            )
            if reply.press is not None
            else MessageSnapshot(
                text=reply.text,
                channel=event.id,
                recipient_emails=[event.organizer.email],
                thread_of=event.id,
                answerable=False,
            )
        )
        self._store.apply(
            Change(
                entity=response,
                operation=Operation.UPDATE if self._store.get(response) is not None else Operation.CREATE,
                actor=Actor.PERSON,
                body=wire.dump(attendees[place]),
                parent=event.id,
                after=after,
            )
        )

    def saw(self, ref: EntityRef, operation: Operation) -> None:
        self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))


# ---------------------------------------------------------------------- the API

Handler = Callable[[Request, Caller], Awaitable[Response]]


def _json(answer: Model, request: Request, model: type[Model], status: int = 200) -> Response:
    fields = request.query_params["fields"] if "fields" in request.query_params else None
    return Response(wire.respond(answer, wire.selection(fields, model, "*")), status_code=status, media_type=JSON)


def _refused(refusal: wire.Refusal) -> Response:
    return Response(wire.error_body(refusal), status_code=refusal.code, media_type=JSON, headers=refusal.headers)


def _param(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def _not_found() -> wire.Refusal:
    return cal.refused(404, "notFound", "Not Found")


class CalendarApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._calendars = CalendarWorld(store)
        self._drive = DriveWorld(store)
        self._clock = clock

    @property
    def calendars(self) -> CalendarWorld:
        return self._calendars

    def guarded(self, handler: Handler, operation: str) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            try:
                caller = signed_in(
                    self._drive,
                    self._clock,
                    bearer(request, _param(request, "access_token")),
                    missing=wire.login_required(),
                )
                kind = due_fault(self._drive, self._clock, operation)
                if kind is not None:
                    raise mail.fault(kind)
                return await handler(request, caller)
            except wire.Refusal as refusal:
                return _refused(refusal)

        return endpoint

    def _own(self, request: Request, caller: Caller) -> cal.CalendarListEntry:
        """The calendar the path names, which must be the caller's own."""
        spelled = request.path_params["calendar_id"]
        if spelled != "primary" and spelled.lower() != caller.email.lower():
            raise _not_found()
        found = self._calendars.calendar(caller.email)
        if found is None:
            raise _not_found()
        return found

    def _served(self, event: cal.StoredEvent, calendar: str, caller: Caller) -> cal.StoredEvent:
        """The event as it reads from `calendar`: `self` set where it names that calendar's account."""
        mine = calendar.lower()
        return event.model_copy(
            update={
                "organizer": event.organizer.model_copy(
                    update={"self": True if event.organizer.email.lower() == mine else None}
                ),
                "creator": event.creator.model_copy(
                    update={"self": True if event.creator.email.lower() == caller.email.lower() else None}
                ),
                "attendees": [
                    a.model_copy(update={"self": True if a.email.lower() == mine else None})
                    for a in event.attendees or []
                ]
                or None,
            }
        )

    def _visible(self, event_id: str, calendar: str) -> tuple[cal.StoredEvent, Stored]:
        found = self._calendars.event(event_id)
        if found is None:
            if self._calendars.was(event_id):
                raise cal.refused(410, "deleted", "Resource has been deleted")
            raise _not_found()
        event, stored = found
        mine = calendar.lower()
        if event.organizer.email.lower() != mine and not any(a.email.lower() == mine for a in event.attendees or []):
            raise _not_found()
        return event, stored

    def _send_updates(self, request: Request) -> None:
        spelled = _param(request, "sendUpdates")
        if spelled is not None and spelled not in cal.SEND_UPDATES:
            raise cal.refused(400, "invalidParameter", f"Invalid value for: sendUpdates: {spelled}")

    def _attendees(
        self, written: list[cal.AttendeeWrite], organizer: str, held: list[cal.Attendee]
    ) -> list[cal.Attendee]:
        """The guest list as written, each guest keeping the response they gave unless the write sets one."""
        before = {a.email.lower(): a for a in held}
        found: list[cal.Attendee] = []
        for guest in written:
            if not guest.email:
                raise cal.refused(400, "required", "Missing attendee email.")
            if guest.responseStatus is not None and guest.responseStatus not in cal.RESPONSES:
                raise cal.refused(400, "invalid", f"Invalid attendee response status: {guest.responseStatus}")
            if any(a.email.lower() == guest.email.lower() for a in found):
                continue
            kept = before[guest.email.lower()] if guest.email.lower() in before else None
            is_organizer = guest.email.lower() == organizer.lower()
            status = guest.responseStatus or (
                kept.responseStatus if kept is not None else ("accepted" if is_organizer else "needsAction")
            )
            found.append(
                cal.Attendee(
                    email=guest.email,
                    displayName=guest.displayName or (kept.displayName if kept is not None else None),
                    organizer=True if is_organizer else None,
                    optional=guest.optional,
                    responseStatus=status,
                    comment=guest.comment if guest.comment is not None else (kept.comment if kept else None),
                    additionalGuests=guest.additionalGuests,
                )
            )
        return found

    def _checked_times(
        self, start: cal.EventTime, end: cal.EventTime, zone: str
    ) -> tuple[cal.EventTime, cal.EventTime]:
        begins, ends = cal.normal(start, zone, "start"), cal.normal(end, zone, "end")
        if (begins.date is None) != (ends.date is None):
            raise cal.refused(400, "invalid", "Start and end times must either both be date or both be dateTime.")
        if cal.instant(ends, zone, "end") <= cal.instant(begins, zone, "start"):
            raise cal.refused(400, "timeRangeEmpty", "The specified time range is empty.")
        return begins, ends

    # ------------------------------------------------------------------ routes

    async def calendar_list(self, request: Request, caller: Caller) -> Response:
        found = self._calendars.calendar(caller.email)
        items = [found] if found is not None else []
        self._calendars.saw(record_ref(calendar_parent(caller.email)), Operation.READ)
        answer = cal.CalendarList(etag=f'"{self._calendars.store.head()}"', items=items)
        return _json(answer, request, cal.CalendarList)

    async def events_insert(self, request: Request, caller: Caller) -> Response:
        calendar = self._own(request, caller)
        self._send_updates(request)
        asked = wire.read_body(cal.EventWrite, wire.read_object(await request.body()))
        if asked.recurrence:
            raise mail.not_implemented("recurring events (recurrence)")
        if asked.start is None:
            raise cal.refused(400, "required", "Missing start time.")
        if asked.end is None:
            raise cal.refused(400, "required", "Missing end time.")
        start, end = self._checked_times(asked.start, asked.end, calendar.timeZone)
        seq = self._calendars.store.head() + 1
        if asked.id is not None:
            if not 5 <= len(asked.id) <= 1024 or any(c not in cal.EVENT_ID_CHARACTERS for c in asked.id):
                raise cal.refused(400, "invalid", "Invalid resource id value.")
            if self._calendars.taken(asked.id):
                raise cal.refused(409, "duplicate", "The requested identifier already exists.")
        new_id = asked.id or event_id(seq)
        now = wire.rfc3339(self._clock.now())
        me = cal.Actor(email=calendar.id, displayName=caller.user.displayName)
        event = cal.StoredEvent(
            etag=f'"{seq}"',
            id=new_id,
            status=asked.status or "confirmed",
            htmlLink=html_link(new_id, calendar.id),
            created=now,
            updated=now,
            summary=asked.summary,
            description=asked.description,
            location=asked.location,
            colorId=asked.colorId,
            creator=cal.Actor(email=caller.email, displayName=caller.user.displayName),
            organizer=me,
            start=start,
            end=end,
            transparency=asked.transparency,
            visibility=asked.visibility,
            iCalUID=f"{new_id}@google.com",
            attendees=self._attendees(asked.attendees or [], calendar.id, []) or None,
            guestsCanModify=asked.guestsCanModify,
        )
        self._calendars.write(event, operation=Operation.CREATE, actor=Actor.AGENT, snapshot=True)
        return _json(self._served(event, calendar.id, caller), request, cal.StoredEvent)

    async def events_get(self, request: Request, caller: Caller) -> Response:
        calendar = self._own(request, caller)
        event, _ = self._visible(request.path_params["event_id"], calendar.id)
        self._calendars.saw(event_ref(event.id), Operation.READ)
        return _json(self._served(event, calendar.id, caller), request, cal.StoredEvent)

    async def events_patch(self, request: Request, caller: Caller) -> Response:
        return await self._changed(request, caller, replace=False)

    async def events_update(self, request: Request, caller: Caller) -> Response:
        return await self._changed(request, caller, replace=True)

    async def _changed(self, request: Request, caller: Caller, *, replace: bool) -> Response:
        """`events.patch` sets what the body names; `events.update` replaces the event with the body."""
        calendar = self._own(request, caller)
        self._send_updates(request)
        event, _ = self._visible(request.path_params["event_id"], calendar.id)
        if event.organizer.email.lower() != calendar.id.lower():
            raise cal.refused(
                403, "forbiddenForNonOrganizer", "Shared properties can only be changed by the organizer of the event."
            )
        found = wire.read_object(await request.body())
        asked = wire.read_body(cal.EventWrite, found)
        if asked.recurrence:
            raise mail.not_implemented("recurring events (recurrence)")
        if asked.status is not None and asked.status not in ("confirmed", "tentative"):
            raise mail.not_implemented(f"setting an event's status to {asked.status}; delete it instead")
        named = set(found) if not replace else set(cal.EventWrite.model_fields)
        if replace and (asked.start is None or asked.end is None):
            raise cal.refused(400, "required", "Missing start time." if asked.start is None else "Missing end time.")
        start = asked.start if "start" in named and asked.start is not None else event.start
        end = asked.end if "end" in named and asked.end is not None else event.end
        start, end = self._checked_times(start, end, calendar.timeZone)
        moved = (start, end) != (event.start, event.end)
        seq = self._calendars.store.head() + 1
        update: dict[str, object] = {
            "start": start,
            "end": end,
            "updated": wire.rfc3339(self._clock.now()),
            "etag": f'"{seq}"',
            "sequence": event.sequence + 1 if moved else event.sequence,
        }
        for name in ("summary", "description", "location", "colorId", "transparency", "visibility", "guestsCanModify"):
            if name in named:
                update[name] = getattr(asked, name)
        if "status" in named and asked.status is not None:
            update["status"] = asked.status
        if "attendees" in named:
            update["attendees"] = (
                self._attendees(asked.attendees or [], event.organizer.email, list(event.attendees or [])) or None
            )
        changed = event.model_copy(update=update)
        self._calendars.write(changed, operation=Operation.UPDATE, actor=Actor.AGENT, snapshot=True)
        return _json(self._served(changed, calendar.id, caller), request, cal.StoredEvent)

    async def events_delete(self, request: Request, caller: Caller) -> Response:
        calendar = self._own(request, caller)
        self._send_updates(request)
        event, _ = self._visible(request.path_params["event_id"], calendar.id)
        if event.organizer.email.lower() != calendar.id.lower():
            raise cal.refused(
                403, "forbiddenForNonOrganizer", "Shared properties can only be changed by the organizer of the event."
            )
        self._calendars.delete(event, actor=Actor.AGENT)
        return Response(status_code=204)

    async def events_list(self, request: Request, caller: Caller) -> Response:
        calendar = self._own(request, caller)
        sync = _param(request, "syncToken")
        if sync is not None:
            return self._incremental(request, caller, calendar, sync)
        low = cal.moment(m, "timeMin") if (m := _param(request, "timeMin")) is not None else None
        high = cal.moment(m, "timeMax") if (m := _param(request, "timeMax")) is not None else None
        updated_min = cal.moment(m, "updatedMin") if (m := _param(request, "updatedMin")) is not None else None
        order = _param(request, "orderBy")
        single = _param(request, "singleEvents") == "true"
        if order not in (None, "startTime", "updated"):  # enum-lint: exempt Calendar's orderBy values
            raise cal.refused(400, "invalid", f"Invalid value for: orderBy: {order}")
        if order == "startTime" and not single:
            raise cal.refused(400, "badRequest", "The requested ordering is not available for the particular query.")
        words = (_param(request, "q") or "").casefold().split()
        found: list[tuple[datetime, cal.StoredEvent]] = []
        for event, _ in self._calendars.on(calendar.id):
            start = cal.instant(event.start, calendar.timeZone, "start")
            end = cal.instant(event.end, calendar.timeZone, "end")
            if not cal.overlaps(start, end, low, high):
                continue
            if updated_min is not None and wire.moment(event.updated) < updated_min:
                continue
            if words and not all(w in self._searchable(event) for w in words):
                continue
            found.append((start, event))
        if order == "updated":  # enum-lint: exempt Calendar's orderBy value
            found.sort(key=lambda pair: pair[1].updated)
        else:
            found.sort(key=lambda pair: (pair[0], pair[1].id))
        size = self._results(request)
        offset = self._offset(request)
        page = [self._served(e, calendar.id, caller) for _, e in found[offset : offset + size]]
        more = offset + size < len(found)
        seq, changed = self._last_change(calendar.id)
        answer = cal.EventList(
            etag=f'"{seq}"',
            summary=calendar.id,
            updated=wire.rfc3339(changed),
            timeZone=calendar.timeZone,
            nextPageToken=wire.encode_page(offset + size) if more else None,
            nextSyncToken=None if more else f"s{seq}",
            items=page,
        )
        self._calendars.saw(record_ref(calendar_parent(calendar.id)), Operation.SEARCH)
        return _json(answer, request, cal.EventList)

    def _incremental(self, request: Request, caller: Caller, calendar: cal.CalendarListEntry, sync: str) -> Response:
        """Every event on the calendar changed since the sync token, a deleted one as `cancelled`, oldest change
        first. A token cannot be combined with the filters a full list takes, as Calendar's guide says."""
        for name in ("timeMin", "timeMax", "q", "orderBy", "updatedMin", "iCalUID", "privateExtendedProperty"):
            if name in request.query_params:
                raise cal.refused(400, "invalid", f"The {name} parameter cannot be used with a syncToken.")
        store = self._calendars.store
        if not sync.startswith("s") or not sync[1:].isdigit() or int(sync[1:]) > store.head():
            raise cal.refused(410, "fullSyncRequired", "Sync token is no longer valid, a full sync is required.")
        items: list[cal.StoredEvent | cal.CancelledEvent] = []
        for event_key, seq, _ in self._changes(calendar.id, since=int(sync[1:])):
            current = self._calendars.event(event_key)
            if current is None:
                items.append(cal.CancelledEvent(etag=f'"{seq}"', id=event_key))
            else:
                items.append(self._served(current[0], calendar.id, caller))
        last, changed = self._last_change(calendar.id)
        answer = cal.IncrementalList(
            etag=f'"{last}"',
            summary=calendar.id,
            updated=wire.rfc3339(changed),
            timeZone=calendar.timeZone,
            nextSyncToken=f"s{last}",
            items=items,
        )
        self._calendars.saw(record_ref(calendar_parent(calendar.id)), Operation.SEARCH)
        return _json(answer, request, cal.IncrementalList)

    def _changes(self, calendar: str, *, since: int) -> list[tuple[str, int, datetime]]:
        """Each event on the calendar changed after `since`, a deleted one included: its id, and the event and the
        moment of its latest change, oldest change first."""
        store = self._calendars.store
        mine = calendar.lower()
        latest: dict[str, tuple[int, datetime]] = {}
        for event in store.events(since=since):
            ref = event.entity
            if (
                ref.provider == MANIFEST.key
                and ref.kind is EntityKind.MESSAGE
                and event.operation not in (Operation.READ, Operation.SEARCH)
            ):
                latest[ref.external_id] = (event.seq, event.sim_time)
        found: list[tuple[str, int, datetime]] = []
        for event_key, (seq, moment) in sorted(latest.items(), key=lambda pair: pair[1][0]):
            versions = [v for v in store.versions(event_ref(event_key)) if v.seq <= seq]
            kept = [k for k in (cal.KEPT.validate_json(v.body) for v in versions) if isinstance(k, cal.StoredEvent)]
            if not kept:
                continue
            last = kept[-1]
            if last.organizer.email.lower() == mine or any(a.email.lower() == mine for a in last.attendees or []):
                found.append((event_key, seq, moment))
        return found

    def _last_change(self, calendar: str) -> tuple[int, datetime]:
        """When the calendar last changed: the latest change to an event on it, else the calendar's making. An
        events list answers it as `updated`, "the last modification time of the calendar"
        (https://developers.google.com/workspace/calendar/api/v3/reference/events/list#response), and its etag and
        sync token follow it, so a list read again with nothing changed answers the same."""
        made = self._calendars.store.get(record_ref(calendar_parent(calendar)))
        since = made.seq if made is not None else 0
        changes = self._changes(calendar, since=since)
        if changes:
            _, seq, moment = changes[-1]
            return seq, moment
        if made is not None:
            return made.seq, made.sim_time
        first = self._calendars.store.events(since=0)
        return 0, first[0].sim_time if first else self._clock.now()

    def _searchable(self, event: cal.StoredEvent) -> str:
        guests = [f"{a.email} {a.displayName or ''}" for a in event.attendees or []]
        return " ".join([event.summary or "", event.description or "", event.location or "", *guests]).casefold()

    def _results(self, request: Request) -> int:
        spelled = _param(request, "maxResults")
        if spelled is None:
            return cal.DEFAULT_RESULTS
        if not spelled.isdigit() or int(spelled) < 1:
            raise cal.refused(400, "invalid", f"Invalid value for: maxResults: {spelled}")
        return min(int(spelled), cal.MAX_RESULTS)

    def _offset(self, request: Request) -> int:
        try:
            return wire.decode_page(_param(request, "pageToken"))
        except wire.Refusal as refused:
            raise cal.refused(400, "invalid", "Invalid page token") from refused

    async def freebusy(self, request: Request, caller: Caller) -> Response:
        asked = wire.read_body(cal.FreeBusyRequest, wire.read_object(await request.body()))
        if asked.timeMin is None:
            raise cal.refused(400, "required", "Missing timeMin parameter.")
        if asked.timeMax is None:
            raise cal.refused(400, "required", "Missing timeMax parameter.")
        low, high = cal.moment(asked.timeMin, "timeMin"), cal.moment(asked.timeMax, "timeMax")
        if high <= low:
            raise cal.refused(400, "timeRangeEmpty", "The specified time range is empty.")
        calendars: dict[str, cal.FreeBusyCalendar] = {}
        for item in asked.items:
            address = caller.email if item.id == "primary" else item.id
            found = self._calendars.calendar(address)
            if found is None:
                calendars[item.id] = cal.FreeBusyCalendar(
                    errors=[cal.FreeBusyError(domain="global", reason="notFound")], busy=[]
                )
                continue
            ranges: list[tuple[datetime, datetime]] = []
            mine = found.id.lower()
            for event, _ in self._calendars.on(found.id):
                if event.transparency == "transparent":
                    continue
                if any(a.email.lower() == mine and a.responseStatus == "declined" for a in event.attendees or []):
                    continue
                start = cal.instant(event.start, found.timeZone, "start")
                end = cal.instant(event.end, found.timeZone, "end")
                if cal.overlaps(start, end, low, high):
                    ranges.append((max(start, low), min(end, high)))
            calendars[item.id] = cal.FreeBusyCalendar(
                busy=[cal.BusyRange(start=cal.utc(s), end=cal.utc(e)) for s, e in cal.merged(ranges)]
            )
            self._calendars.saw(record_ref(calendar_parent(found.id)), Operation.READ)
        answer = cal.FreeBusy(timeMin=cal.utc(low), timeMax=cal.utc(high), calendars=calendars)
        return _json(answer, request, cal.FreeBusy)

    async def not_built(self, request: Request) -> Response:
        return _refused(
            mail.not_implemented(f"minutehand's Google Calendar does not implement {request.method} {request.url.path}")
        )

    def routes(self) -> list[Route]:
        def call(handler: Handler, operation: str) -> Callable[[Request], Awaitable[Response]]:
            return self.guarded(handler, operation)

        events = "/calendar/v3/calendars/{calendar_id}/events"
        return [
            Route("/calendar/v3/users/me/calendarList", call(self.calendar_list, "calendarList.list"), methods=["GET"]),
            Route(events, call(self.events_list, "events.list"), methods=["GET"]),
            Route(events, call(self.events_insert, "events.insert"), methods=["POST"]),
            Route(f"{events}/{{event_id}}", call(self.events_get, "events.get"), methods=["GET"]),
            Route(f"{events}/{{event_id}}", call(self.events_patch, "events.patch"), methods=["PATCH"]),
            Route(f"{events}/{{event_id}}", call(self.events_update, "events.update"), methods=["PUT"]),
            Route(f"{events}/{{event_id}}", call(self.events_delete, "events.delete"), methods=["DELETE"]),
            Route("/calendar/v3/freeBusy", call(self.freebusy, "freebusy.query"), methods=["POST"]),
            Route("/calendar/v3/{rest:path}", self.not_built, methods=EVERY_METHOD),
        ]
