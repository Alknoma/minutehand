"""Graph for Outlook calendars: events made, read, listed, changed and deleted, the calendar view over a window,
free/busy (`getSchedule`), and attendees answering invitations, the agent through Graph and a person at their
reply's moment.

- **An event is one item**, in its organizer's calendar, seen with the same id from the calendar of every
  attendee who is a user of the tenant; `isOrganizer` and `responseStatus` are answered for the mailbox that reads
  it. Times are answered in UTC; a `timeZone` sent is UTC or an IANA name.
- **An invitation is the agent asking each attendee.** An event made with attendees sends a meeting request from
  the organizer (`eventMessageRequest`, in Sent Items and in each attendee's Inbox), in the event's own
  conversation, carrying Accept, Tentative and Decline (`MessageSnapshot.actions`). A change of subject, time or
  place sends a new request to every attendee and clears their answers; a new attendee gets one of their own.
- **A person answers an invitation** as a reply does: pressing Accept, Tentative or Decline on the request
  (`Scripted` `press`) sets their `responseStatus` on the event at that moment, as their change (actor PERSON), and
  sends the organizer a response message (`eventMessageResponse`) in the same conversation; a subscription on the
  organizer's mailbox or calendar is notified. A reply written back to a request instead is an email to the
  organizer. An answer to a request a newer one replaced, or to an event since deleted, changes nothing.
- **Query options** on a list or the view: `$top` (1 to 1000, 10 by default), `$skip`, `$select`; `$orderby` on
  `start/dateTime` or `end/dateTime`; `$filter` on `start/dateTime` and `end/dateTime` compared to a quoted date
  and time, and `subject eq`, joined by `and`. Anything else is not served (501).
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from starlette.requests import Request
from starlette.responses import Response

from minutehand.adapters.providers.microsoft import wire
from minutehand.adapters.providers.microsoft.common import GRAPH_JSON, GraphRefusal, bad_request, graph_caller, query
from minutehand.adapters.providers.microsoft.graph_mail import (
    Composed,
    Mail,
    address_of,
    as_html,
    mailbox_owner,
    outlook_id,
    plain,
    recipient_of,
)
from minutehand.adapters.providers.microsoft.state import (
    CALENDAR,
    GRAPH,
    RESPONSES,
    MicrosoftWorld,
    UserRecord,
    event_ref,
    graph_time,
    response_ref,
    user_ref,
)
from minutehand.adapters.providers.microsoft.subscriptions import calendar_watch, notify
from minutehand.domain.people import PersonReply
from minutehand.domain.world import (
    Actor,
    InteractionKind,
    InteractionSnapshot,
    MessageAction,
    Operation,
    RecordSnapshot,
)
from minutehand.ports.clock import Clock

PAGE_DEFAULT = 10
PAGE_MAX = 1000
EVENT_TYPE = "#Microsoft.Graph.Event"
CALENDAR_SEGMENTS = frozenset({"events", "calendar", "calendars", "calendarView", "findMeetingTimes"})
NEVER = "0001-01-01T00:00:00Z"

ANSWERS = {
    "accept": (wire.ResponseKind.ACCEPTED, wire.MeetingMessageType.ACCEPTED, "Accepted"),
    "tentativelyAccept": (wire.ResponseKind.TENTATIVE, wire.MeetingMessageType.TENTATIVE, "Tentative"),
    "decline": (wire.ResponseKind.DECLINED, wire.MeetingMessageType.DECLINED, "Declined"),
}
"""Each answer to an invitation, by the action Graph names it with: the attendee's response, the message the
organizer is sent, and the word its subject opens with."""

REQUEST_ACTIONS = [
    MessageAction(action_id="accept", label="Accept"),
    MessageAction(action_id="tentativelyAccept", label="Tentative"),
    MessageAction(action_id="decline", label="Decline"),
]

_STATUS_RANK = {"free": 0, "tentative": 1, "busy": 2, "oof": 3}
_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
}


def moment(sent: wire.SentDateTime) -> datetime:
    """A `dateTimeTimeZone` a caller sent, as a moment: its own offset when it carries one, else its `timeZone`
    (UTC or an IANA name)."""
    try:
        found = datetime.fromisoformat(sent.dateTime.replace("Z", "+00:00"))
    except ValueError as e:
        raise GraphRefusal(400, "ErrorInvalidRequest", f"'{sent.dateTime}' is not a valid dateTime.") from e
    if found.tzinfo is not None:
        return found.astimezone(UTC)
    zone = sent.timeZone or "UTC"
    if zone.upper() in ("UTC", "ETC/UTC", "Z"):
        return found.replace(tzinfo=UTC)
    try:
        return found.replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise NotImplementedError(f"the time zone {zone!r}: only UTC and IANA names are read") from e


def outlook_time(at: datetime) -> wire.DateTimeTimeZone:
    utc = at.astimezone(UTC)
    return wire.DateTimeTimeZone(
        dateTime=utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}0", timeZone="UTC"
    )


def described(stored: wire.StoredEvent) -> str:
    """What a meeting request says: its subject, when and where, and its body."""
    when = f"{stored.starts:%A %d %B %Y, %H:%M} to {stored.ends:%H:%M} UTC"
    place = f"\nWhere: {stored.event.location.displayName}" if stored.event.location.displayName else ""
    body = plain(stored.event.body)
    return f"When: {when}{place}" + (f"\n\n{body}" if body else "")


class Calendar:
    def __init__(self, world: MicrosoftWorld, clock: Clock, mail: Mail) -> None:
        self._world = world
        self._clock = clock
        self._mail = mail

    # ------------------------------------------------------------------ reading

    def attends(self, stored: wire.StoredEvent, user: UserRecord) -> wire.Attendee | None:
        mine = {address_of(user).lower(), user.user.userPrincipalName.lower()}
        return next((a for a in stored.event.attendees if a.emailAddress.address.lower() in mine), None)

    def visible(self, user: UserRecord) -> list[wire.StoredEvent]:
        """The events in a user's calendar: those they organize and those they are invited to."""
        return [e for e in self._world.events() if e.organizer_id == user.user.id or self.attends(e, user) is not None]

    def seen_by(self, stored: wire.StoredEvent, user: UserRecord) -> wire.Event:
        organizer = stored.organizer_id == user.user.id
        attendee = self.attends(stored, user)
        status = (
            wire.ResponseStatus(response=wire.ResponseKind.ORGANIZER, time=stored.event.createdDateTime)
            if organizer
            else attendee.status
            if attendee is not None
            else wire.ResponseStatus(response=wire.ResponseKind.NONE)
        )
        return stored.event.model_copy(update={"isOrganizer": organizer, "responseStatus": status})

    def _found(self, user: UserRecord, event: str) -> wire.StoredEvent:
        stored = self._world.event(event)
        if stored is None or (stored.organizer_id != user.user.id and self.attends(stored, user) is None):
            raise GraphRefusal(404, "ErrorItemNotFound", "The specified object was not found in the store.")
        return stored

    # ------------------------------------------------------------------ Graph

    async def answer(self, request: Request, parts: list[str]) -> Response:
        claims = graph_caller(request)
        owner, rest = mailbox_owner(self._world, claims, parts)
        method = request.method
        if rest[:1] == ["calendar"] and rest[1:2] in (["events"], ["calendarView"], ["getSchedule"]):
            rest = rest[1:]
        if rest in (["calendar"], ["calendars"]) and method == "GET":
            return self._calendars(owner, single=rest == ["calendar"])
        if rest == ["getSchedule"] and method == "POST":
            return await self._schedule(request, owner)
        if rest == ["calendarView"] and method == "GET":
            return self._view(request, owner)
        if rest == ["events"] and method == "GET":
            return self._listed(request, owner, self.visible(owner), "events")
        if rest == ["events"] and method == "POST":
            return await self._create(request, owner)
        if len(rest) >= 2 and rest[0] == "events":
            stored = self._found(owner, rest[1])
            if len(rest) == 2 and method == "GET":
                self._world.saw(event_ref(stored.event.id), Operation.READ)
                return self._one(request, owner, stored)
            if len(rest) == 2 and method == "PATCH":
                return await self._patch(request, owner, stored)
            if len(rest) == 2 and method == "DELETE":  # enum-lint: exempt HTTP's method name
                return self._delete(owner, stored)
            if len(rest) == 3 and rest[2] in ANSWERS and method == "POST":
                try:
                    asked = wire.read(wire.EventResponseRequest, await request.body())
                except wire.Unreadable as e:
                    raise bad_request(e.message) from e
                if stored.organizer_id == owner.user.id:
                    raise GraphRefusal(
                        400,
                        "ErrorInvalidRequest",
                        "Your request can't be completed. You can't respond to your own meeting.",
                    )
                await self.respond(
                    stored, owner, rest[2], actor=Actor.AGENT, send=asked.sendResponse, comment=asked.comment
                )
                return Response(status_code=202)
        raise NotImplementedError(f"{method} /{'/'.join(parts)}")

    def _one(self, request: Request, owner: UserRecord, stored: wire.StoredEvent, status: int = 200) -> Response:
        fields = [f for f in (query(request, "$select") or "").split(",") if f] or None
        context = f"{GRAPH}/$metadata#users('{owner.user.id}')/events/$entity"
        body = wire.select(wire.with_context(wire.dump(self.seen_by(stored, owner)), context), fields)
        return Response(body, status_code=status, media_type=GRAPH_JSON)

    def _calendars(self, owner: UserRecord, *, single: bool) -> Response:
        calendar = wire.CalendarResource(
            id=outlook_id(owner.user.id, "calendar"),
            owner=wire.EmailAddress(name=owner.user.displayName, address=address_of(owner)),
        )
        if single:
            context = f"{GRAPH}/$metadata#users('{owner.user.id}')/calendar/$entity"
            return Response(wire.with_context(wire.dump(calendar), context), media_type=GRAPH_JSON)
        page = wire.Page[wire.CalendarResource](
            context=f"{GRAPH}/$metadata#users('{owner.user.id}')/calendars", value=[calendar]
        )
        return Response(wire.dump(page), media_type=GRAPH_JSON)

    # ------------------------------------------------------------------ listing

    @staticmethod
    def _filtered(events: list[wire.StoredEvent], text: str | None) -> list[wire.StoredEvent]:
        if not text:
            return events
        if re.search(r"\s+or\s+|\bnot\b", text, flags=re.IGNORECASE):
            raise NotImplementedError(f"$filter with 'or' or 'not' on events: {text}")
        found = events
        for part in re.split(r"\s+and\s+(?=(?:[^']*'[^']*')*[^']*$)", text.strip(), flags=re.IGNORECASE):
            when = re.fullmatch(r"\s*(start|end)/dateTime\s+(eq|ne|gt|ge|lt|le)\s+'([^']+)'\s*", part)
            subject = re.fullmatch(r"\s*subject\s+eq\s+'((?:[^']|'')*)'\s*", part)
            if when is not None:
                side, compare = when.group(1), _COMPARE[when.group(2)]
                at = moment(wire.SentDateTime(dateTime=when.group(3)))
                starting = side == "start"  # enum-lint: exempt Graph's start/dateTime property
                found = [e for e in found if compare(e.starts if starting else e.ends, at)]
            elif subject is not None:
                wanted = subject.group(1).replace("''", "'")
                found = [e for e in found if e.event.subject == wanted]
            else:
                raise NotImplementedError(f"$filter clause on events: {part.strip()}")
        return found

    def _listed(self, request: Request, owner: UserRecord, events: list[wire.StoredEvent], where: str) -> Response:
        for option in ("$search", "$expand", "$count", "$skiptoken"):
            if option in request.query_params:
                raise NotImplementedError(f"{option} on events")
        found = self._filtered(events, query(request, "$filter"))
        order = (query(request, "$orderby") or "start/dateTime").strip()
        side, _, direction = order.partition(" ")
        if side not in ("start/dateTime", "end/dateTime") or direction.lower() not in ("", "asc", "desc"):
            raise NotImplementedError(f"$orderby on events: {order}")
        found = sorted(
            found, key=lambda e: e.starts if side == "start/dateTime" else e.ends, reverse=direction.lower() == "desc"
        )
        top, skip = query(request, "$top"), query(request, "$skip")
        if (top is not None and (not top.isdigit() or not 1 <= int(top) <= PAGE_MAX)) or (
            skip is not None and not skip.isdigit()
        ):
            raise bad_request(f"Invalid paging: $top must be 1 to {PAGE_MAX} and $skip a whole number.")
        size, offset = int(top) if top else PAGE_DEFAULT, int(skip) if skip else 0
        page = [self.seen_by(e, owner) for e in found[offset : offset + size]]
        following = None
        if offset + size < len(found):
            kept = "&".join(
                f"{k}={request.query_params[k]}"
                for k in ("startDateTime", "endDateTime", "$filter", "$orderby", "$select")
                if k in request.query_params
            )
            following = (
                f"{GRAPH}/users/{owner.user.id}/{where}?{kept + '&' if kept else ''}$top={size}&$skip={offset + size}"
            )
        self._world.saw(user_ref(owner.user.id), Operation.SEARCH)
        body = wire.dump(
            wire.Page[wire.Event](
                context=f"{GRAPH}/$metadata#users('{owner.user.id}')/{where}", value=page, next_link=following
            )
        )
        fields = [f for f in (query(request, "$select") or "").split(",") if f] or None
        return Response(wire.select_page(body, fields), media_type=GRAPH_JSON)

    def _window(self, start: str | None, end: str | None) -> tuple[datetime, datetime]:
        if not start or not end:
            raise GraphRefusal(
                400,
                "ErrorInvalidParameter",
                "This request requires a time window specified by the query string parameters StartDateTime and "
                "EndDateTime.",
            )
        return moment(wire.SentDateTime(dateTime=start)), moment(wire.SentDateTime(dateTime=end))

    def _view(self, request: Request, owner: UserRecord) -> Response:
        start, end = self._window(query(request, "startDateTime"), query(request, "endDateTime"))
        inside = [e for e in self.visible(owner) if e.starts < end and e.ends > start]
        return self._listed(request, owner, inside, "calendarView")

    # ------------------------------------------------------------------ making and changing

    def _attendees(self, sent: list[wire.SentAttendee], organizer: UserRecord) -> list[wire.Attendee]:
        found: list[wire.Attendee] = []
        seen: set[str] = {address_of(organizer).lower()}
        for a in sent:
            address = a.emailAddress.address
            if not address or "@" not in address:
                raise GraphRefusal(400, "ErrorInvalidRecipients", f"An attendee's address is not valid: '{address}'.")
            canonical = self._mail.canonical(address)
            if canonical.lower() in seen:
                continue
            seen.add(canonical.lower())
            user = self._world.user_by(canonical)
            found.append(
                wire.Attendee(
                    type=a.type,
                    status=wire.ResponseStatus(response=wire.ResponseKind.NONE),
                    emailAddress=wire.EmailAddress(
                        name=a.emailAddress.name or (user.user.displayName if user is not None else None),
                        address=canonical,
                    ),
                )
            )
        return found

    @staticmethod
    def _body(sent: wire.SentBody | None) -> wire.ItemBody:
        if sent is None:
            return wire.ItemBody(contentType="html", content="")
        kind = sent.contentType.lower()
        if kind not in ("text", "html"):
            raise bad_request(f"'{sent.contentType}' is not a body content type: text or html.")
        return as_html(wire.ItemBody(contentType=kind, content=sent.content))  # type: ignore[arg-type]

    async def _create(self, request: Request, owner: UserRecord) -> Response:
        try:
            asked = wire.read(wire.EventRequest, await request.body())
        except wire.Unreadable as e:
            raise bad_request(e.message) from e
        if asked.transactionId:
            again = next(
                (
                    e
                    for e in self._world.events()
                    if e.organizer_id == owner.user.id and e.event.transactionId == asked.transactionId
                ),
                None,
            )
            if again is not None:
                return self._one(request, owner, again, 201)
        if asked.start is None or asked.end is None:
            raise GraphRefusal(400, "ErrorInvalidRequest", "An event needs both 'start' and 'end'.")
        starts, ends = moment(asked.start), moment(asked.end)
        if ends < starts:
            raise GraphRefusal(400, "ErrorInvalidRequest", "The event's end is before its start.")
        stored = self.make(
            owner,
            subject=asked.subject or "",
            body=self._body(asked.body),
            starts=starts,
            ends=ends,
            location=asked.location.displayName if asked.location is not None else "",
            attendees=self._attendees(asked.attendees or [], owner),
            actor=Actor.AGENT,
            transaction=asked.transactionId,
            online=bool(asked.isOnlineMeeting),
        )
        if stored.event.attendees:
            stored = await self._invite(stored, owner, stored.event.attendees)
        await self._notify(stored, "created")
        return self._one(request, owner, stored, 201)

    def make(
        self,
        organizer: UserRecord,
        *,
        subject: str,
        body: wire.ItemBody,
        starts: datetime,
        ends: datetime,
        location: str,
        attendees: list[wire.Attendee],
        actor: Actor,
        transaction: str | None = None,
        online: bool = False,
        seeded: str | None = None,
    ) -> wire.StoredEvent:
        """Write a new event in `organizer`'s calendar, inviting nobody yet. `seeded` names an event the scenario
        seeds, so its id is derived from what it is rather than where the log stands."""
        now = graph_time(self._clock.now())
        made = ("seeded event", seeded) if seeded is not None else ("event", str(self._world.next_seq()))
        event_id = outlook_id(organizer.user.id, *made)
        stored = wire.StoredEvent(
            event=wire.Event(
                id=event_id,
                createdDateTime=now,
                lastModifiedDateTime=now,
                changeKey=outlook_id(event_id, now),
                iCalUId="040000008200E00074C5B7101A82E008" + outlook_id(event_id, "ical").removeprefix("AAMkA")[:32],
                transactionId=transaction,
                subject=subject,
                bodyPreview=plain(body)[:255],
                body=body,
                start=outlook_time(starts),
                end=outlook_time(ends),
                location=wire.Location(displayName=location),
                attendees=attendees,
                organizer=recipient_of(organizer),
                isOrganizer=True,
                responseStatus=wire.ResponseStatus(response=wire.ResponseKind.ORGANIZER, time=now),
                isOnlineMeeting=online,
                webLink=f"https://outlook.office365.com/owa/?itemid={event_id}&exvsurl=1&path=/calendar/item",
            ),
            organizer_id=organizer.user.id,
            starts=starts,
            ends=ends,
            conversation=outlook_id(event_id, "conversation"),
        )
        self._world.write_event(
            stored, operation=Operation.CREATE, actor=actor, after=RecordSnapshot(resource="event", text=subject)
        )
        return stored

    async def _invite(
        self, stored: wire.StoredEvent, organizer: UserRecord, attendees: list[wire.Attendee]
    ) -> wire.StoredEvent:
        """Send a meeting request for the event to `attendees`, from its organizer, and keep it as the event's
        current request."""
        request = await self._mail.send(
            Composed(
                sender=organizer,
                subject=stored.event.subject,
                body=wire.ItemBody(contentType="text", content=described(stored)),
                to=[wire.Recipient(emailAddress=a.emailAddress) for a in attendees],
                conversation=stored.conversation,
                meeting=wire.MeetingMessageType.REQUEST,
                event=stored.event.id,
            ),
            actor=Actor.AGENT,
            actions=REQUEST_ACTIONS,
        )
        sent = stored.model_copy(update={"request": request.message.id})
        self._world.write_event(sent, operation=Operation.UPDATE, actor=Actor.AGENT, after=None)
        return sent

    async def _patch(self, request: Request, owner: UserRecord, stored: wire.StoredEvent) -> Response:
        try:
            asked = wire.read(wire.EventRequest, await request.body())
        except wire.Unreadable as e:
            raise bad_request(e.message) from e
        extra = sorted(asked.model_extra or {})
        if extra or asked.transactionId is not None:
            raise NotImplementedError(
                f"PATCH of {', '.join([*extra, *(['transactionId'] if asked.transactionId else [])])} on an event"
            )
        if stored.organizer_id != owner.user.id:
            raise NotImplementedError(
                "an attendee's own changes to an event: an event here is its organizer's one item"
            )
        starts = moment(asked.start) if asked.start is not None else stored.starts
        ends = moment(asked.end) if asked.end is not None else stored.ends
        if ends < starts:
            raise GraphRefusal(400, "ErrorInvalidRequest", "The event's end is before its start.")
        event = stored.event
        subject = asked.subject if asked.subject is not None else event.subject
        place = asked.location.displayName if asked.location is not None else event.location.displayName
        moved = (subject, starts, ends, place) != (
            event.subject,
            stored.starts,
            stored.ends,
            event.location.displayName,
        )
        attendees = event.attendees
        added: list[wire.Attendee] = []
        if asked.attendees is not None:
            kept = {a.emailAddress.address.lower(): a for a in event.attendees}
            attendees = [
                kept[a.emailAddress.address.lower()] if a.emailAddress.address.lower() in kept else a
                for a in self._attendees(asked.attendees, owner)
            ]
            added = [a for a in attendees if a.emailAddress.address.lower() not in kept]
        if moved:
            attendees = [
                a.model_copy(update={"status": wire.ResponseStatus(response=wire.ResponseKind.NONE)}) for a in attendees
            ]
        now = graph_time(self._clock.now())
        body = self._body(asked.body) if asked.body is not None else event.body
        changed = stored.model_copy(
            update={
                "event": event.model_copy(
                    update={
                        "subject": subject,
                        "body": body,
                        "bodyPreview": plain(body)[:255],
                        "start": outlook_time(starts),
                        "end": outlook_time(ends),
                        "location": wire.Location(displayName=place),
                        "attendees": attendees,
                        "lastModifiedDateTime": now,
                        "changeKey": outlook_id(event.id, now),
                    }
                ),
                "starts": starts,
                "ends": ends,
            }
        )
        self._world.write_event(
            changed, operation=Operation.UPDATE, actor=Actor.AGENT, after=RecordSnapshot(resource="event", text=subject)
        )
        if moved and attendees:
            changed = await self._invite(changed, owner, attendees)
        elif added:
            changed = await self._invite(changed, owner, added)
        await self._notify(changed, "updated")
        return self._one(request, owner, changed)

    def _delete(self, owner: UserRecord, stored: wire.StoredEvent) -> Response:
        if stored.organizer_id != owner.user.id:
            raise NotImplementedError(
                "an attendee removing an event from their own calendar: an event here is its organizer's one item"
            )
        self._world.remove(
            event_ref(stored.event.id),
            actor=Actor.AGENT,
            parent=CALENDAR.format(user=stored.organizer_id),
            before=RecordSnapshot(resource="event", text=stored.event.subject),
        )
        return Response(status_code=204)

    async def _notify(self, stored: wire.StoredEvent, change: str) -> None:
        watchers = {
            stored.organizer_id,
            *(u.user.id for a in stored.event.attendees if (u := self._world.user_by(a.emailAddress.address))),
        }
        for user in watchers:
            await notify(
                self._world,
                self._clock,
                calendar_watch(user),
                change=change,
                odata_type=EVENT_TYPE,
                resource=f"Users/{user}/Events/{stored.event.id}",
                item=stored.event.id,
            )

    # ------------------------------------------------------------------ answering an invitation

    async def respond(
        self, stored: wire.StoredEvent, user: UserRecord, action: str, *, actor: Actor, send: bool, comment: str
    ) -> None:
        """`user` answers the event's invitation: their `responseStatus` set, and the organizer sent a response
        message in the event's conversation when `send`."""
        response, meeting, word = ANSWERS[action]
        now = graph_time(self._clock.now())
        mine = address_of(user).lower()
        attendees = [
            a.model_copy(update={"status": wire.ResponseStatus(response=response, time=now)})
            if a.emailAddress.address.lower() in (mine, user.user.userPrincipalName.lower())
            else a
            for a in stored.event.attendees
        ]
        changed = stored.model_copy(
            update={
                "event": stored.event.model_copy(
                    update={"attendees": attendees, "changeKey": outlook_id(stored.event.id, now, mine)}
                )
            }
        )
        self._world.write_event(
            changed,
            operation=Operation.UPDATE,
            actor=actor,
            after=RecordSnapshot(resource="event", text=f"{address_of(user)} {response.value} {stored.event.subject}"),
        )
        organizer = self._world.user(stored.organizer_id)
        if send and organizer is not None:
            await self._mail.send(
                Composed(
                    sender=user,
                    subject=f"{word}: {stored.event.subject}",
                    body=wire.ItemBody(contentType="text", content=comment),
                    to=[recipient_of(organizer)],
                    conversation=stored.conversation,
                    meeting=meeting,
                    event=stored.event.id,
                ),
                actor=actor,
                answerable=False,
            )
        await self._notify(changed, "updated")

    async def person_responds(self, reply: PersonReply) -> None:
        """A person presses Accept, Tentative or Decline on a meeting request: they answer the event it invites to,
        unless a newer request replaced it or the event is gone."""
        assert reply.press is not None
        _, request = self._mail.located(reply.in_reply_to.external_id)
        if request.event is None or request.message.meetingMessageType is not wire.MeetingMessageType.REQUEST:
            raise ValueError(f"the message {request.message.id} is no meeting request: an email carries no controls")
        action = reply.press.action_id
        if action not in ANSWERS:
            raise LookupError(f"a meeting request has no button {action!r} ({reply.press.label!r})")
        user = self._world.person(reply.person)
        if user is None:
            raise LookupError(f"{reply.person} is not a user of the tenant")
        stored = self._world.event(request.event)
        if stored is None or stored.request != request.message.id or self.attends(stored, user) is None:
            return
        self._world.write(
            response_ref(stored.event.id, self._world.next_seq()),
            wire.ResponseStatus(response=ANSWERS[action][0], time=graph_time(self._clock.now())),
            operation=Operation.CREATE,
            actor=Actor.PERSON,
            parent=RESPONSES.format(event=stored.event.id),
            after=InteractionSnapshot(
                interaction=InteractionKind.PRESS,
                person=reply.person,
                on=reply.in_reply_to,
                action_id=action,
                label=reply.press.label,
            ),
        )
        await self.respond(stored, user, action, actor=Actor.PERSON, send=True, comment="")

    # ------------------------------------------------------------------ free/busy

    def _busy(self, user: UserRecord, start: datetime, end: datetime) -> list[wire.ScheduleItem]:
        items: list[wire.ScheduleItem] = []
        for stored in self.visible(user):
            if not (stored.starts < end and stored.ends > start):
                continue
            attendee = self.attends(stored, user)
            status: Literal["tentative", "busy"]
            if stored.organizer_id != user.user.id and attendee is not None:
                if attendee.status.response is wire.ResponseKind.DECLINED:
                    continue
                status = "busy" if attendee.status.response is wire.ResponseKind.ACCEPTED else "tentative"
            else:
                status = "busy"
            items.append(
                wire.ScheduleItem(
                    status=status,
                    subject=stored.event.subject,
                    location=stored.event.location.displayName or None,
                    start=outlook_time(stored.starts),
                    end=outlook_time(stored.ends),
                )
            )
        away = self._world.away(user, start)
        if away is not None and away[0] < end and away[1] > start:
            items.append(
                wire.ScheduleItem(
                    status="oof", subject=away[2].reason, start=outlook_time(away[0]), end=outlook_time(away[1])
                )
            )
        return sorted(items, key=lambda i: i.start.dateTime)

    async def _schedule(self, request: Request, owner: UserRecord) -> Response:
        try:
            asked = wire.read(wire.ScheduleRequest, await request.body())
        except wire.Unreadable as e:
            raise bad_request(e.message) from e
        start, end = moment(asked.startTime), moment(asked.endTime)
        if end <= start:
            raise GraphRefusal(400, "ErrorInvalidRequest", "The schedule's endTime must be after its startTime.")
        if not 5 <= asked.availabilityViewInterval <= 1440:
            raise GraphRefusal(400, "ErrorInvalidRequest", "availabilityViewInterval must be from 5 to 1440 minutes.")
        step = timedelta(minutes=asked.availabilityViewInterval)
        found: list[wire.ScheduleInformation] = []
        for address in asked.schedules:
            user = self._world.user_by(address)
            if user is None:
                found.append(
                    wire.ScheduleInformation(
                        scheduleId=address,
                        error=wire.FreeBusyError(
                            message="The specified recipient could not be found.",
                            responseCode="ErrorMailRecipientNotFound",
                        ),
                    )
                )
                continue
            items = self._busy(user, start, end)
            view = ""
            slot = start
            while slot < end:
                over = [
                    i
                    for i in items
                    if moment(wire.SentDateTime(dateTime=i.start.dateTime)) < slot + step
                    and moment(wire.SentDateTime(dateTime=i.end.dateTime)) > slot
                ]
                view += str(max((_STATUS_RANK[i.status] for i in over), default=0))
                slot += step
            found.append(wire.ScheduleInformation(scheduleId=address, availabilityView=view, scheduleItems=items))
            self._world.saw(user_ref(user.user.id), Operation.READ)
        page = wire.Page[wire.ScheduleInformation](
            context=f"{GRAPH}/$metadata#Collection(microsoft.graph.scheduleInformation)", value=found
        )
        del owner
        return Response(wire.dump(page), media_type=GRAPH_JSON)
