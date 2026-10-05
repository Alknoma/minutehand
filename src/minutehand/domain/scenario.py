"""A scenario: the goal, the people, and what already exists in the world.

A scenario holds no absolute dates except `starts_at`. Every other moment is an
offset from it, so the same file replays on any day and under any seed.

A scenario file may leave `starts_at` out (`WrittenScenario`): it then starts at the
moment the run starts, for an agent that reads the real clock. A run resolves it once,
before anything is played, into the `Scenario` it plays and records, so every sample,
fork and rerun of that run starts at the same instant.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    """Every model in this package: frozen, and an unknown field is an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


ProviderKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
"""A provider's registry key ("slack", "asana"). Open, because the set of providers
is whatever is installed; it is checked against the registry when a scenario loads."""


class AbsenceTrigger(StrEnum):
    AT_START = "at_start"
    ON_FIRST_ASK = "on_first_ask"


class Absence(Model):
    """A stretch during which a person does not answer."""

    trigger: AbsenceTrigger = AbsenceTrigger.AT_START
    starts_after: timedelta = timedelta(0)
    lasts: timedelta
    delegate: str | None = Field(default=None, description="Person.key who covers")
    reason: str | None = None


class DelayRange(Model):
    """How long a person takes to answer, in simulated time."""

    shortest: timedelta = timedelta(hours=6)
    longest: timedelta = timedelta(hours=66)

    @model_validator(mode="after")
    def _ordered(self) -> DelayRange:
        if self.longest < self.shortest:
            raise ValueError("longest is shorter than shortest")
        return self


class FormInput(Model):
    """What a person types into one field of a form the agent opens for them."""

    input_id: str | None = Field(
        default=None, description="The provider's own id for the field; None is the form's only text field"
    )
    value: str


class ScriptedPress(Model):
    """Use a control on the message being answered instead of writing back: the one whose label reads `label`, as
    the person sees it, in any case. When the agent answers the press by opening a form, the person fills it with
    `form` and submits it."""

    label: str = Field(min_length=1)
    picks: str | None = Field(default=None, description="Person.key chosen, when the control picks a person")
    form: list[FormInput] = []


class ScriptedReply(Model):
    """One fixed answer, given to the nth question this person receives: text written back, or a control used."""

    to_ask: int = Field(ge=1)
    text: str = ""
    press: ScriptedPress | None = None

    @model_validator(mode="after")
    def _says_something(self) -> ScriptedReply:
        if not self.text and self.press is None:
            raise ValueError("a scripted reply writes text or presses something")
        if self.text and self.press is not None:
            raise ValueError("a scripted reply either writes text or presses something, not both")
        return self

    @property
    def said(self) -> str:
        """Everything this reply puts into the world in the person's words: the text, or what they type in the form."""
        if self.press is None:
            return self.text
        return "\n".join(f.value for f in self.press.form)


class Helpfulness(StrEnum):
    """What this person does with a question."""

    FULL = "full"  # answers it
    PARTIAL = "partial"  # answers part and leaves the rest
    ASKS_BACK = "asks_back"  # replies with a question of their own
    DECLINES = "declines"  # says it is not theirs, names nobody
    MISTAKEN = "mistaken"  # answers confidently from an out-of-date fact


class Answers(Model):
    """Replies are written by a model from this person's facts.

    The first reply to each ask is stored with the run; a rerun replays it, so
    only a new ask costs a model call.
    """

    kind: Literal["answers"] = "answers"
    delay: DelayRange = DelayRange()
    helpfulness: Helpfulness = Helpfulness.FULL
    voice: str | None = Field(default=None, description="How they write: terse, formal, chatty")
    model: str | None = Field(default=None, description="None uses the run's default model")
    temperature: float = Field(default=0.6, ge=0, le=2)


class Scripted(Model):
    """Replies are fixed text. No model call, fully repeatable."""

    kind: Literal["scripted"] = "scripted"
    delay: DelayRange = DelayRange()
    replies: list[ScriptedReply]


class Silent(Model):
    """This person never answers."""

    kind: Literal["silent"] = "silent"


ReplyBehaviour = Annotated[Answers | Scripted | Silent, Field(discriminator="kind")]


class WorkingHours(Model):
    """Replies land only inside these hours, in this person's own timezone."""

    timezone: str = "UTC"
    opens: time = time(9)
    closes: time = time(17)
    weekdays_only: bool = True


class Account(StrEnum):
    """What kind of account a person holds in the services the run fakes."""

    MEMBER = "member"
    GUEST = "guest"  # invited from outside, sees only the channels they are in
    DEACTIVATED = "deactivated"  # once a member; listed, but can no longer be reached
    BOT = "bot"  # another app's bot user, not a human; never answers


class Person(Model):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    email: str
    title: str | None = None
    account: Account = Account.MEMBER
    facts: list[str] = Field(default=[], description="What this person knows; all a model reply may draw on")
    stale_facts: list[str] = Field(default=[], description="What they believe that is no longer true")
    reply: ReplyBehaviour = Answers()
    working_hours: WorkingHours | None = None
    absences: list[Absence] = []


class TicketState(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class SeededComment(Model):
    """A comment already on a seeded ticket when the run starts."""

    by: str = Field(description="Person.key")
    text: str = Field(min_length=1)


class SeededTicket(Model):
    key: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="The scenario's own name for this ticket, by which a provider's seed names it; only a provider "
        "whose manifest holds ticket keys accepts one",
    )
    provider: ProviderKey
    project: str
    title: str
    body: str = ""
    assignee: str | None = Field(default=None, description="Person.key")
    state: TicketState = TicketState.OPEN
    labels: list[str] = Field(default=[], description="Tags or labels the ticket carries")
    comments: list[SeededComment] = []


class DocumentKind(StrEnum):
    """What a seeded document is, whatever its provider calls it."""

    DOCUMENT = "document"  # a word-processor document; `text` is Markdown: headings, lists, tables, links
    SPREADSHEET = "spreadsheet"  # `rows` are its cells, first sheet only
    PRESENTATION = "presentation"  # `text`: one slide per block of lines separated by a blank line
    FILE = "file"  # an uploaded file: `text` is its content, as UTF-8, of `mime_type`


class AccessRole(StrEnum):
    """What a person may do with a document or a shared space, from least to most."""

    READER = "reader"
    COMMENTER = "commenter"
    WRITER = "writer"
    ORGANIZER = "organizer"


class Access(Model):
    person: str = Field(description="Person.key")
    role: AccessRole = AccessRole.WRITER


class SeededDocument(Model):
    provider: ProviderKey
    title: str
    text: str = ""
    kind: DocumentKind = DocumentKind.DOCUMENT
    rows: list[list[str]] = Field(default=[], description="A spreadsheet's cells, row by row")
    mime_type: str | None = Field(default=None, description="A FILE's media type; text/plain when None")
    folder: str | None = Field(
        default=None, description="A folder path, '/'-separated, made the first time it is named"
    )
    owner: str | None = Field(default=None, description="Person.key; None is the scenario's owner")
    space: str | None = Field(default=None, description="SharedSpace.name it lives in; None is its owner's own")
    shared_with: list[Access] = []
    modified_before_start: timedelta = Field(
        default=timedelta(0), ge=timedelta(0), description="How long before the scenario starts it was last changed"
    )
    modified_by: str | None = Field(default=None, description="Person.key who changed it last; None is its owner")

    @model_validator(mode="after")
    def _content_fits_kind(self) -> SeededDocument:
        if self.rows and self.kind is not DocumentKind.SPREADSHEET:
            raise ValueError(f"document {self.title!r} has rows but is a {self.kind.value}")
        if self.mime_type is not None and self.kind is not DocumentKind.FILE:
            raise ValueError(f"document {self.title!r} has a mime_type but is a {self.kind.value}")
        return self


class SharedSpace(Model):
    """A place documents live that no one person owns: a shared drive."""

    provider: ProviderKey
    name: str
    members: list[Access] = Field(min_length=1)


class SignIn(Model):
    """A credential the agent signs in to a provider with, and whom it signs in as.

    The provider decides what the credential is: for Google, a refresh token or a service account's email.
    Once a scenario names any sign-in for a provider, a credential it does not name is refused."""

    provider: ProviderKey
    credential: str = Field(min_length=1)
    person: str | None = Field(default=None, description="Person.key; None: an account of its own, not a person's")


class Edited(Model):
    """The person adds a paragraph at the end of the document."""

    kind: Literal["edited"] = "edited"
    append: str = Field(min_length=1)


class Renamed(Model):
    kind: Literal["renamed"] = "renamed"
    to: str = Field(min_length=1)


class Moved(Model):
    kind: Literal["moved"] = "moved"
    folder: str = Field(description="A folder path in the same place, made if it is not there")


class Shared(Model):
    kind: Literal["shared"] = "shared"
    access: Access


class Trashed(Model):
    kind: Literal["trashed"] = "trashed"


DocumentAction = Annotated[Edited | Renamed | Moved | Shared | Trashed, Field(discriminator="kind")]


class DocumentHappening(Model):
    """A person changes a seeded document at a moment, with no agent involved: edits, renames, moves, shares or
    trashes it.

    It lands on the run's clock through the provider that holds the document (`ports.provider.ChangesDocuments`),
    recorded as that person's change. It wakes nobody, unless the agent asked that provider to be told of changes
    (`ports.provider.NotifiesChanges`): then telling it is a wake, as a pushed event is.
    """

    kind: Literal["document"] = "document"
    person: str = Field(description="Person.key of whoever changes it")
    document: str = Field(description="The title of the seeded document changed; it names exactly one")
    after: timedelta = Field(gt=timedelta(0), description="Offset from the scenario's start")
    action: DocumentAction


class ProviderSeed(Model):
    """What one provider seeds beyond the shared people, tickets and documents, in that provider's own shape.

    The shared model cannot name a provider's types (`domain` imports no adapter), so the shape crosses as
    the provider's own JSON text, as a stored entity's body does, and only that provider parses it: it
    validates `body` against its own seed model when it seeds, and refuses what it cannot read. A scenario
    file writes the shape as YAML or JSON structure; it is kept as its JSON text.
    """

    provider: ProviderKey
    body: str = Field(description="The provider's own seed model, as JSON text")

    @field_validator("body", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> object:
        if isinstance(value, dict | list):
            return json.dumps(value)
        return value


ChannelName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")]
PostKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]

GENERAL_CHANNEL: ChannelName = "general"
"""The channel every workspace starts with and every member is in; a happening may name it unseeded."""


class SeededFile(Model):
    """A file attached to a message, with its content, downloadable by the agent."""

    name: str
    title: str | None = None
    mime_type: str = "text/plain"
    text: str


class SeededPost(Model):
    """A message already in a conversation when the run starts, with the thread under it."""

    by: str = Field(description="Person.key")
    text: str
    ago: timedelta = Field(gt=timedelta(0), description="How long before the scenario starts it was posted")
    key: PostKey | None = Field(default=None, description="How a happening names this post")
    files: list[SeededFile] = []
    replies: list[SeededPost] = Field(default=[], description="Its thread, each reply posted after it")

    @model_validator(mode="after")
    def _replies_follow(self) -> SeededPost:
        if any(r.ago >= self.ago for r in self.replies):
            raise ValueError(f"a reply to {self.text[:40]!r} is older than the post it answers")
        if any(r.replies for r in self.replies):
            raise ValueError("a thread reply has no thread of its own")
        return self


class SeededChannel(Model):
    """A conversation that exists when the run starts: a named channel, or with no name, the direct conversation
    between the agent and its members (one member: a DM; more: a group DM)."""

    provider: ProviderKey
    name: ChannelName | None = None
    private: bool = False
    archived: bool = False
    topic: str = ""
    purpose: str = ""
    members: list[str] = Field(default=[], description="Person.key of each human member")
    agent_member: bool = Field(default=True, description="Whether the agent's own account is in it")
    history: list[SeededPost] = []


class _MessagingHappening(Model):
    """Something a person does in a messaging service, pushed to the agent as the real service pushes it
    (`ports.provider.PushesEvents.happen`)."""

    provider: ProviderKey
    person: str = Field(description="Person.key of whoever does it")
    after: timedelta = Field(default=timedelta(0), ge=timedelta(0), description="Offset from the scenario's start")


class PersonPosts(_MessagingHappening):
    """A person writes, unprompted: in a channel, or with no channel in their DM with the agent."""

    kind: Literal["posts"] = "posts"
    channel: ChannelName | None = None
    text: str
    mentions_agent: bool = Field(default=False, description="The message names the agent, as an @-mention")
    in_thread_of: PostKey | None = None
    key: PostKey | None = None
    files: list[SeededFile] = []


class PersonEdits(_MessagingHappening):
    kind: Literal["edits"] = "edits"
    post: PostKey
    text: str


class PersonDeletes(_MessagingHappening):
    kind: Literal["deletes"] = "deletes"
    post: PostKey


class PersonReacts(_MessagingHappening):
    """A reaction on a post, or with none named, on the agent's latest message in `channel` (None: their DM)."""

    kind: Literal["reacts"] = "reacts"
    post: PostKey | None = None
    channel: ChannelName | None = None
    reaction: str = Field(pattern=r"^[a-z0-9_+-]+$")


class PersonJoins(_MessagingHappening):
    kind: Literal["joins"] = "joins"
    channel: ChannelName


class PersonOpensAgent(_MessagingHappening):
    """A person opens the agent's own page inside the service (Slack's App Home)."""

    kind: Literal["opens_agent"] = "opens_agent"


class PersonCommands(_MessagingHappening):
    """A person runs one of the agent's commands (a Slack slash command), in a channel or their DM with it."""

    kind: Literal["commands"] = "commands"
    command: str = Field(pattern=r"^/[a-z0-9_-]+$")
    text: str = ""
    channel: ChannelName | None = None


MessagingHappening = (
    PersonPosts | PersonEdits | PersonDeletes | PersonReacts | PersonJoins | PersonOpensAgent | PersonCommands
)
"""What a person does unprompted in a messaging service; each member names its own provider."""


class Moves(Model):
    """The person moves the ticket to a state: ticks it done, cancels it, or reopens it."""

    kind: Literal["moves"] = "moves"
    to: TicketState


class Reassigns(Model):
    """The person hands the ticket to someone else, or takes everyone off it."""

    kind: Literal["reassigns"] = "reassigns"
    to: str | None = Field(description="Person.key; None leaves it unassigned")


class Comments(Model):
    """The person writes a comment on the ticket."""

    kind: Literal["comments"] = "comments"
    text: str = Field(min_length=1)


class Deletes(Model):
    """The person deletes the ticket."""

    kind: Literal["deletes"] = "deletes"


TicketAction = Annotated[Moves | Reassigns | Comments | Deletes, Field(discriminator="kind")]


class TicketHappening(Model):
    """A person acts on a seeded ticket at a moment, with no agent involved: the agent finds it on its next read.

    It lands on the run's clock through the provider that holds the ticket (`ports.provider.ActsOnTickets`),
    recorded as that person's change, and wakes nobody, as a ticket fate does not. A ticket already deleted by
    then is left alone.
    """

    kind: Literal["ticket"] = "ticket"
    person: str = Field(description="Person.key of whoever acts")
    ticket: str = Field(description="The title of the seeded ticket acted on; it names exactly one")
    after: timedelta = Field(ge=timedelta(0), description="Offset from the scenario's start")
    action: TicketAction


Happening = Annotated[TicketHappening | DocumentHappening | MessagingHappening, Field(discriminator="kind")]
"""Something a person does by themselves, unprompted, at a moment the scenario sets. Three families, one per kind of
thing acted on, each landing through the port its provider implements: a `TicketHappening` through `ActsOnTickets`,
a `DocumentHappening` through `ChangesDocuments`, a `MessagingHappening` through `PushesEvents.happen`. A scenario
whose happening lands on a provider without that port is refused before the run starts. A ticket happening wakes
nobody; a document happening wakes the agent only when it watches that provider's changes; a messaging happening is
pushed to the agent, as a reply is. A new family is a new member with its own `kind` and its own port."""


class TicketFate(Model):
    """What happens to a ticket the agent hands to a person."""

    assignee: str = Field(description="Person.key")
    becomes: TicketState
    after: timedelta


class Direction(Model):
    """Something the owner says to the agent partway through."""

    text: str
    after: timedelta


class Bound(Model):
    """How many matches are right, and by when."""

    at_least: int = Field(default=1, ge=0)
    at_most: int | None = Field(default=None, ge=0)
    by: timedelta | None = Field(default=None, description="Offset from the scenario's start")

    @model_validator(mode="after")
    def _ordered(self) -> Bound:
        if self.at_most is not None and self.at_most < self.at_least:
            raise ValueError("at_most is below at_least")
        return self


class PersonAsked(Bound):
    """The agent sent this person a message; with `about`, one that asks them about that, as a model judges it."""

    kind: Literal["person_asked"] = "person_asked"
    person: str = Field(description="Person.key")
    mentions: list[str] = Field(default=[], description="Words the message must contain, any case")
    about: str | None = Field(
        default=None,
        description="What the message asks this person about, in meaning rather than words; a model judges it",
    )


class TicketCreated(Bound):
    """The agent filed a ticket."""

    kind: Literal["ticket_created"] = "ticket_created"
    assignee: str | None = Field(default=None, description="Person.key; None matches any")
    mentions: list[str] = Field(default=[], description="Words the title or body must contain")


class TicketDeleted(Bound):
    """The agent deleted a ticket. The default says it must not."""

    kind: Literal["ticket_deleted"] = "ticket_deleted"
    at_least: int = Field(default=0, ge=0)
    at_most: int | None = Field(default=0, ge=0)


class TicketInState(Bound):
    """A ticket assigned to this person reached this state."""

    kind: Literal["ticket_in_state"] = "ticket_in_state"
    assignee: str = Field(description="Person.key")
    state: TicketState


class Relayed(Bound):
    """The agent passed on what a person said: a message from the agent to `to` carries the `tell`, a phrase the
    scenario's author picks from what `said_by` will say, matched in any case.

    The tell is what makes this mechanical rather than a guess at meaning. A scenario is refused when its goal, a
    direction, a seeded ticket or document, or anyone else's scripted reply or facts holds the tell, and when
    `said_by` could never say it: silent, or none of their scripted replies (or, written by a model, none of their
    facts) holds it. In a run, the first thing in the world to hold the tell must be `said_by`'s own reply; an
    agent message that held it before means the agent did not hear it from them, and no message counts."""

    kind: Literal["relayed"] = "relayed"
    said_by: str = Field(description="Person.key of whoever says the tell")
    to: str = Field(description="Person.key the agent must pass it on to")
    tell: str = Field(min_length=1, description="A phrase only `said_by`'s answer holds")


Expectation = Annotated[
    PersonAsked | TicketCreated | TicketDeleted | TicketInState | Relayed, Field(discriminator="kind")
]


class _ScenarioBody(Model):
    """Everything a scenario says but when it starts."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    goal: str = Field(description="The text handed to the agent, verbatim")
    owner: str = Field(description="Person.key of whoever gave the goal")
    deadline_after: timedelta | None = None
    max_wakes: int = Field(default=20, ge=1)
    seed: int = 17
    protected_names: list[str] = Field(default=[], description="Names the agent must spell exactly as given")
    people: list[Person]
    tickets: list[SeededTicket] = []
    documents: list[SeededDocument] = []
    spaces: list[SharedSpace] = []
    sign_ins: list[SignIn] = []
    ticket_fates: list[TicketFate] = []
    directions: list[Direction] = []
    channels: list[SeededChannel] = Field(default=[], description="Conversations that exist when the run starts")
    happenings: list[Happening] = Field(
        default=[], description="What people do by themselves, unprompted, at moments the scenario sets"
    )
    provider_seeds: list[ProviderSeed] = Field(
        default=[], description="Each provider's own seed beyond people, tickets and documents; one per provider"
    )
    expect: list[Expectation] = Field(default=[], description="What must be true of the world for this run to be right")

    @model_validator(mode="after")
    def _keys_resolve(self) -> Self:
        keys = [p.key for p in self.people]
        if len(keys) != len(set(keys)):
            raise ValueError("two people share a key")
        ticket_keys = [t.key for t in self.tickets if t.key is not None]
        if len(ticket_keys) != len(set(ticket_keys)):
            raise ValueError("two seeded tickets share a key")
        known = set(keys)
        self._refuse_unknown_places()
        named = [self.owner]
        named += [k for c in self.channels for k in c.members]
        named += [p.by for c in self.channels for p in _every_post(c.history)]
        named += [h.person for h in self.happenings]
        named += [
            r.press.picks
            for p in self.people
            if isinstance(p.reply, Scripted)
            for r in p.reply.replies
            if r.press is not None and r.press.picks
        ]
        named += [t.assignee for t in self.tickets if t.assignee]
        named += [c.by for t in self.tickets for c in t.comments]
        named += [f.assignee for f in self.ticket_fates]
        named += [a.delegate for p in self.people for a in p.absences if a.delegate]
        named += [e.person for e in self.expect if isinstance(e, PersonAsked)]
        named += [e.assignee for e in self.expect if isinstance(e, (TicketCreated, TicketInState)) and e.assignee]
        named += [k for e in self.expect if isinstance(e, Relayed) for k in (e.said_by, e.to)]
        named += [h.action.to for h in self._ticket_happenings() if isinstance(h.action, Reassigns) and h.action.to]
        named += [k for d in self.documents for k in (d.owner, d.modified_by) if k is not None]
        named += [a.person for d in self.documents for a in d.shared_with]
        named += [a.person for s in self.spaces for a in s.members]
        named += [s.person for s in self.sign_ins if s.person is not None]
        named += [h.action.access.person for h in self._document_happenings() if isinstance(h.action, Shared)]
        missing = sorted(set(named) - known)
        if missing:
            raise ValueError(f"no such person: {', '.join(missing)}")
        for happening in self._ticket_happenings():
            self.happening_ticket(happening)
        self._places_resolve()
        seeded = [s.provider for s in self.provider_seeds]
        twice = sorted({p for p in seeded if seeded.count(p) > 1})
        if twice:
            raise ValueError(f"more than one provider seed for {', '.join(twice)}")
        for relayed in (e for e in self.expect if isinstance(e, Relayed)):
            self._refuse_tell(relayed)
        return self

    def happening_ticket(self, happening: TicketHappening) -> SeededTicket:
        """The one seeded ticket a happening acts on, found by its title."""
        found = [t for t in self.tickets if t.title == happening.ticket]
        if len(found) != 1:
            raise ValueError(
                f"a happening acts on the seeded ticket {happening.ticket!r}, and {len(found)} seeded tickets "
                "have that title; it must name exactly one"
            )
        return found[0]

    def happening_document(self, happening: DocumentHappening) -> SeededDocument:
        """The one seeded document a happening changes, found by its title."""
        found = [d for d in self.documents if d.title == happening.document]
        if len(found) != 1:
            raise ValueError(
                f"a happening changes the seeded document {happening.document!r}, and {len(found)} seeded documents "
                "have that title; it must name exactly one"
            )
        return found[0]

    def happening_provider(self, happening: Happening) -> ProviderKey:
        """The provider a happening lands on: the one holding its seeded ticket or document, or the one it names."""
        if isinstance(happening, TicketHappening):
            return self.happening_ticket(happening).provider
        if isinstance(happening, DocumentHappening):
            return self.happening_document(happening).provider
        return happening.provider

    def _ticket_happenings(self) -> list[TicketHappening]:
        return [h for h in self.happenings if isinstance(h, TicketHappening)]

    def _document_happenings(self) -> list[DocumentHappening]:
        return [h for h in self.happenings if isinstance(h, DocumentHappening)]

    def provider_seed(self, provider: str) -> ProviderSeed | None:
        """The provider's own seed, when the scenario gives it one."""
        return next((s for s in self.provider_seeds if s.provider == provider), None)

    def _refuse_unknown_places(self) -> None:
        """Every channel a happening names is seeded, and every post it names was seeded or posted before it."""
        names = {(c.provider, c.name) for c in self.channels if c.name is not None}
        if len(names) != len([c for c in self.channels if c.name is not None]):
            raise ValueError("two seeded channels share a name")
        posts = {(c.provider, p.key) for c in self.channels for p in _every_post(c.history) if p.key is not None}
        messaging = [h for h in self.happenings if not isinstance(h, TicketHappening | DocumentHappening)]
        for happening in sorted(messaging, key=lambda h: h.after):
            channel = happening.channel if isinstance(happening, (PersonPosts, PersonReacts, PersonCommands)) else None
            if isinstance(happening, PersonJoins):
                channel = happening.channel
            if channel is not None and channel != GENERAL_CHANNEL and (happening.provider, channel) not in names:
                raise ValueError(f"no seeded channel #{channel} on {happening.provider}")
            named: list[str] = []
            if isinstance(happening, (PersonEdits, PersonDeletes)):
                named.append(happening.post)
            if isinstance(happening, PersonReacts) and happening.post is not None:
                named.append(happening.post)
            if isinstance(happening, PersonPosts) and happening.in_thread_of is not None:
                named.append(happening.in_thread_of)
            for key in named:
                if (happening.provider, key) not in posts:
                    raise ValueError(
                        f"no post {key!r} on {happening.provider} before {happening.kind} at {happening.after}"
                    )
            if isinstance(happening, PersonPosts) and happening.key is not None:
                if (happening.provider, happening.key) in posts:
                    raise ValueError(f"two posts share the key {happening.key!r}")
                posts.add((happening.provider, happening.key))

    def _places_resolve(self) -> None:
        """Every space a document names exists, every changed document exists, and no title is seeded twice in
        one provider."""
        spaces = [(s.provider, s.name) for s in self.spaces]
        if len(spaces) != len(set(spaces)):
            raise ValueError("two shared spaces of one provider share a name")
        for document in self.documents:
            if document.space is not None and (document.provider, document.space) not in spaces:
                raise ValueError(f"document {document.title!r} is in space {document.space!r}, which is not seeded")
        titles = [(d.provider, d.title) for d in self.documents]
        if len(titles) != len(set(titles)):
            raise ValueError("two seeded documents of one provider share a title")
        for happening in self._document_happenings():
            self.happening_document(happening)

    def _refuse_tell(self, relayed: Relayed) -> None:
        """A tell the agent could write without hearing it from `said_by`, or that `said_by` can never say."""
        tell = relayed.tell.casefold()
        if relayed.said_by == relayed.to:
            raise ValueError(f"a relayed tell goes from one person to another; {relayed.said_by} is both")
        elsewhere = [("the goal", self.goal)]
        elsewhere += [(f"direction {i + 1}", d.text) for i, d in enumerate(self.directions)]
        elsewhere += [(f"seeded ticket {t.title!r}", f"{t.title} {t.body}") for t in self.tickets]
        elsewhere += [(f"a comment on {t.title!r}", c.text) for t in self.tickets for c in t.comments]
        elsewhere += [(f"the {s.provider} seed", s.body) for s in self.provider_seeds]
        elsewhere += [
            (f"seeded document {d.title!r}", " ".join([d.title, d.text, *(cell for row in d.rows for cell in row)]))
            for d in self.documents
        ]
        elsewhere += [
            (f"the change to {h.document!r}", h.action.append)
            for h in self._document_happenings()
            if isinstance(h.action, Edited)
        ]
        elsewhere += [
            (f"{h.person}'s comment on {h.ticket!r}", h.action.text)
            for h in self._ticket_happenings()
            if isinstance(h.action, Comments)
        ]
        for person in self.people:
            if person.key == relayed.said_by:
                continue
            said = [r.said for r in person.reply.replies] if isinstance(person.reply, Scripted) else []
            elsewhere += [(f"what {person.key} says or knows", text) for text in [*said, *person.facts]]
        for where, text in elsewhere:
            if tell in text.casefold():
                raise ValueError(
                    f"the tell {relayed.tell!r} appears in {where}, so the agent could write it without hearing "
                    f"it from {relayed.said_by}"
                )
        speaker = next(p for p in self.people if p.key == relayed.said_by)
        reply = speaker.reply
        if isinstance(reply, Silent):
            raise ValueError(f"{relayed.said_by} is silent and can never say the tell {relayed.tell!r}")
        own = [r.said for r in reply.replies] if isinstance(reply, Scripted) else speaker.facts
        if not any(tell in text.casefold() for text in own):
            source = "scripted reply" if isinstance(reply, Scripted) else "fact"
            raise ValueError(f"no {source} of {relayed.said_by} holds the tell {relayed.tell!r}")


def _every_post(posts: list[SeededPost]) -> list[SeededPost]:
    """Posts and their thread replies, depth first."""
    return [found for post in posts for found in (post, *_every_post(post.replies))]


class WrittenScenario(_ScenarioBody):
    """A scenario as its file states it. With no `starts_at` it starts at the moment the run does."""

    starts_at: AwareDatetime | None = Field(default=None, description="None: the moment the run starts")

    def starting(self, now: datetime) -> Scenario:
        """The scenario a run plays: its own `starts_at`, or `now`, the moment the run starts, when it has none."""
        return Scenario.model_validate({**self.model_dump(), "starts_at": self.starts_at or now})


class Seed(WrittenScenario):
    """What a standing world (`minutehand serve`) starts from: a scenario file's people, tickets, documents,
    fates and directions, with nothing to achieve. The goal and the expectations may be left out, so a scenario
    file without them loads, and a whole scenario file loads too: its expectations are what the checks hold the
    world to when they are asked. The owner, when not named, is the first person; a seed names at least one,
    since every provider writes the world as someone."""

    name: str = Field(default="world", pattern=r"^[a-z][a-z0-9_]*$")
    goal: str = Field(default="", description="Handed to nobody: a standing world has no run loop")
    people: list[Person] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _owner_is_the_first_person(cls, written: object) -> object:
        if not isinstance(written, dict) or "owner" in written or "people" not in written:
            return written
        people: object = written["people"]
        if not isinstance(people, list) or not people:
            return written
        first: object = people[0]
        if isinstance(first, Person):
            return {**written, "owner": first.key}
        if isinstance(first, dict) and "key" in first:
            return {**written, "owner": first["key"]}
        return written


class Scenario(_ScenarioBody):
    """A scenario as a run plays it and records it: its start is an instant."""

    starts_at: AwareDatetime = Field(description="Simulated; every other moment is an offset from it")

    def starting(self, now: datetime) -> Scenario:
        """Itself: its start is already an instant."""
        return self

    @property
    def deadline(self) -> datetime | None:
        return self.starts_at + self.deadline_after if self.deadline_after else None
