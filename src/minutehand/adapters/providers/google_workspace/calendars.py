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
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from urllib.parse import quote

from pydantic import Field, JsonValue
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.google_workspace import calendar_wire as cal
from minutehand.adapters.providers.google_workspace import gmail_wire as mail
from minutehand.adapters.providers.google_workspace import wire
from minutehand.adapters.providers.google_workspace.access import Caller, bearer, due_fault, signed_in
from minutehand.adapters.providers.google_workspace.channels import CALENDAR_LIFETIME, Channels
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
    WorldEvent,
)
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

JSON = "application/json; charset=UTF-8"
CALENDARS = "calendars"
_SCAN = 1000


class Method(Model):
    """One method of Calendar v3's discovery document (https://www.googleapis.com/discovery/v1/apis/calendar/v3/rest):
    its name without the `calendar.` prefix, its HTTP method and path under `/calendar/v3/`, the parameters this
    fake serves (`serves`), and the documented parameters it does not serve yet (`refuses`), each answered 501
    naming the method and the parameter."""

    name: str
    http: str
    path: str
    serves: frozenset[str] = frozenset()
    refuses: frozenset[str] = frozenset()


_LIST_PARAMETERS = frozenset(
    {
        "alwaysIncludeEmail",
        "calendarId",
        "eventTypes",
        "iCalUID",
        "maxAttendees",
        "maxResults",
        "orderBy",
        "pageToken",
        "privateExtendedProperty",
        "q",
        "sharedExtendedProperty",
        "showDeleted",
        "showHiddenInvitations",
        "singleEvents",
        "syncToken",
        "timeMax",
        "timeMin",
        "timeZone",
        "updatedMin",
    }
)
_EVENTS_LIST_SERVES = frozenset(
    {
        "alwaysIncludeEmail",  # "Deprecated and ignored."
        "calendarId",
        "maxResults",
        "orderBy",
        "pageToken",
        "q",
        "showDeleted",
        "singleEvents",
        "syncToken",
        "timeMax",
        "timeMin",
        "updatedMin",
    }
)
_WRITE_SERVES = frozenset({"alwaysIncludeEmail", "calendarId", "eventId", "sendNotifications", "sendUpdates"})
_WRITE_REFUSES = frozenset({"conferenceDataVersion", "eventLabelVersion", "maxAttendees", "supportsAttachments"})

SERVED: tuple[Method, ...] = (
    Method(name="acl.insert", http="POST", path="calendars/{calendarId}/acl",
           serves=frozenset({"calendarId", "sendNotifications"})),
    Method(name="acl.list", http="GET", path="calendars/{calendarId}/acl",
           serves=frozenset({"calendarId", "maxResults", "pageToken", "showDeleted"}),
           refuses=frozenset({"syncToken"})),
    Method(name="calendarList.insert", http="POST", path="users/me/calendarList",
           refuses=frozenset({"colorRgbFormat"})),
    Method(name="calendarList.list", http="GET", path="users/me/calendarList",
           serves=frozenset({"maxResults", "minAccessRole", "pageToken", "showDeleted", "showHidden"}),
           refuses=frozenset({"showOwnOrganizationOnly", "syncToken"})),
    Method(name="calendars.get", http="GET", path="calendars/{calendarId}", serves=frozenset({"calendarId"})),
    Method(name="calendars.insert", http="POST", path="calendars"),
    Method(name="channels.stop", http="POST", path="channels/stop"),
    Method(name="events.delete", http="DELETE", path="calendars/{calendarId}/events/{eventId}",
           serves=frozenset({"calendarId", "eventId", "sendNotifications", "sendUpdates"})),
    Method(name="events.get", http="GET", path="calendars/{calendarId}/events/{eventId}",
           serves=frozenset({"alwaysIncludeEmail", "calendarId", "eventId"}),
           refuses=frozenset({"maxAttendees", "timeZone"})),
    Method(name="events.insert", http="POST", path="calendars/{calendarId}/events",
           serves=frozenset({"calendarId", "sendNotifications", "sendUpdates"}), refuses=_WRITE_REFUSES),
    Method(name="events.list", http="GET", path="calendars/{calendarId}/events", serves=_EVENTS_LIST_SERVES,
           refuses=_LIST_PARAMETERS - _EVENTS_LIST_SERVES),
    Method(name="events.patch", http="PATCH", path="calendars/{calendarId}/events/{eventId}",
           serves=_WRITE_SERVES, refuses=_WRITE_REFUSES),
    Method(name="events.update", http="PUT", path="calendars/{calendarId}/events/{eventId}",
           serves=_WRITE_SERVES, refuses=_WRITE_REFUSES),
    Method(name="events.watch", http="POST", path="calendars/{calendarId}/events/watch",
           serves=frozenset({"calendarId"}), refuses=_LIST_PARAMETERS - {"calendarId"}),
    Method(name="freebusy.query", http="POST", path="freeBusy"),
)  # fmt: skip
"""Every Calendar method this fake serves, with the parameters it serves and those it refuses by name."""

REFUSED: tuple[Method, ...] = (
    Method(name="acl.delete", http="DELETE", path="calendars/{calendarId}/acl/{ruleId}"),
    Method(name="acl.get", http="GET", path="calendars/{calendarId}/acl/{ruleId}"),
    Method(name="acl.patch", http="PATCH", path="calendars/{calendarId}/acl/{ruleId}"),
    Method(name="acl.update", http="PUT", path="calendars/{calendarId}/acl/{ruleId}"),
    Method(name="acl.watch", http="POST", path="calendars/{calendarId}/acl/watch"),
    Method(name="calendarList.delete", http="DELETE", path="users/me/calendarList/{calendarId}"),
    Method(name="calendarList.get", http="GET", path="users/me/calendarList/{calendarId}"),
    Method(name="calendarList.patch", http="PATCH", path="users/me/calendarList/{calendarId}"),
    Method(name="calendarList.update", http="PUT", path="users/me/calendarList/{calendarId}"),
    Method(name="calendarList.watch", http="POST", path="users/me/calendarList/watch"),
    Method(name="calendars.clear", http="POST", path="calendars/{calendarId}/clear"),
    Method(name="calendars.delete", http="DELETE", path="calendars/{calendarId}"),
    Method(name="calendars.patch", http="PATCH", path="calendars/{calendarId}"),
    Method(name="calendars.transferOwnership", http="POST", path="calendars/{calendarId}/transferOwnership"),
    Method(name="calendars.update", http="PUT", path="calendars/{calendarId}"),
    Method(name="colors.get", http="GET", path="colors"),
    Method(name="events.import", http="POST", path="calendars/{calendarId}/events/import"),
    Method(name="events.instances", http="GET", path="calendars/{calendarId}/events/{eventId}/instances"),
    Method(name="events.move", http="POST", path="calendars/{calendarId}/events/{eventId}/move"),
    Method(name="events.quickAdd", http="POST", path="calendars/{calendarId}/events/quickAdd"),
    Method(name="settings.get", http="GET", path="users/me/settings/{setting}"),
    Method(name="settings.list", http="GET", path="users/me/settings"),
    Method(name="settings.watch", http="POST", path="users/me/settings/watch"),
)
"""Every other method of the discovery document: each answered 501 naming it."""

STANDARD_PARAMETERS = frozenset({"alt", "fields", "key", "oauth_token", "prettyPrint", "quotaUser", "userIp"})
"""The parameters the discovery document gives every method. `fields` selects; `alt` has the one value `json`;
`key`, `quotaUser` and `userIp` say whose quota a call counts against, which no answer shows; `oauth_token` is the
caller's token, read as `access_token` is; `prettyPrint` indents the answer."""

OPERATIONS = frozenset(m.name for m in SERVED)
"""Every Calendar call a fault may name, by Google's own method name."""


class CalendarRecord(Model):
    """A calendar: an account's primary, seeded for each person, whose id is its address; or a secondary one
    `calendars.insert` made, whose data owner is the account that made it."""

    id: str
    timeZone: str
    summary: str | None = Field(default=None, description="As written; None on a primary, read as its address")
    description: str | None = None
    location: str | None = None
    dataOwner: str | None = Field(default=None, description="The account that made it; None on a primary")


ROLES = ("none", "freeBusyReader", "reader", "writerWithoutPrivateAccess", "writer", "owner")
"""An ACL rule's roles, least first, as the AclRule reference lists them."""


def calendar_parent(address: str) -> str:
    return f"calendar:{address.lower()}"


def acl_parent(calendar: str) -> str:
    return f"acl:{calendar.lower()}"


def list_parent(address: str) -> str:
    return f"calendarList:{address.lower()}"


def calendar_id(seq: int) -> str:
    """An id for a calendar `calendars.insert` made, from the change that made it."""
    return hashlib.sha256(f"calendar\x1f{seq}".encode()).hexdigest()[:26] + "@group.calendar.google.com"


def event_ref(event: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.MESSAGE, external_id=event)


def record_ref(external_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=external_id)


def event_id(seq: int) -> str:
    """An id Calendar would mint: base32hex, from the event that made it."""
    digest = hashlib.sha256(f"event\x1f{seq}".encode()).digest()[:15]
    return base64.b32hexencode(digest).decode().lower().rstrip("=")


def calendars_of(*events: cal.StoredEvent) -> set[str]:
    """Every calendar the events are on: their organizers' and their guests'."""
    return {a for e in events for a in (e.organizer.email, *(g.email for g in e.attendees or []))}


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


class HeldCalendar(Model):
    record: CalendarRecord
    seq: int = Field(description="The change that last wrote it, its etag; 0 for an account seeded without one")

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def summary(self) -> str:
        return self.record.summary if self.record.summary is not None else self.record.id

    @property
    def timeZone(self) -> str:
        return self.record.timeZone


class CalendarWorld:
    """Typed reads and writes of the run's calendars and events."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._drive = DriveWorld(store)

    @property
    def store(self) -> Store:
        return self._store

    def calendar(self, calendar_id: str) -> HeldCalendar | None:
        """A calendar by its id (an address names its account's primary, in any case); None when there is none."""
        held = self._store.get(record_ref(calendar_parent(calendar_id)))
        if held is not None and held.parent == CALENDARS:
            return HeldCalendar(record=CalendarRecord.model_validate_json(held.body), seq=held.seq)
        user = self._drive.user(calendar_id)
        if user is None or user.emailAddress is None:
            return None
        return HeldCalendar(record=CalendarRecord(id=user.emailAddress, timeZone="UTC"), seq=0)

    def secondary(self) -> list[HeldCalendar]:
        """Every calendar `calendars.insert` made."""
        found: list[HeldCalendar] = []
        for stored in self._store.children(MANIFEST.key, EntityKind.RECORD, CALENDARS, limit=_SCAN):
            record = CalendarRecord.model_validate_json(stored.body)
            if record.dataOwner is not None:
                found.append(HeldCalendar(record=record, seq=stored.seq))
        return found

    def keep_calendar(self, record: CalendarRecord, *, actor: Actor, operation: Operation = Operation.CREATE) -> int:
        return self._store.apply(
            Change(
                entity=record_ref(calendar_parent(record.id)),
                operation=operation,
                actor=actor,
                body=wire.dump(record),
                parent=CALENDARS,
            )
        ).seq

    def rules(self, calendar: CalendarRecord) -> list[cal.AclRule]:
        """The rules `acl.insert` wrote on the calendar, a later one for a scope replacing the earlier."""
        stored = self._store.children(MANIFEST.key, EntityKind.RECORD, acl_parent(calendar.id), limit=_SCAN)
        return [cal.AclRule.model_validate_json(s.body) for s in stored]

    def listed(self, address: str) -> list[str]:
        """The calendars `calendarList.insert` put on an account's calendar list."""
        stored = self._store.children(MANIFEST.key, EntityKind.RECORD, list_parent(address), limit=_SCAN)
        return [CalendarRecord.model_validate_json(s.body).id for s in stored]

    def keep_listed(self, address: str, calendar: CalendarRecord, *, actor: Actor) -> None:
        ref = record_ref(f"{list_parent(address)}:{calendar.id.lower()}")
        self._store.apply(
            Change(
                entity=ref,
                operation=Operation.UPDATE if self._store.get(ref) is not None else Operation.CREATE,
                actor=actor,
                body=wire.dump(calendar),
                parent=list_parent(address),
            )
        )

    def keep_rule(self, calendar: CalendarRecord, rule: cal.AclRule, *, actor: Actor) -> None:
        ref = record_ref(f"{acl_parent(calendar.id)}:{rule.id}")
        held = self._store.get(ref)
        self._store.apply(
            Change(
                entity=ref,
                operation=Operation.UPDATE if held is not None else Operation.CREATE,
                actor=actor,
                body=wire.dump(rule),
                parent=acl_parent(calendar.id),
            )
        )

    def role(self, calendar: CalendarRecord, address: str) -> str | None:
        """The access role an account has on a calendar by its ACL, None when no rule names it."""
        mine = address.lower()
        if calendar.dataOwner is not None and calendar.dataOwner.lower() == mine:
            return "owner"
        named = [r for r in self.rules(calendar) if r.scope.type == "user" and (r.scope.value or "").lower() == mine]
        return named[-1].role if named and named[-1].role != "none" else None  # enum-lint: exempt Calendar ACL role

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

    def deleted(self, event: str) -> tuple[cal.StoredEvent, WorldEvent] | None:
        """A deleted event: its last version, and the change that deleted it; None when it is not deleted."""
        if self._store.get(event_ref(event)) is not None:
            return None
        kept = [k for k in (cal.KEPT.validate_json(v.body) for v in self._store.versions(event_ref(event)))]
        last = [k for k in kept if isinstance(k, cal.StoredEvent)]
        if not last:
            return None
        removal = [e for e in self._store.events() if e.entity == event_ref(event) and e.operation is Operation.DELETE]
        return (last[-1], removal[-1]) if removal else None

    def deletions(self) -> list[tuple[cal.StoredEvent, WorldEvent]]:
        """Every deleted event, with the change that deleted it."""
        found: list[tuple[cal.StoredEvent, WorldEvent]] = []
        for change in self._store.events():
            ref = change.entity
            if ref.provider == MANIFEST.key and ref.kind is EntityKind.MESSAGE and change.operation is Operation.DELETE:
                gone = self.deleted(ref.external_id)
                if gone is not None and gone[1].seq == change.seq:
                    found.append(gone)
        return found

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

    def on(self, calendar_id: str) -> list[tuple[cal.StoredEvent, Stored]]:
        """Every event on a calendar: those it organizes and those its account is a guest of."""
        wanted = calendar_id.lower()
        organizers = [u.emailAddress for u in self._drive.users() if u.emailAddress is not None]
        organizers += [c.id for c in self.secondary()]
        found: list[tuple[cal.StoredEvent, Stored]] = []
        for organizer in organizers:
            for event, stored in self.organized(organizer):
                if organizer.lower() == wanted or any(a.email.lower() == wanted for a in event.attendees or []):
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

    def land(self, reply: PersonReply, clock: Clock) -> set[str]:
        """A guest answers an invitation: a press sets their `responseStatus`, written text their response comment.
        An event deleted since, or a guest no longer on it, is left alone. Answers the calendars the event changed
        on, none when it was left alone."""
        found = self.event(reply.in_reply_to.external_id)
        person = self._drive.person(reply.person)
        if found is None or person is None or person.emailAddress is None:
            return set()
        event, _ = found
        address = person.emailAddress.lower()
        attendees = list(event.attendees or [])
        place = next((n for n, a in enumerate(attendees) if a.email.lower() == address), None)
        if place is None:
            return set()
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
        return calendars_of(answered)

    def saw(self, ref: EntityRef, operation: Operation) -> None:
        self._store.apply(Change(entity=ref, operation=operation, actor=Actor.AGENT))


# ---------------------------------------------------------------------- the API

Handler = Callable[[Request, Caller], Awaitable[Response]]


def _pretty(request: Request, body: bytes) -> bytes:
    """`prettyPrint`, true unless it says false, "Returns response with indentations and line breaks": two spaces a
    level, as Google indents (`tests/data/google_calendar_v3/unauthenticated-calendar-list-2026-10-08.txt`)."""
    if _param(request, "prettyPrint") == "false":
        return body
    return json.dumps(json.loads(body), indent=2, ensure_ascii=False).encode() + b"\n"


def _json(answer: Model, request: Request, model: type[Model], status: int = 200) -> Response:
    fields = request.query_params["fields"] if "fields" in request.query_params else None
    body = wire.respond(answer, wire.selection(fields, model, "*"))
    return Response(_pretty(request, body), status_code=status, media_type=JSON)


def _refused(refusal: wire.Refusal, request: Request) -> Response:
    body = _pretty(request, wire.error_body(refusal))
    return Response(body, status_code=refusal.code, media_type=JSON, headers=refusal.headers)


def _param(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def _parameters(request: Request, method: Method) -> None:
    """A documented parameter this fake does not serve yet is refused, naming it; `alt` has one value, `json`."""
    for name in request.query_params:
        if name not in method.serves | method.refuses | STANDARD_PARAMETERS | {"access_token"}:
            raise mail.not_implemented(
                f"minutehand's Google Calendar does not serve the parameter {name} of calendar.{method.name}: the "
                "discovery document does not name it, and Google's answer to it is not recorded"
            )
        if name in method.refuses:
            raise mail.not_implemented(
                f"minutehand's Google Calendar does not serve the parameter {name} of calendar.{method.name}"
            )
    alt = _param(request, "alt")
    if alt is not None and alt != "json":  # enum-lint: exempt Calendar alt value
        raise mail.not_implemented(f"minutehand's Google Calendar does not serve alt={alt}")


def _latest(etags: list[str]) -> str:
    """A collection's etag: its latest member's, so reading it again unchanged answers the same."""
    return max(etags, key=lambda e: int(e.strip('"')), default='"0"')


def _insufficient(needs: str) -> wire.Refusal:
    """A caller holding a role on a calendar below what the call needs: Google's answer is not recorded."""
    return mail.not_implemented(
        f"minutehand's Google Calendar does not serve this call to a caller holding less than {needs} on the calendar"
    )


def _not_found() -> wire.Refusal:
    return cal.refused(404, "notFound", "Not Found")


class CalendarApi:
    def __init__(self, store: Store, clock: Clock, channels: Channels) -> None:
        self._calendars = CalendarWorld(store)
        self._drive = DriveWorld(store)
        self._clock = clock
        self._channels = channels

    @property
    def calendars(self) -> CalendarWorld:
        return self._calendars

    def guarded(self, handler: Handler, method: Method) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            try:
                token = _param(request, "access_token") or _param(request, "oauth_token")
                caller = signed_in(self._drive, self._clock, bearer(request, token), missing=wire.login_required())
                _parameters(request, method)
                kind = due_fault(self._drive, self._clock, method.name)
                if kind is not None:
                    raise mail.fault(kind)
                return await handler(request, caller)
            except wire.Refusal as refusal:
                return _refused(refusal, request)

        return endpoint

    def _role(self, calendar: HeldCalendar, caller: Caller) -> str | None:
        """The caller's access role on a calendar: owner of their own primary, else what its ACL grants them."""
        if calendar.record.dataOwner is None and calendar.id.lower() == caller.email.lower():
            return "owner"
        return self._calendars.role(calendar.record, caller.email)

    def _own(self, request: Request, caller: Caller, *, needs: str = "reader") -> HeldCalendar:
        """The calendar the path names (`primary` is the caller's own), which the caller must hold at least `needs`
        on. A calendar the caller has no role on is a 404, as one that does not exist is."""
        spelled = request.path_params["calendarId"]
        found = self._calendars.calendar(caller.email if spelled == "primary" else spelled)
        role = self._role(found, caller) if found is not None else None
        if found is None or role is None:
            raise _not_found()
        if ROLES.index(role) < ROLES.index(needs):
            raise _insufficient(needs)
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

    def _on_calendar(self, event: cal.StoredEvent, calendar: str) -> bool:
        mine = calendar.lower()
        return event.organizer.email.lower() == mine or any(a.email.lower() == mine for a in event.attendees or [])

    def _cancelled(
        self, event: cal.StoredEvent, removal: WorldEvent, calendar: str, caller: Caller, *, details: bool
    ) -> cal.StoredEvent | cal.CancelledEvent:
        """A deleted event as Calendar answers it: "Deleted events are only guaranteed to have the id field
        populated"; "On the organizer's calendar, cancelled events continue to expose event details", which an
        incremental sync without `showDeleted` leaves out."""
        etag = f'"{removal.seq}"'
        if details and event.organizer.email.lower() == calendar.lower():
            gone = event.model_copy(
                update={"status": "cancelled", "etag": etag, "updated": wire.rfc3339(removal.sim_time)}
            )
            return self._served(gone, calendar, caller)
        return cal.CancelledEvent(etag=etag, id=event.id)

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
                    resource=guest.resource,
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

    def _entry(self, calendar: HeldCalendar, role: str, caller: Caller) -> cal.CalendarListEntry:
        record = calendar.record
        return cal.CalendarListEntry(
            etag=f'"{calendar.seq}"',
            id=record.id,
            summary=calendar.summary,
            description=record.description,
            location=record.location,
            timeZone=record.timeZone,
            dataOwner=record.dataOwner,
            accessRole=role,
            primary=True if record.dataOwner is None and record.id.lower() == caller.email.lower() else None,
        )

    def _listed(self, caller: Caller) -> list[cal.CalendarListEntry]:
        """The caller's calendar list: their primary, each secondary calendar they are the data owner of, and each
        calendar they added with `calendarList.insert` and still hold a role on. Sharing a calendar does not add it
        to the grantee's list."""
        found: list[cal.CalendarListEntry] = []
        own = self._calendars.calendar(caller.email)
        if own is not None:
            found.append(self._entry(own, "owner", caller))
        added = {i.lower() for i in self._calendars.listed(caller.email)}
        for calendar in self._calendars.secondary():
            owned = (calendar.record.dataOwner or "").lower() == caller.email.lower()
            role = self._role(calendar, caller)
            if role is not None and (owned or calendar.id.lower() in added):
                found.append(self._entry(calendar, role, caller))
        for spelled in added:
            calendar = self._calendars.calendar(spelled)
            if calendar is None or calendar.record.dataOwner is not None or calendar.id.lower() == caller.email.lower():
                continue
            role = self._role(calendar, caller)
            if role is not None:
                found.append(self._entry(calendar, role, caller))
        return found

    async def calendar_list_insert(self, request: Request, caller: Caller) -> Response:
        """Add a calendar the caller holds a role on to their calendar list; one they cannot see is a 404."""
        asked = wire.read_body(cal.CalendarListWrite, wire.read_object(await request.body()))
        if not asked.id:
            raise mail.not_implemented("minutehand's Google Calendar does not serve calendarList.insert without an id")
        calendar = self._calendars.calendar(asked.id)
        role = self._role(calendar, caller) if calendar is not None else None
        if calendar is None or role is None:
            raise _not_found()
        self._calendars.keep_listed(caller.email, calendar.record, actor=Actor.AGENT)
        return _json(self._entry(calendar, role, caller), request, cal.CalendarListEntry)

    async def calendar_list(self, request: Request, caller: Caller) -> Response:
        least = _param(request, "minAccessRole")
        if least is not None and least not in ROLES[1:]:
            raise cal.refused(400, "invalid", f"Invalid value for: minAccessRole: {least}")
        items = [e for e in self._listed(caller) if least is None or ROLES.index(e.accessRole) >= ROLES.index(least)]
        size, offset = self._results(request), self._offset(request)
        more = offset + size < len(items)
        self._calendars.saw(record_ref(calendar_parent(caller.email)), Operation.READ)
        answer = cal.CalendarList(
            etag=_latest([e.etag for e in items]),
            nextPageToken=wire.encode_page(offset + size) if more else None,
            items=items[offset : offset + size],
        )
        return _json(answer, request, cal.CalendarList)

    def _resource(self, calendar: HeldCalendar) -> cal.CalendarResource:
        record = calendar.record
        return cal.CalendarResource(
            etag=f'"{calendar.seq}"',
            id=record.id,
            summary=calendar.summary,
            description=record.description,
            location=record.location,
            timeZone=record.timeZone,
            dataOwner=record.dataOwner,
        )

    async def calendars_get(self, request: Request, caller: Caller) -> Response:
        """`primary` or a calendar the caller holds a role on; any other is a 404 `notFound`."""
        calendar = self._own(request, caller)
        self._calendars.saw(record_ref(calendar_parent(calendar.id)), Operation.READ)
        return _json(self._resource(calendar), request, cal.CalendarResource)

    async def calendars_insert(self, request: Request, caller: Caller) -> Response:
        """A secondary calendar whose data owner is the caller, on their calendar list as its owner."""
        asked = wire.read_body(cal.CalendarWrite, wire.read_object(await request.body()))
        if not asked.summary or not asked.timeZone:
            raise mail.not_implemented(
                "minutehand's Google Calendar does not serve calendars.insert without a summary and a timeZone: "
                "Google's answer to either missing is not recorded"
            )
        cal.zone(asked.timeZone)
        seq = self._calendars.store.head() + 1
        record = CalendarRecord(
            id=calendar_id(seq),
            summary=asked.summary,
            description=asked.description,
            location=asked.location,
            timeZone=asked.timeZone,
            dataOwner=caller.email,
        )
        kept = self._calendars.keep_calendar(record, actor=Actor.AGENT)
        return _json(self._resource(HeldCalendar(record=record, seq=kept)), request, cal.CalendarResource)

    async def acl_list(self, request: Request, caller: Caller) -> Response:
        """The calendar's rules, for a caller holding writer or owner on it ("Provides read access to the
        calendar's ACLs")."""
        calendar = self._own(request, caller, needs="writer")
        rules = self._calendars.rules(calendar.record)
        size, offset = self._results(request), self._offset(request)
        more = offset + size < len(rules)
        self._calendars.saw(record_ref(acl_parent(calendar.id)), Operation.SEARCH)
        answer = cal.Acl(
            etag=_latest([f'"{calendar.seq}"', *(r.etag for r in rules)]),
            nextPageToken=wire.encode_page(offset + size) if more else None,
            items=rules[offset : offset + size],
        )
        return _json(answer, request, cal.Acl)

    async def acl_insert(self, request: Request, caller: Caller) -> Response:
        """A rule granting one user a role on a calendar the caller owns."""
        calendar = self._own(request, caller, needs="owner")
        asked = wire.read_body(cal.AclWrite, wire.read_object(await request.body()))
        if asked.role is None:
            raise cal.refused(400, "required", "Missing role.")
        if asked.scope is None or not asked.scope.type:
            raise cal.refused(400, "required", "Missing scope type.")
        if asked.role not in ROLES:
            raise cal.refused(400, "invalid", f"Invalid role: {asked.role}")
        if asked.role not in ("writer", "owner"):  # enum-lint: exempt Calendar ACL roles
            raise mail.not_implemented(f"minutehand's Google Calendar does not serve the ACL role {asked.role}")
        if asked.scope.type != "user":
            raise mail.not_implemented(f"minutehand's Google Calendar does not serve the ACL scope {asked.scope.type}")
        if not asked.scope.value:
            raise cal.refused(400, "required", "Missing scope value.")
        rule = cal.AclRule(
            etag=f'"{self._calendars.store.head() + 1}"',
            id=f"user:{asked.scope.value}",
            scope=cal.AclScope(type="user", value=asked.scope.value),
            role=asked.role,
        )
        self._calendars.keep_rule(calendar.record, rule, actor=Actor.AGENT)
        return _json(rule, request, cal.AclRule)

    def _write(self, found: dict[str, JsonValue], held: cal.StoredEvent | None) -> cal.EventWrite:
        """The body of an insert, patch or update, read: a field this fake does not serve yet is refused, naming
        it; a read-only one (an event read and sent back) is ignored."""
        for name in cal.NOT_SERVED:
            if name in found and found[name] is not None:
                raise mail.not_implemented(f"minutehand's Google Calendar does not serve the event field {name}")
        asked = wire.read_body(cal.EventWrite, found)
        if asked.recurrence:
            raise mail.not_implemented("minutehand's Google Calendar does not serve the event field recurrence")
        if asked.eventType is not None and asked.eventType != (held.eventType if held is not None else "default"):
            raise mail.not_implemented(f"minutehand's Google Calendar does not serve eventType {asked.eventType}")
        if asked.iCalUID is not None and (held is None or asked.iCalUID != held.iCalUID):
            raise mail.not_implemented("minutehand's Google Calendar does not serve writing an event's iCalUID")
        return asked

    def _matches(self, request: Request, event: cal.StoredEvent) -> None:
        """`If-Match`: the change goes ahead only while the event's etag is the one named; otherwise a 412
        `conditionNotMet` and nothing changes."""
        if "if-match" not in request.headers:
            return
        named = [t.strip() for t in request.headers["if-match"].split(",")]
        if "*" in named or event.etag in named:
            return
        item = wire.ErrorItem(
            domain="global",
            reason="conditionNotMet",
            message="Precondition Failed",
            locationType="header",
            location="If-Match",
        )
        raise wire.Refusal(
            wire.GoogleError(error=wire.ErrorBody(code=412, message="Precondition Failed", errors=[item]))
        )

    async def events_insert(self, request: Request, caller: Caller) -> Response:
        calendar = self._own(request, caller, needs="writer")
        self._send_updates(request)
        found = wire.read_object(await request.body())
        asked = self._write(found, None)
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
        event = cal.StoredEvent(
            etag=f'"{seq}"',
            id=new_id,
            status=asked.status or "confirmed",
            htmlLink=html_link(new_id, calendar.id),
            created=now,
            updated=now,
            creator=cal.Actor(email=caller.email),
            organizer=cal.Actor(email=calendar.id),
            start=start,
            end=end,
            iCalUID=f"{new_id}@google.com",
            sequence=asked.sequence or 0,
            attendees=self._attendees(asked.attendees or [], calendar.id, []) or None,
            **{name: getattr(asked, name) for name in cal.VERBATIM},
        )
        self._calendars.write(event, operation=Operation.CREATE, actor=Actor.AGENT, snapshot=True)
        self._channels.tell_calendars_later(calendars_of(event))
        return _json(self._served(event, calendar.id, caller), request, cal.StoredEvent)

    async def events_get(self, request: Request, caller: Caller) -> Response:
        """An event on the calendar; a deleted one as its cancelled copy, which "the get method always returns"."""
        calendar = self._own(request, caller)
        spelled = request.path_params["eventId"]
        gone = self._calendars.deleted(spelled)
        if gone is not None and self._on_calendar(gone[0], calendar.id):
            self._calendars.saw(event_ref(spelled), Operation.READ)
            return _json(self._cancelled(gone[0], gone[1], calendar.id, caller, details=True), request, cal.StoredEvent)
        event, _ = self._visible(spelled, calendar.id)
        self._calendars.saw(event_ref(event.id), Operation.READ)
        return _json(self._served(event, calendar.id, caller), request, cal.StoredEvent)

    async def events_patch(self, request: Request, caller: Caller) -> Response:
        return await self._changed(request, caller, replace=False)

    async def events_update(self, request: Request, caller: Caller) -> Response:
        return await self._changed(request, caller, replace=True)

    async def _changed(self, request: Request, caller: Caller, *, replace: bool) -> Response:
        """`events.patch` sets what the body names; `events.update` replaces the event with the body."""
        calendar = self._own(request, caller, needs="writer")
        self._send_updates(request)
        event, _ = self._visible(request.path_params["eventId"], calendar.id)
        if event.organizer.email.lower() != calendar.id.lower():
            raise cal.refused(
                403, "forbiddenForNonOrganizer", "Shared properties can only be changed by the organizer of the event."
            )
        self._matches(request, event)
        found = wire.read_object(await request.body())
        asked = self._write(found, event)
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
        if "sequence" in named and asked.sequence is not None:
            update["sequence"] = asked.sequence
        for name in cal.VERBATIM:
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
        self._channels.tell_calendars_later(calendars_of(event, changed))
        return _json(self._served(changed, calendar.id, caller), request, cal.StoredEvent)

    async def events_delete(self, request: Request, caller: Caller) -> Response:
        calendar = self._own(request, caller, needs="writer")
        self._send_updates(request)
        event, _ = self._visible(request.path_params["eventId"], calendar.id)
        if event.organizer.email.lower() != calendar.id.lower():
            raise cal.refused(
                403, "forbiddenForNonOrganizer", "Shared properties can only be changed by the organizer of the event."
            )
        self._matches(request, event)
        self._calendars.delete(event, actor=Actor.AGENT)
        self._channels.tell_calendars_later(calendars_of(event))
        return Response(status_code=204)

    async def events_watch(self, request: Request, caller: Caller) -> Response:
        """A channel on the calendar's events: told `sync` once opened, then `exists` whenever an event on it is
        created, changed or deleted, by the agent or by anyone else."""
        calendar = self._own(request, caller)
        asked, expires = self._channels.asked(await request.body(), CALENDAR_LIFETIME)
        spelled = request.path_params["calendarId"]
        channel = wire.CalendarChannel(
            id=asked.id,
            resourceId=hashlib.sha256(f"calendar events\x1f{calendar.id.lower()}".encode()).hexdigest()[:27],
            resourceUri=f"https://www.googleapis.com/calendar/v3/calendars/{quote(spelled, safe='')}/events?alt=json",
            address=asked.address,
            expiration=wire.rfc3339(expires),
            token=asked.token,
            email=caller.email,
            calendar=calendar.id,
        )
        return _json(self._channels.open(channel), request, wire.ChannelAnswer)

    async def channels_stop(self, request: Request, caller: Caller) -> Response:
        asked = wire.read_body(wire.ChannelStop, wire.read_object(await request.body()))
        self._channels.stop(asked, wire.CalendarChannel)
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
        show_deleted = _param(request, "showDeleted") == "true"
        candidates: list[tuple[cal.StoredEvent, WorldEvent | None]] = [
            (e, None) for e, _ in self._calendars.on(calendar.id)
        ]
        if show_deleted or updated_min is not None:
            # "When specified, entries deleted since this time will always be included regardless of showDeleted."
            candidates += [(e, gone) for e, gone in self._calendars.deletions() if self._on_calendar(e, calendar.id)]
        found: list[tuple[datetime, str, cal.StoredEvent | cal.CancelledEvent]] = []
        for event, removal in candidates:
            start = cal.instant(event.start, calendar.timeZone, "start")
            end = cal.instant(event.end, calendar.timeZone, "end")
            if not cal.overlaps(start, end, low, high):
                continue
            changed_at = wire.moment(event.updated) if removal is None else removal.sim_time
            if updated_min is not None and changed_at < updated_min:
                continue
            if words and not all(w in self._searchable(event) for w in words):
                continue
            served = (
                self._served(event, calendar.id, caller)
                if removal is None
                else self._cancelled(event, removal, calendar.id, caller, details=True)
            )
            found.append((start, wire.rfc3339(changed_at), served))
        if order == "updated":  # enum-lint: exempt Calendar's orderBy value
            found.sort(key=lambda row: row[1])
        else:
            found.sort(key=lambda row: (row[0], row[2].id))
        size = self._results(request)
        offset = self._offset(request)
        page = [e for _, _, e in found[offset : offset + size]]
        more = offset + size < len(found)
        seq, changed = self._last_change(calendar.id)
        answer = cal.EventList(
            etag=f'"{seq}"',
            summary=calendar.summary,
            updated=wire.rfc3339(changed),
            timeZone=calendar.timeZone,
            accessRole=self._role(calendar, caller) or "none",
            nextPageToken=wire.encode_page(offset + size) if more else None,
            nextSyncToken=None if more else f"s{seq}",
            items=page,
        )
        self._calendars.saw(record_ref(calendar_parent(calendar.id)), Operation.SEARCH)
        return _json(answer, request, cal.EventList)

    def _incremental(self, request: Request, caller: Caller, calendar: HeldCalendar, sync: str) -> Response:
        """Every event on the calendar changed since the sync token, a deleted one as `cancelled`, oldest change
        first. A token cannot be combined with the filters a full list takes, as Calendar's guide says."""
        for name in ("timeMin", "timeMax", "q", "orderBy", "updatedMin", "iCalUID", "privateExtendedProperty"):
            if name in request.query_params:
                raise cal.refused(400, "invalid", f"The {name} parameter cannot be used with a syncToken.")
        store = self._calendars.store
        if not sync.startswith("s") or not sync[1:].isdigit() or int(sync[1:]) > store.head():
            raise cal.refused(410, "fullSyncRequired", "Sync token is no longer valid, a full sync is required.")
        show_deleted = _param(request, "showDeleted") == "true"
        items: list[cal.StoredEvent | cal.CancelledEvent] = []
        for event_key, _, _ in self._changes(calendar.id, since=int(sync[1:])):
            current = self._calendars.event(event_key)
            gone = self._calendars.deleted(event_key)
            if current is not None:
                items.append(self._served(current[0], calendar.id, caller))
            elif gone is not None:
                items.append(self._cancelled(gone[0], gone[1], calendar.id, caller, details=show_deleted))
        last, changed = self._last_change(calendar.id)
        answer = cal.IncrementalList(
            etag=f'"{last}"',
            summary=calendar.summary,
            updated=wire.rfc3339(changed),
            timeZone=calendar.timeZone,
            accessRole=self._role(calendar, caller) or "none",
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
            mail.not_implemented(
                f"minutehand's Google Calendar does not implement {request.method} {request.url.path}"
            ),
            request,
        )

    def _refuse(self, method: Method) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            return _refused(
                mail.not_implemented(f"minutehand's Google Calendar does not serve calendar.{method.name}"), request
            )

        return endpoint

    def routes(self) -> list[Route]:
        handlers: dict[str, Handler] = {
            "acl.insert": self.acl_insert,
            "acl.list": self.acl_list,
            "calendarList.insert": self.calendar_list_insert,
            "calendarList.list": self.calendar_list,
            "calendars.get": self.calendars_get,
            "calendars.insert": self.calendars_insert,
            "channels.stop": self.channels_stop,
            "events.delete": self.events_delete,
            "events.get": self.events_get,
            "events.insert": self.events_insert,
            "events.list": self.events_list,
            "events.patch": self.events_patch,
            "events.update": self.events_update,
            "events.watch": self.events_watch,
            "freebusy.query": self.freebusy,
        }
        served = [Route(f"/calendar/v3/{m.path}", self.guarded(handlers[m.name], m), methods=[m.http]) for m in SERVED]
        refused = [Route(f"/calendar/v3/{m.path}", self._refuse(m), methods=[m.http]) for m in REFUSED]
        return [*served, *refused, Route("/calendar/v3/{rest:path}", self.not_built, methods=EVERY_METHOD)]
