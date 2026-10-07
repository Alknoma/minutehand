"""What a conformance driver is: one small file per provider that performs each abstract operation through THAT
vendor's public API and reads the answer back through the same API.

A driver knows nothing of Minutehand's insides. It is handed an `Api` (the proxy, the CA bundle, and an HTTP client
whose every request is kept, so the record can be compared with what was sent) and the `WorldView` the control API
answered, and it answers in the neutral shapes below. Everything a property asserts is read through these shapes,
the control API, or the world's record.

A driver says "the vendor has no such thing" by naming the capability in `Driver.absent` with the reason; the
property then reports it as not applicable, never silently. A capability neither implemented nor declared absent
fails the property that needs it, saying so.
"""

from __future__ import annotations

import re
import ssl
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar

import httpx

from minutehand.adapters.control.wire import CreateWorld, WorldView
from minutehand.domain.scenario import AccessRole, DocumentKind


class Family(StrEnum):
    """What a provider is, read from the entity kinds its manifest maps. Every provider is in `ACCOUNTS`."""

    ACCOUNTS = "accounts"
    TICKETS = "tickets"
    MESSAGING = "messaging"
    DOCUMENTS = "documents"


class Property(StrEnum):
    SEEDED = "p01_seeded_is_held_or_refused"
    FAMILY = "p02_family_equivalence"
    PEOPLE_ACT = "p03_what_people_do_everywhere"
    ACCOUNTS = "p04_any_account_can_act"
    IDS = "p05_ids_are_stable"
    RECORD = "p06_the_record_is_complete_and_verbatim"
    STATE = "p07_state_lives_only_in_the_record"
    TIME = "p08_time_is_the_worlds"
    FAULTS = "p09_faults"
    LISTING = "p10_listing_is_consistent"


class IdKind(StrEnum):
    PERSON = "person"
    TICKET = "ticket"
    CHANNEL = "channel"
    MESSAGE = "message"
    DOCUMENT = "document"


class Progress(StrEnum):
    """Where a ticket stands, in the words every tracker has: the neutral `TicketState` plus `in_progress`, which
    the neutral view holds as open."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class AccountKind(StrEnum):
    """The kinds of account a seed can declare (property 4)."""

    MEMBER = "member"
    NO_EMAIL = "no_email"
    VENDOR_LOGIN = "vendor_login"
    BOT = "bot"


# ---------------------------------------------------------------------------------------------- what a driver sees


@dataclass(frozen=True)
class PersonSeen:
    id: str
    name: str
    email: str | None
    active: bool = True
    bot: bool = False
    guest: bool = False
    title: str | None = None
    login: str | None = None


@dataclass(frozen=True)
class CommentSeen:
    author_email: str | None
    text: str
    author_name: str | None = field(default=None, compare=False)
    """The author's display name, for a vendor that documents the email as never populated (Drive's comments)."""

    def by(self, email: str, name: str) -> bool:
        return self.author_email == email or (self.author_email is None and self.author_name == name)


@dataclass(frozen=True)
class TicketSeen:
    id: str
    title: str
    body: str
    progress: Progress
    assignee_email: str | None
    assignee_id: str | None = None
    labels: frozenset[str] = frozenset()
    comments: tuple[CommentSeen, ...] = ()
    key: str | None = field(default=None, metadata={"doc": "The vendor-visible key or number (PROJ-12), if any"})
    created: datetime | None = None
    links: tuple[str, ...] = field(default=(), metadata={"doc": "Vendor ids of the tickets it is linked to"})
    project: str | None = None
    changed_by_email: str | None = field(
        default=None, metadata={"doc": "Who the vendor says made its latest change (an activity, a changelog)"}
    )


@dataclass(frozen=True)
class ChannelSeen:
    id: str
    name: str | None
    private: bool = False
    archived: bool = False
    topic: str = ""
    purpose: str = ""
    member_emails: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MessageSeen:
    id: str
    text: str
    author_email: str | None
    thread_of: str | None = None
    at: datetime | None = None
    reactions: tuple[str, ...] = ()
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentSeen:
    id: str
    title: str
    kind: DocumentKind | None = None
    folder: str | None = field(default=None, metadata={"doc": "'/'-separated path from the top; None at the top"})
    owner_email: str | None = None
    space: str | None = None
    shared: Mapping[str, AccessRole] = field(default_factory=dict)
    last_editor_email: str | None = None
    modified: datetime | None = None
    created: datetime | None = None
    trashed: bool = False
    mime_type: str | None = None


@dataclass(frozen=True)
class Sent:
    """One request a driver made and the answer it got, as the bytes crossed."""

    method: str
    host: str
    path: str
    request: bytes
    status: int
    response: bytes


class Recorder:
    """Every request made through `Api.http`, in order."""

    def __init__(self) -> None:
        self.sent: list[Sent] = []

    def mark(self) -> int:
        return len(self.sent)

    def since(self, mark: int) -> list[Sent]:
        return self.sent[mark:]


@dataclass
class Api:
    """What the suite hands a driver: how a service reaches the fakes, and a client that keeps what it sent."""

    proxy: str
    bundle: str
    recorder: Recorder

    def trust(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=self.bundle)

    def http(self, headers: Mapping[str, str] | None = None, *, base_url: str = "") -> httpx.Client:
        """An httpx client through the proxy, trusting its CA; every exchange is kept on the recorder."""
        recorder = self.recorder

        def answered(response: httpx.Response) -> None:
            response.read()
            request = response.request
            recorder.sent.append(
                Sent(
                    method=request.method,
                    host=request.url.host,
                    path=request.url.raw_path.decode("ascii"),
                    request=request.content,
                    status=response.status_code,
                    response=response.content,
                )
            )

        return httpx.Client(
            proxy=self.proxy,
            verify=self.trust(),
            trust_env=False,
            timeout=30,
            headers=dict(headers or {}),
            base_url=base_url,
            event_hooks={"response": [answered]},
        )


class VendorRefused(Exception):
    """The vendor API refused an operation a driver performed: its status and body, for the failure message."""

    def __init__(self, what: str, response: httpx.Response) -> None:
        super().__init__(f"{what}: {response.status_code} {response.text[:300]}")
        self.status = response.status_code


def ok(what: str, response: httpx.Response) -> httpx.Response:
    """The response, or `VendorRefused` naming the operation when it is no success."""
    if not response.is_success:
        raise VendorRefused(what, response)
    return response


# ---------------------------------------------------------------------------------------------- the families


class Session(ABC):
    """One authenticated client of one world, as one account. Every provider's session is at least this.

    Every request a session makes goes through `Api.http` (property 6 compares them with the record), and every
    operation raises (`ok`, `VendorRefused`) when the vendor refuses it, never answers a default."""

    secrets: tuple[str, ...] = ()
    """Every credential this session sends or was answered, so the record can be searched for them."""

    @abstractmethod
    def close(self) -> None:
        """Release what the session holds (its HTTP clients)."""

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @abstractmethod
    def whoami(self) -> str:
        """The vendor's answer to "who is this credential": a login, an email or a display name, as answered."""

    @abstractmethod
    def people(self) -> list[PersonSeen]:
        """Every account the vendor lists, through its own listing, deactivated ones included where it lists them."""

    @abstractmethod
    def people_pages(self, page_size: int) -> list[list[str]]:
        """The account listing paged at `page_size`: each page's ids, in the order answered."""

    def person_id(self, email: str) -> str:
        """The vendor id of the account with that email, as `direct` and `assign` take it: by default, the one
        `people` lists."""
        found = [p.id for p in self.people() if p.email == email]
        if len(found) != 1:
            raise AssertionError(f"the vendor lists {len(found)} accounts with the email {email}")
        return found[0]

    @abstractmethod
    def unknown_credential(self) -> httpx.Response:
        """One request the vendor would answer, sent with a credential nobody seeded and nothing else naming a world
        (no claimed host, site or tenant key), so no world claims it."""

    @abstractmethod
    def observe(self) -> str:
        """A fixed set of reads, their answered bodies joined: "the vendor's answers" for properties 7 and 8."""

    @abstractmethod
    def change(self, label: str) -> None:
        """One write through the vendor API, holding `label`, that changes what `observe` reads."""

    def stamp(self, label: str) -> tuple[str, datetime]:
        """Create one thing holding `label`; its id and the creation time the vendor reports, parsed strictly in
        the vendor's own format. Each family supplies it."""
        raise NotImplementedError(f"{type(self).__name__} creates nothing with a time")

    def stamped(self, made: str) -> datetime:
        """The creation time the vendor reports, read again, for a thing `stamp` made."""
        raise NotImplementedError(f"{type(self).__name__} creates nothing with a time")


class Tickets(Session):
    @abstractmethod
    def ticket_ids(self) -> dict[str, str]:
        """Every ticket the API reaches, title → vendor id."""

    @abstractmethod
    def read_ticket(self, ticket: str) -> TicketSeen: ...

    @abstractmethod
    def create_ticket(self, project: str, title: str, body: str) -> str:
        """A ticket filed in the project of that name; its vendor id."""

    @abstractmethod
    def set_progress(self, ticket: str, progress: Progress) -> None: ...

    @abstractmethod
    def assign(self, ticket: str, person: str | None) -> None:
        """Assign to the account with that vendor id (from `people`), or unassign."""

    @abstractmethod
    def comment(self, ticket: str, text: str) -> None: ...

    @abstractmethod
    def link(self, ticket: str, other: str) -> None: ...

    @abstractmethod
    def relabel(self, ticket: str, labels: list[str]) -> None:
        """Its labels become exactly these."""

    @abstractmethod
    def delete_ticket(self, ticket: str) -> None: ...

    @abstractmethod
    def search_tickets(self, text: str) -> list[str]:
        """Ids of the tickets the vendor's search finds for `text` in their titles."""

    @abstractmethod
    def ticket_pages(self, project: str, page_size: int) -> list[list[str]]: ...

    def stamp(self, label: str) -> tuple[str, datetime]:
        made = self.create_ticket(STAMP_PROJECT, label, "")
        return made, self.stamped(made)

    def stamped(self, made: str) -> datetime:
        created = self.read_ticket(made).created
        if created is None:
            raise AssertionError("the tracker reported no creation time for a ticket")
        return created


class Messaging(Session):
    @abstractmethod
    def channel(self, name: str) -> str:
        """The vendor id of the channel seeded as `name`."""

    @abstractmethod
    def direct(self, person: str) -> str:
        """The direct conversation between the session's account and the account with that vendor id."""

    @abstractmethod
    def channels(self) -> list[ChannelSeen]:
        """Every named channel the vendor lists to this account, archived and private ones included."""

    @abstractmethod
    def post(self, channel: str, text: str) -> str: ...

    @abstractmethod
    def post_button(self, channel: str, text: str, action_id: str, label: str) -> str:
        """A message carrying one button, in the vendor's own way of writing one."""

    @abstractmethod
    def reply(self, channel: str, thread: str, text: str) -> str: ...

    @abstractmethod
    def edit(self, channel: str, message: str, text: str) -> None: ...

    @abstractmethod
    def react(self, channel: str, message: str, reaction: str) -> None: ...

    @abstractmethod
    def delete_message(self, channel: str, message: str) -> None: ...

    @abstractmethod
    def history(self, channel: str) -> list[MessageSeen]:
        """The conversation's messages, thread replies included, oldest first; deleted ones absent."""

    @abstractmethod
    def history_pages(self, channel: str, page_size: int) -> list[list[str]]: ...

    def stamp(self, label: str) -> tuple[str, datetime]:
        channel = self.channel(STAMP_CHANNEL)
        return self.post(channel, label), self._at(channel, label)

    def stamped(self, made: str) -> datetime:
        found = next(m for m in self.history(self.channel(STAMP_CHANNEL)) if m.id == made)
        if found.at is None:
            raise AssertionError("the service reported no time for a message")
        return found.at

    def _at(self, channel: str, label: str) -> datetime:
        found = next(m for m in self.history(channel) if m.text == label)
        if found.at is None:
            raise AssertionError("the service reported no time for a message")
        return found.at


class Documents(Session):
    @abstractmethod
    def documents(self) -> list[DocumentSeen]:
        """Every document the session's account reaches, trashed ones excluded, folders excluded."""

    @abstractmethod
    def create_document(self, title: str, folder: str | None) -> str:
        """A word-processor document, at the top or in the folder at that '/'-path (made when not there)."""

    @abstractmethod
    def write(self, document: str, text: str) -> None:
        """Its text becomes `text`."""

    @abstractmethod
    def read_text(self, document: str) -> str: ...

    @abstractmethod
    def rename(self, document: str, title: str) -> None: ...

    @abstractmethod
    def move(self, document: str, folder: str) -> None:
        """Into the folder at that '/'-path, made when not there."""

    @abstractmethod
    def share(self, document: str, email: str, role: AccessRole) -> None: ...

    @abstractmethod
    def trash(self, document: str) -> None: ...

    @abstractmethod
    def comments(self, document: str) -> list[CommentSeen]: ...

    @abstractmethod
    def document_pages(self, page_size: int) -> list[list[str]]: ...

    def upload(self, name: str, content: bytes, mime_type: str) -> str:
        """A binary file uploaded; its id. Only where the vendor has uploads (else declared `documents.binary`)."""
        raise NotImplementedError(f"{type(self).__name__} has no upload")

    def download(self, document: str) -> bytes:
        raise NotImplementedError(f"{type(self).__name__} has no download")

    def stamp(self, label: str) -> tuple[str, datetime]:
        made = self.create_document(label, None)
        return made, self.stamped(made)

    def stamped(self, made: str) -> datetime:
        found = next(d for d in self.documents() if d.id == made)
        stamp = found.created or found.modified
        if stamp is None:
            raise AssertionError("the service reported no time for a document")
        return stamp


STAMP_PROJECT = "Launch"
STAMP_CHANNEL = "launch"

FAMILY_SESSIONS: dict[Family, type[Session]] = {
    Family.ACCOUNTS: Session,
    Family.TICKETS: Tickets,
    Family.MESSAGING: Messaging,
    Family.DOCUMENTS: Documents,
}


# ---------------------------------------------------------------------------------------------- what drivers declare


@dataclass(frozen=True)
class FaultCase:
    """One fault kind a provider declares (`POST /provider-faults`), how to trigger it, and what it must look like.

    `trigger` performs the faulted call with the vendor's REAL client library and returns what it raised (None when
    it raised nothing); `typed` is the exception that library raises for an API error (None where it has none);
    `status` and `holds` are the documented status and a phrase of the documented error body; `uses` is how many
    calls it answers before it is consumed; `then` is the same call, which must succeed once it is consumed (or once
    the clock has moved `expires_after`)."""

    name: str
    fragment: Mapping[str, object]
    trigger: Callable[[Api, WorldView], BaseException | None]
    typed: type[BaseException] | None
    status: int
    holds: str
    uses: int = 1
    expires_after: timedelta | None = None
    then: Callable[[Api, WorldView], None] | None = None


@dataclass(frozen=True)
class DeclaredId:
    """A seed that names a thing's vendor-visible id or number, where the provider's docs say it may: `seed` is
    merged into the world's seed (top-level keys), `read` reads the id back through the vendor API."""

    name: str
    seed: Mapping[str, object]
    read: Callable[[Session], str]
    expected: str


@dataclass(frozen=True)
class PermissionCase:
    """A named permission the provider grants and withholds (`POST /permissions`), and a read it governs."""

    person: str
    permission: str
    project: str | None
    allowed: Callable[[Session], bool]


CAPABILITIES: dict[str, str] = {
    "accounts.people": "the vendor lists accounts at all",
    "accounts.whoami": "the API answers who the credential in use is",
    "accounts.person_credentials": "a person can hold a credential of their own and act through the API with it",
    "accounts.title": "an account shows a job title",
    "accounts.bot": "an account can be a bot or app user, and says so",
    "accounts.guest": "an account can be a guest, and says so",
    "accounts.deactivated": "an account can be deactivated and still listed, and says so",
    "accounts.no_email": "an account can exist with no email",
    "accounts.vendor_login": "an account has a login of its own (dots, dashes, uppercase)",
    "tickets.in_progress": "a ticket can be in progress, between open and done",
    "tickets.cancelled": "a ticket can be cancelled, apart from done",
    "tickets.link": "two tickets can be linked",
    "tickets.labels": "a ticket carries labels or tags",
    "tickets.changed_by": "the API says who made a ticket's latest change",
    "messaging.channels": "messages sit in named channels a seed declares, and in the seeded direct chats",
    "messaging.edit": "a message already sent can be edited",
    "messaging.react": "a message takes a reaction",
    "messaging.button": "a message carries a button a person can press",
    "messaging.files": "a message carries files",
    "messaging.topic": "a channel has a topic",
    "messaging.purpose": "a channel has a purpose or description",
    "messaging.private": "a channel can be private",
    "messaging.archived": "a channel can be archived and still listed",
    "messaging.direct": "the agent's account has a direct conversation with a person",
    "documents.binary": "a binary file can be uploaded and downloaded",
    "documents.folders": "a document sits in a folder path",
    "documents.spaces": "a document sits in a shared space (a shared drive, a site)",
    "documents.spreadsheet": "a spreadsheet is a kind of document here",
    "documents.presentation": "a presentation is a kind of document here",
    "documents.file": "an uploaded file of a media type is a kind of document here",
    "documents.sharing": "a document is shared with a person, and who it is shared with is read back, through the API",
    "documents.comments": "a document takes comments, read back through the API",
    "documents.last_editor": "the API says who last changed a document, and when",
    "documents.owner": "the API says who owns a document",
    "time.stamp": "the provider creates something that carries a creation time",
    "state.concurrent": "two worlds can be open at once and told apart by their calls",
    "listing.people": "the account listing is paged",
    "listing.tickets": "the ticket listing is paged",
    "listing.history": "a conversation's history is paged",
    "listing.documents": "the document listing is paged",
}
"""Every capability a driver may declare absent (`Driver.absent`), and what having it means. A declaration must
name one of these and give the vendor's reason; the properties report each one."""


# ---------------------------------------------------------------------------------------------- the driver


class Driver(ABC):
    """One provider's way through its vendor API. A module `drivers/<provider>.py` defines `DRIVER`."""

    provider: ClassVar[str]
    session: ClassVar[type[Session]]
    """The session class `connect` returns; the families it subclasses are the families the driver drives."""
    absent: ClassVar[Mapping[str, str]] = {}
    """Capability → why the vendor has no such thing. The capability names are listed in docs/conformance.md."""
    id_formats: ClassVar[Mapping[IdKind, re.Pattern[str]]] = {}
    """Each id kind's documented format, justified in a comment beside it from the provider's README or CLAIMS."""
    page_floor: ClassVar[Mapping[str, int]] = {}
    """The smallest page size each list operation allows: `people`, `tickets`, `history`, `documents`."""
    vendor_login: ClassVar[str] = "Lena.Ortiz-QA"
    """A login the vendor can hold that a scenario key could never be: dots, dashes or uppercase. A vendor whose
    logins cannot hold one of these overrides it with one that holds the others."""
    time_resolution: ClassVar[timedelta] = timedelta(seconds=1)
    """The finest a time the vendor answers can be, as its documentation says (a minute where it rounds to one)."""
    unknown_refusal: ClassVar[tuple[int, str]]
    """The vendor's documented answer to a request with a credential it does not know: status, and a phrase of
    its body."""

    @abstractmethod
    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        """The world to open: `seed` (people, tickets, documents, channels, spaces, sign-ins, `starts_at`) plus
        whatever this provider needs to be reached (its own seed) and claims unique to `tag` (lowercase letters and
        digits), with a credential for every person where the vendor gives people credentials. The same `tag` makes
        the same claims, so a world opened again after the first closed is reached the same way. `logins` gives a
        person (by key) a vendor login of their own, where the vendor has logins."""

    @abstractmethod
    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        """A session as the agent's account, or as `person` (a Person.key) when given."""

    def signed_in(self, api: Api, world: WorldView, credential: str) -> Session:
        """A session through a seeded `SignIn` credential. Every vendor has some sign-in; a provider that reads no
        `SignIn` must refuse a seed that gives one, so this raising is a failure, never an exception."""
        raise NotImplementedError(f"{type(self).__name__} has no sign-in")

    def sign_in_credential(self, unique: str) -> str:
        """A credential of the shape the vendor issues, holding `unique`, for a seeded `SignIn`."""
        return f"signin-{unique}"

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        """The credential `connect` uses for the person (or the agent); None where the person can have none."""
        return None

    def faults(self) -> list[FaultCase]:
        """Every fault kind the provider declares."""
        return []

    def declared_ids(self) -> list[DeclaredId]:
        return []

    def permission(self) -> PermissionCase | None:
        """The provider's named permissions, where it has any."""
        return None
