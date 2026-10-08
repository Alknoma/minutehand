"""Google Calendar v3's own JSON.

An event is kept as one entity in its organizer's calendar (`StoredEvent`), in the shape Calendar serves it except
for what depends on whose calendar it is read from (`self` on the organizer, the creator and each attendee), which is
filled in when it is served.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, JsonValue, TypeAdapter

from minutehand.adapters.providers.google_workspace import gmail_wire as mail
from minutehand.adapters.providers.google_workspace import wire
from minutehand.domain.scenario import Model

RESPONSES = ("needsAction", "declined", "tentative", "accepted")
"""An attendee's `responseStatus`, as Calendar spells it."""
ANSWERS: dict[str, str] = {"accepted": "Yes", "tentative": "Maybe", "declined": "No"}
"""What an invitation offers its guest, by the `responseStatus` each sets, labelled as Google Calendar's invitation
labels them ("Going?  Yes  Maybe  No")."""
SEND_UPDATES = frozenset({"all", "externalOnly", "none"})
MAX_RESULTS = 2500
DEFAULT_RESULTS = 250
EVENT_ID_CHARACTERS = frozenset("0123456789abcdefghijklmnopqrstuv")
"""The base32hex alphabet a client-chosen event id is written in, as Calendar's reference allows."""


class EventTime(Model):
    date: str | None = None
    dateTime: str | None = None
    timeZone: str | None = None


class Attendee(Model):
    email: str
    displayName: str | None = None
    organizer: bool | None = None
    self: bool | None = None
    optional: bool | None = None
    responseStatus: str = "needsAction"
    comment: str | None = None
    additionalGuests: int | None = None
    resource: bool | None = None


class Actor(Model):
    email: str
    displayName: str | None = None
    self: bool | None = None


class StoredEvent(Model):
    """One event, as its organizer's calendar holds it."""

    kind: Literal["calendar#event"] = "calendar#event"
    etag: str
    id: str
    status: str = "confirmed"
    htmlLink: str
    created: str
    updated: str
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    colorId: str | None = None
    creator: Actor
    organizer: Actor
    start: EventTime
    end: EventTime
    transparency: str | None = None
    visibility: str | None = None
    iCalUID: str
    sequence: int = 0
    attendees: list[Attendee] | None = None
    guestsCanModify: bool | None = None
    guestsCanInviteOthers: bool | None = None
    guestsCanSeeOtherGuests: bool | None = None
    anyoneCanAddSelf: bool | None = None
    privateCopy: bool | None = None
    endTimeUnspecified: bool | None = None
    reminders: JsonValue = Field(default=None, description="As written, verbatim")
    extendedProperties: JsonValue = Field(default=None, description="As written, verbatim")
    source: JsonValue = Field(default=None, description="As written, verbatim")
    eventType: str = "default"


class CancelledEvent(Model):
    """What an incremental list serves for an event deleted since its sync token."""

    kind: Literal["calendar#event"] = "calendar#event"
    etag: str
    id: str
    status: Literal["cancelled"] = "cancelled"


class EventList(Model):
    kind: Literal["calendar#events"] = "calendar#events"
    etag: str
    summary: str
    updated: str
    timeZone: str
    accessRole: str = "owner"
    defaultReminders: list[JsonValue] = []
    nextPageToken: str | None = None
    nextSyncToken: str | None = None
    items: list[StoredEvent | CancelledEvent]


class IncrementalList(Model):
    kind: Literal["calendar#events"] = "calendar#events"
    etag: str
    summary: str
    updated: str
    timeZone: str
    accessRole: str = "owner"
    defaultReminders: list[JsonValue] = []
    nextPageToken: str | None = None
    nextSyncToken: str | None = None
    items: list[StoredEvent | CancelledEvent]


class CalendarListEntry(Model):
    kind: Literal["calendar#calendarListEntry"] = "calendar#calendarListEntry"
    etag: str
    id: str
    summary: str
    description: str | None = None
    location: str | None = None
    timeZone: str
    dataOwner: str | None = Field(default=None, description="Set only for secondary calendars")
    accessRole: str = "owner"
    primary: bool | None = Field(default=None, description="True on the caller's primary; the default is False")
    selected: bool = True
    defaultReminders: list[JsonValue] = []


class CalendarResource(Model):
    """`calendars.get` and `calendars.insert`'s answer: the calendar's own metadata, whoever reads it."""

    kind: Literal["calendar#calendar"] = "calendar#calendar"
    etag: str
    id: str
    summary: str
    description: str | None = None
    location: str | None = None
    timeZone: str
    dataOwner: str | None = Field(default=None, description="Set only for secondary calendars")


class CalendarWrite(Model):
    """What `calendars.insert` takes."""

    summary: str | None = None
    description: str | None = None
    location: str | None = None
    timeZone: str | None = None


class CalendarListWrite(Model):
    """What `calendarList.insert` takes: the calendar's id."""

    id: str | None = None


class AclScope(Model):
    type: str
    value: str | None = None


class AclRule(Model):
    kind: Literal["calendar#aclRule"] = "calendar#aclRule"
    etag: str
    id: str
    scope: AclScope
    role: str


class Acl(Model):
    kind: Literal["calendar#acl"] = "calendar#acl"
    etag: str
    nextPageToken: str | None = None
    nextSyncToken: str | None = None
    items: list[AclRule]


class AclWrite(Model):
    role: str | None = None
    scope: AclScope | None = None


class CalendarList(Model):
    kind: Literal["calendar#calendarList"] = "calendar#calendarList"
    etag: str
    nextPageToken: str | None = None
    nextSyncToken: str | None = None
    items: list[CalendarListEntry]


class BusyRange(Model):
    start: str
    end: str


class FreeBusyError(Model):
    domain: str
    reason: str


class FreeBusyCalendar(Model):
    errors: list[FreeBusyError] | None = None
    busy: list[BusyRange]


class FreeBusy(Model):
    kind: Literal["calendar#freeBusy"] = "calendar#freeBusy"
    timeMin: str
    timeMax: str
    calendars: dict[str, FreeBusyCalendar]


# --------------------------------------------------------------------------- requests


class AttendeeWrite(Model):
    email: str | None = None
    displayName: str | None = None
    optional: bool | None = None
    responseStatus: str | None = None
    comment: str | None = None
    additionalGuests: int | None = None
    resource: bool | None = None
    id: str | None = Field(default=None, description="Google's profile id: assigned, so what is sent is ignored")
    self: bool | None = Field(default=None, description="Read-only: ignored")
    organizer: bool | None = Field(default=None, description="Read-only: ignored")


VERBATIM = (
    "summary",
    "description",
    "location",
    "colorId",
    "transparency",
    "visibility",
    "guestsCanModify",
    "guestsCanInviteOthers",
    "guestsCanSeeOtherGuests",
    "anyoneCanAddSelf",
    "privateCopy",
    "endTimeUnspecified",
    "reminders",
    "extendedProperties",
    "source",
)
"""The event fields a write stores as sent and a read answers as stored."""
READ_ONLY = frozenset(
    {"kind", "etag", "htmlLink", "created", "updated", "creator", "organizer", "hangoutLink", "attendeesOmitted",
     "locked"}
)  # fmt: skip
"""Event fields the reference marks read-only: a body that carries them (an event read and sent back) has them
ignored."""
NOT_SERVED = (
    "conferenceData",
    "attachments",
    "gadget",
    "outOfOfficeProperties",
    "focusTimeProperties",
    "workingLocationProperties",
    "birthdayProperties",
    "eventLabelId",
    "recurringEventId",
    "originalStartTime",
)
"""Event fields this fake does not serve yet: a write that sets one is refused, naming it."""


class EventWrite(Model):
    """The part of an event a caller writes; the reference's other fields are `READ_ONLY` or `NOT_SERVED`."""

    id: str | None = None
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    colorId: str | None = None
    start: EventTime | None = None
    end: EventTime | None = None
    attendees: list[AttendeeWrite] | None = None
    transparency: str | None = None
    visibility: str | None = None
    status: str | None = None
    recurrence: list[str] | None = None
    guestsCanModify: bool | None = None
    guestsCanInviteOthers: bool | None = None
    guestsCanSeeOtherGuests: bool | None = None
    anyoneCanAddSelf: bool | None = None
    privateCopy: bool | None = None
    endTimeUnspecified: bool | None = None
    reminders: JsonValue = None
    extendedProperties: JsonValue = None
    source: JsonValue = None
    sequence: int | None = None
    iCalUID: str | None = None
    eventType: str | None = None


class FreeBusyItem(Model):
    id: str


class FreeBusyRequest(Model):
    timeMin: str | None = None
    timeMax: str | None = None
    timeZone: str | None = None
    items: list[FreeBusyItem] = []


# --------------------------------------------------------------------------- times


def refused(code: int, reason: str, message: str) -> wire.Refusal:
    return mail.refusal(code, reason, message)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise refused(400, "invalid", f"Invalid time zone definition: {name}") from error


def moment(spelled: str, where: str) -> datetime:
    """An RFC 3339 timestamp as a parameter carries it (`timeMin`, `timeMax`): its offset is required."""
    try:
        found = datetime.fromisoformat(spelled.replace("Z", "+00:00"))
    except ValueError as error:
        raise refused(400, "badRequest", f"Bad Request: invalid {where}") from error
    if found.tzinfo is None:
        raise refused(400, "badRequest", f"Bad Request: invalid {where}")
    return found


def instant(time: EventTime, fallback: str | None, where: str) -> datetime:
    """When an event's start or end is: its `dateTime` with its offset, or in its `timeZone` (then the calendar's)
    when it has none; an all-day `date` at midnight in that zone."""
    if time.dateTime is not None:
        try:
            found = datetime.fromisoformat(time.dateTime.replace("Z", "+00:00"))
        except ValueError as error:
            raise refused(400, "invalid", f"Invalid {where} time.") from error
        if found.tzinfo is not None:
            return found
        name = time.timeZone or fallback
        if not name:
            raise refused(400, "invalid", f"Missing time zone definition for {where} time.")
        return found.replace(tzinfo=zone(name))
    if time.date is not None:
        try:
            day = date.fromisoformat(time.date)
        except ValueError as error:
            raise refused(400, "invalid", f"Invalid {where} time.") from error
        return datetime(day.year, day.month, day.day, tzinfo=zone(time.timeZone or fallback or "UTC"))
    raise refused(400, "required", f"Missing {where} time.")


def normal(time: EventTime, fallback: str | None, where: str) -> EventTime:
    """A start or end as Calendar answers it: as written, but a `dateTime` written without an offset is given the
    offset of its `timeZone` (then the calendar's)."""
    if time.dateTime is None:
        if time.date is None:
            raise refused(400, "required", f"Missing {where} time.")
        return time
    found = instant(time, fallback, where)
    if datetime.fromisoformat(time.dateTime.replace("Z", "+00:00")).tzinfo is not None:
        return time
    return time.model_copy(update={"dateTime": found.isoformat()})


def utc(found: datetime) -> str:
    return wire.rfc3339(found).replace(".000Z", "Z")


def overlaps(start: datetime, end: datetime, low: datetime | None, high: datetime | None) -> bool:
    """Whether [start, end) meets [low, high): Calendar's `timeMin` bounds an event's end, `timeMax` its start."""
    return (low is None or end > low) and (high is None or start < high)


def merged(ranges: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    joined: list[tuple[datetime, datetime]] = []
    for start, end in sorted(ranges):
        if joined and start <= joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    return joined


Kept = Annotated[mail.StoredMail | StoredEvent, Field(discriminator="kind")]
"""A message entity of this provider: an email in a mailbox, or an event in a calendar (its invitation)."""
KEPT: TypeAdapter[mail.StoredMail | StoredEvent] = TypeAdapter(Kept)
