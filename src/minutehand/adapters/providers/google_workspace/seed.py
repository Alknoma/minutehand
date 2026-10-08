"""The Google Workspace a scenario starts with: everyone's My Drive, mailbox and calendar, the shared drives, the
documents, who may see them, the emails and events already there, how the agent signs in, and the faults the
scenario declared.

- Every person is a user with a My Drive of their own; so is every sign-in that is not a person's (a service
  account), named by its credential.
- A `SharedSpace` is a shared drive whose members are granted on its root.
- Each `SeededDocument` is a file owned by its owner (the scenario's owner when it names none), in its owner's
  My Drive or its shared drive, under its folder path, made the first time a path names it. A DOCUMENT is a
  Google Doc whose text is read as Markdown; a SPREADSHEET a Google Sheet of its rows; a PRESENTATION a Google
  Slides deck; a FILE an uploaded file of its `mime_type`. It was last changed `modified_before_start`
  before the scenario starts, by `modified_by`.
- A `SignIn` for this provider signs in as its person (or, naming none, as a service account of that name). No
  credential is enforced: any other credential, or none, acts as the seed's `unknown_credentials_act_as`, by
  default the scenario's owner.
- Every person has a mailbox at their address and a primary calendar in their working hours' time zone (UTC
  without).
- Its own seed (`WorkspaceSeed`, the scenario's `ProviderSeed` for `google_workspace`) declares the emails already
  sent (each in its sender's and its recipients' mailboxes, threaded by what it answers), the events already on
  calendars, and the faults: the next `times` calls of a Google operation, from `after` on, refused with a
  `wire.FaultKind`. A person the seed names by a key that is nobody's, or an email it answers that is not seeded
  before it, is refused before anything is written.
- A `SeededChannel` on this provider is refused: Gmail has no channel or chat to hold it.

Everything is written as actor SCENARIO.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from typing import Literal

from pydantic import Field

from minutehand.adapters.providers.google_workspace import (
    calendar_wire,
    calendars,
    docs,
    gmail,
    gmail_wire,
    slides,
    state,
    wire,
)
from minutehand.adapters.providers.google_workspace.app import OPERATIONS
from minutehand.adapters.providers.google_workspace.calendars import CalendarRecord, CalendarWorld, html_link
from minutehand.adapters.providers.google_workspace.gmail import MailWorld
from minutehand.adapters.providers.google_workspace.manifest import MANIFEST
from minutehand.adapters.providers.google_workspace.state import (
    ROLES,
    ROOT_NAME,
    DriveWorld,
    ensure_folder,
    folder_file,
    grant,
)
from minutehand.domain.scenario import DocumentKind, Model, Person, Scenario, SeededDocument
from minutehand.domain.world import Actor, Operation
from minutehand.ports.store import Store


class FaultSeed(Model):
    """The next `times` calls of `operation`, from `after` on, are refused with `kind`."""

    operation: str = Field(min_length=1, description="Google's own name for the call, e.g. 'documents.get'")
    kind: wire.FaultKind
    times: int = Field(default=1, ge=1)
    after: timedelta = Field(default=timedelta(0), ge=timedelta(0), description="Offset from the scenario's start")


class SeededEmail(Model):
    """An email sent before the scenario starts. A name without `@` is a `Person.key`; one with it an address,
    which may be outside the world."""

    key: str | None = Field(default=None, description="How a later email names this one in `in_reply_to`")
    sender: str = Field(min_length=1)
    to: list[str] = Field(min_length=1)
    cc: list[str] = []
    subject: str = ""
    text: str
    ago: timedelta = Field(gt=timedelta(0), description="How long before the scenario starts it was sent")
    in_reply_to: str | None = Field(default=None, description="The `key` of an earlier seeded email it answers")
    unread: bool = Field(default=True, description="Whether its recipients have yet to read it")


class SeededAttendee(Model):
    person: str = Field(min_length=1, description="A Person.key, or an address outside the world")
    response: Literal["needsAction", "declined", "tentative", "accepted"] = "needsAction"


class SeededEvent(Model):
    """An event on its organizer's calendar when the scenario starts."""

    calendar: str = Field(description="Person.key of its organizer, on whose calendar it is")
    summary: str = Field(min_length=1)
    starts: timedelta = Field(description="Offset from the scenario's start; negative for one already begun or past")
    lasts: timedelta = Field(gt=timedelta(0))
    attendees: list[SeededAttendee] = []
    description: str | None = None
    location: str | None = None
    transparent: bool = Field(default=False, description="Free rather than busy, for free/busy")


class WorkspaceSeed(Model):
    """What only Google Workspace seeds, as the body of the scenario's `ProviderSeed` for `google_workspace`."""

    faults: list[FaultSeed] = []
    emails: list[SeededEmail] = []
    events: list[SeededEvent] = []
    unknown_credentials_act_as: str | None = Field(
        default=None,
        description="Key of the person every credential the world does not hold, or none at all, acts as (an "
        "unseeded or expired refresh token, a service account nobody declared, an authorization code); None is the "
        "scenario's owner",
    )


def workspace_seed(scenario: Scenario) -> WorkspaceSeed:
    found = scenario.provider_seed(MANIFEST.key)
    return WorkspaceSeed() if found is None else WorkspaceSeed.model_validate_json(found.body)


def user_of(person: Person) -> wire.DriveUser:
    return wire.DriveUser(
        displayName=person.name, emailAddress=person.email, permissionId=state.permission_id(person.email)
    )


def _robot(credential: str) -> wire.DriveUser:
    return wire.DriveUser(
        displayName=credential.split("@", 1)[0], emailAddress=credential, permissionId=state.permission_id(credential)
    )


def _seed_document(
    drive: DriveWorld,
    document: SeededDocument,
    *,
    root: wire.StoredFile,
    owner: wire.DriveUser,
    modifier: wire.DriveUser,
    stamp: str,
    changed: str,
    position: int,
) -> wire.StoredFile:
    ordinal = position * (state.SEEDED_FOLDER_DEPTH + 1)
    parent = ensure_folder(drive, root, document.folder, owner, stamp, Actor.SCENARIO, seeded=ordinal)
    blob = (
        drive.keep_blob(document.text.encode("utf-8"), actor=Actor.SCENARIO)
        if document.kind is DocumentKind.FILE
        else None
    )
    file_id = state.seeded_file_id(ordinal + state.SEEDED_FOLDER_DEPTH, parent.file.id, document.title)
    content: wire.Content | None
    if document.kind is DocumentKind.DOCUMENT:
        mime, content = wire.DOCUMENT, docs.from_markdown(file_id, document.text)
    elif document.kind is DocumentKind.SPREADSHEET:
        mime, content = wire.SPREADSHEET, wire.Sheet(rows=document.rows)
    elif document.kind is DocumentKind.PRESENTATION:
        mime, content = wire.PRESENTATION, slides.deck_from_text(file_id, document.text)
    else:
        mime, content = document.mime_type or "text/plain", blob
    made = wire.StoredFile(
        file=wire.DriveFile(
            id=file_id,
            name=document.title,
            mimeType=mime,
            parents=[parent.file.id],
            owners=[owner] if root.file.driveId is None else None,
            createdTime=changed,
            modifiedTime=changed,
            version=str(state.SEEDED_VERSION),
            webViewLink=wire.web_view_link(file_id, mime),
            size=str(blob.size) if blob is not None else None,
            md5Checksum=blob.md5 if blob is not None else None,
            driveId=root.file.driveId,
            lastModifyingUser=modifier,
            shared=True if document.shared_with else None,
        ),
        content=content,
    )
    drive.write_file(made, operation=Operation.CREATE, actor=Actor.SCENARIO)
    return made


def seed(scenario: Scenario, world: Store) -> None:
    own = workspace_seed(scenario)
    _check(own, scenario)
    drive = DriveWorld(world)
    start = scenario.starts_at
    stamp = wire.rfc3339(start)
    people = {p.key: p for p in scenario.people}
    users = {p.key: user_of(p) for p in scenario.people}
    sign_ins = [s for s in scenario.sign_ins if s.provider == MANIFEST.key]
    robots = [_robot(s.credential) for s in sign_ins if s.person is None]

    for key, user in users.items():
        drive.keep_person(key, user)
    for user in [*users.values(), *robots]:
        assert user.emailAddress is not None
        drive.write_user(user, actor=Actor.SCENARIO)
        root = folder_file(
            state.root_id(user.emailAddress),
            ROOT_NAME,
            parent=None,
            owner=user,
            drive_id=None,
            stamp=stamp,
            version=state.SEEDED_VERSION,
        )
        drive.write_file(root, operation=Operation.CREATE, actor=Actor.SCENARIO)

    default = people[own.unknown_credentials_act_as or scenario.owner]
    drive.keep_credential(state.ANY_CREDENTIAL, wire.Credential(email=default.email), operation=Operation.CREATE)
    for sign_in in sign_ins:
        email = people[sign_in.person].email if sign_in.person is not None else sign_in.credential
        credential = wire.Credential(email=email, service_account="@" in sign_in.credential)
        drive.keep_credential(state.secret_digest(sign_in.credential), credential, operation=Operation.CREATE)

    spaces: dict[str, wire.StoredFile] = {}
    for position, space in enumerate(scenario.spaces):
        if space.provider != MANIFEST.key:
            continue
        drive_id = state.seeded_drive_id(position, space.name)
        drive.write_drive(wire.SharedDrive(id=drive_id, name=space.name, createdTime=stamp), actor=Actor.SCENARIO)
        root = folder_file(
            drive_id, space.name, parent=None, owner=None, drive_id=drive_id, stamp=stamp, version=state.SEEDED_VERSION
        )
        drive.write_file(root, operation=Operation.CREATE, actor=Actor.SCENARIO)
        for member in space.members:
            grant(drive, root, users[member.person], ROLES[member.role], actor=Actor.SCENARIO)
        spaces[space.name] = root

    for position, document in enumerate(scenario.documents):
        if document.provider != MANIFEST.key:
            continue
        owner = users[document.owner or scenario.owner]
        assert owner.emailAddress is not None
        root = spaces[document.space] if document.space is not None else drive.file(state.root_id(owner.emailAddress))
        assert root is not None
        made = _seed_document(
            drive,
            document,
            root=root,
            owner=owner,
            modifier=users[document.modified_by] if document.modified_by else owner,
            stamp=stamp,
            changed=wire.rfc3339(start - document.modified_before_start),
            position=position,
        )
        drive.keep_seeded(document.title, made.file.id)
        for access in document.shared_with:
            grant(drive, made, users[access.person], ROLES[access.role], actor=Actor.SCENARIO)

    calendars = CalendarWorld(world)
    for person in scenario.people:
        zone = person.working_hours.timezone if person.working_hours is not None else "UTC"
        calendars.keep_calendar(CalendarRecord(id=person.email, timeZone=zone), actor=Actor.SCENARIO)
    _seed_emails(own.emails, scenario, MailWorld(world))
    _seed_events(own.events, scenario, calendars)
    write_faults(drive, own.faults, start)


def _check(own: WorkspaceSeed, scenario: Scenario) -> None:
    """Every name the seed gives must name something, and nothing the shared seed puts here may be dropped, checked
    before anything is written."""
    people = {p.key: p for p in scenario.people}
    if own.unknown_credentials_act_as is not None and own.unknown_credentials_act_as not in people:
        raise ValueError(
            f"unknown_credentials_act_as names {own.unknown_credentials_act_as!r}, who is not a person in the "
            f"scenario; they are {', '.join(people)}"
        )
    for channel in scenario.channels:
        if channel.provider == MANIFEST.key:
            what = f"the seeded channel {channel.name!r}" if channel.name is not None else "a seeded direct chat"
            raise ValueError(
                f"{what} is on {MANIFEST.key}, which holds no channels: Gmail has no channel or chat a conversation "
                "could be, so it would be dropped; mail a scenario starts with is seeded as this provider's own "
                "`emails`"
            )
    keys: set[str] = set()
    for n, email in enumerate(own.emails, start=1):
        what = f"seeded email {n}"
        if email.key is not None and email.key in keys:
            raise ValueError(f"{what} takes the key {email.key!r}, which an earlier email has")
        if email.in_reply_to is not None and email.in_reply_to not in keys:
            raise ValueError(f"{what} answers {email.in_reply_to!r}, which no earlier seeded email is")
        for name in [email.sender, *email.to, *email.cc]:
            _address(name, people, what)
        if email.key is not None:
            keys.add(email.key)
    for n, event in enumerate(own.events, start=1):
        what = f"seeded event {n}"
        if event.calendar not in people:
            raise ValueError(f"{what} is on {event.calendar!r}'s calendar, who is not a person in the scenario")
        for guest in event.attendees:
            _address(guest.person, people, what)


def _address(name: str, people: dict[str, Person], what: str) -> tuple[str, str]:
    """A name the seed gives someone: (display name, address). A name without `@` must be a person's key."""
    if "@" in name:
        return "", name
    if name not in people:
        raise ValueError(f"{what} names {name!r}, who is not a person in the scenario; they are {', '.join(people)}")
    return people[name].name, people[name].email


def _seed_emails(emails: list[SeededEmail], scenario: Scenario, mailboxes: MailWorld) -> None:
    people = {p.key: p for p in scenario.people}
    ids: dict[str, str] = {}
    for n, email in enumerate(emails, start=1):
        what = f"seeded email {n}"
        sender = _address(email.sender, people, what)
        message = EmailMessage()
        message["From"] = formataddr(sender)
        message["To"] = ", ".join(formataddr(_address(t, people, what)) for t in email.to)
        if email.cc:
            message["Cc"] = ", ".join(formataddr(_address(c, people, what)) for c in email.cc)
        if email.subject:
            message["Subject"] = email.subject
        sent = scenario.starts_at - email.ago
        message["Date"] = format_datetime(sent.astimezone(UTC))
        message_id = f"<seeded.{n}.{scenario.name}@mail.gmail.com>"
        message["Message-ID"] = message_id
        if email.in_reply_to is not None:
            message["In-Reply-To"] = ids[email.in_reply_to]
            message["References"] = ids[email.in_reply_to]
        message.set_content(email.text)
        if email.key is not None:
            ids[email.key] = message_id
        holder = mailboxes.owner(sender[1]) or next(
            (box for box in (mailboxes.owner(_address(t, people, what)[1]) for t in email.to) if box is not None), None
        )
        mailboxes.deliver(
            message.as_bytes(),
            sender=sender[1],
            actor=Actor.SCENARIO,
            taken_in=sent,
            snapshot_in=holder,
            recipient_labels=[gmail_wire.INBOX, gmail_wire.UNREAD] if email.unread else [gmail_wire.INBOX],
        )


def _seed_events(events: list[SeededEvent], scenario: Scenario, calendars: CalendarWorld) -> None:
    people = {p.key: p for p in scenario.people}
    for n, seeded in enumerate(events, start=1):
        what = f"seeded event {n}"
        organizer = people[seeded.calendar]
        start = scenario.starts_at + seeded.starts
        stamp = wire.rfc3339(scenario.starts_at)
        event_id = f"seeded{n:04d}{scenario.name.replace('_', '')[:20]}".lower()
        event_id = "".join(c for c in event_id if c in calendar_wire.EVENT_ID_CHARACTERS)
        attendees = []
        for guest in seeded.attendees:
            display, address = _address(guest.person, people, what)
            attendees.append(
                calendar_wire.Attendee(
                    email=address,
                    displayName=display or None,
                    organizer=True if address == organizer.email else None,
                    responseStatus=guest.response,
                )
            )
        me = calendar_wire.Actor(email=organizer.email, displayName=organizer.name)
        event = calendar_wire.StoredEvent(
            etag=f'"{n}"',
            id=event_id,
            htmlLink=html_link(event_id, organizer.email),
            created=stamp,
            updated=stamp,
            summary=seeded.summary,
            description=seeded.description,
            location=seeded.location,
            creator=me,
            organizer=me,
            start=calendar_wire.EventTime(dateTime=start.isoformat()),
            end=calendar_wire.EventTime(dateTime=(start + seeded.lasts).isoformat()),
            transparency="transparent" if seeded.transparent else None,
            iCalUID=f"{event_id}@google.com",
            attendees=attendees or None,
        )
        calendars.write(event, operation=Operation.CREATE, actor=Actor.SCENARIO, snapshot=True)


DECLARED = 1_000_000
"""Where the numbers of faults declared on an open world (`provider-faults`) start: above every number a seed gives
its own faults, which count from 0 in the seed's order, so a fault a later seed fragment adds never takes the
number of one declared before it, and the seed's are armed ahead of the declared ones."""


def write_faults(drive: DriveWorld, faults: list[FaultSeed], start: datetime, *, declared: bool = False) -> None:
    """Record each fault after those already recorded, from `start` plus its own offset."""
    first = (DECLARED if declared else 0) + len(drive.faults())
    answered = OPERATIONS | gmail.OPERATIONS | calendars.OPERATIONS
    for fault in faults:
        if fault.operation not in answered:
            raise ValueError(
                f"a fault names {fault.operation!r}, which is not a Google Drive, Docs, Slides, Gmail or Calendar call "
                f"this simulation answers; it answers {', '.join(sorted(answered))}"
            )
    for position, fault in enumerate(faults, start=first):
        stored = wire.StoredFault(
            operation=fault.operation,
            kind=fault.kind,
            after=wire.rfc3339(start + fault.after),
            remaining=fault.times,
        )
        drive.keep_fault(f"fault{position:04d}", stored, operation=Operation.CREATE)
