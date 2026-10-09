"""A scenario: the goal, the people, and what already exists in the world.

A scenario holds no absolute dates except `starts_at`. Every other moment is an
offset from it, so the same file replays on any day and under any seed.

A scenario file may leave `starts_at` out (`WrittenScenario`): it then starts at the
moment the run starts, for an agent that reads the real clock. A run resolves it once,
before anything is played, into the `Scenario` it plays and records, so every sample,
fork and rerun of that run starts at the same instant.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator, model_validator

from minutehand.domain.assessments import IntegrityCheck, Rule, refuse_repeated_rules, refuse_unknown_people
from minutehand.domain.common import (
    ProviderKey,
    SigningSecret,
    Window,
)
from minutehand.domain.memory import SeededMemory
from minutehand.domain.model import Model
from minutehand.domain.services import Service, refuse_unknown_responders


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
    """How long a person takes to answer, in simulated time, when they declare no `reply_within`: a delay drawn
    from this range, then pushed past their absences and outside their working hours."""

    shortest: timedelta = timedelta(hours=6)
    longest: timedelta = timedelta(hours=66)

    @model_validator(mode="after")
    def _ordered(self) -> DelayRange:
        if self.longest < self.shortest:
            raise ValueError("longest is shorter than shortest")
        return self


class Reminded(Model):
    """What a follow-up does to an answer this person owes: a moment is drawn again from `sooner_within`, of their
    available time after the follow-up, and the answer moves there when that is sooner. Never later."""

    sooner_within: Window


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


class Intent(StrEnum):
    """What a person does with the message a step of their script answers. The words are the model's, written from
    the step's facts, the person's own facts, voice and helpfulness."""

    ANSWER = "answer"  # answers it, from the step's facts
    DECLINE = "decline"  # says it is not theirs to answer, and does not answer it
    ASK_BACK = "ask_back"  # asks a question of their own before they will answer
    DEFER = "defer"  # says they will come back to it, with what the step's facts say about when or why


class ScriptedReply(Model):
    """One step of a person's script: what they know, decide or do in answer to the nth message they receive.

    By default the words are a model's, written from `facts` (what this reply carries) and `intent`, in the
    person's voice; the step fixes what is said, never how. `verbatim` is the rare exact string, for a test that
    needs those words and no others. `press` uses a control on the message instead of writing back."""

    to_ask: int = Field(ge=1)
    facts: list[str] = Field(
        default=[], description="What this reply carries, as facts the model writes from; empty: their own facts"
    )
    intent: Intent = Intent.ANSWER
    verbatim: str | None = Field(
        default=None, min_length=1, description="These exact words and no model: the rare opt-in"
    )
    press: ScriptedPress | None = None
    within: Window | None = Field(
        default=None, description="When this reply lands, in place of the person's own `reply_within` or delay"
    )

    @model_validator(mode="after")
    def _one_way(self) -> ScriptedReply:
        if self.press is not None and (self.facts or self.verbatim is not None or self.intent is not Intent.ANSWER):
            raise ValueError("a scripted step that presses a control writes nothing: no facts, verbatim or intent")
        if self.verbatim is not None and (self.facts or self.intent is not Intent.ANSWER):
            raise ValueError("a verbatim step says exactly its words: no facts or intent beside them")
        return self

    @property
    def written(self) -> bool:
        """Whether a model writes this step's words."""
        return self.press is None and self.verbatim is None

    @property
    def said(self) -> list[str]:
        """Everything this step puts into the world in the person's words, or the facts the model writes it from."""
        if self.press is not None:
            return [f.value for f in self.press.form]
        if self.verbatim is not None:
            return [self.verbatim]
        return list(self.facts)


class Helpfulness(StrEnum):
    """What this person does with a question."""

    FULL = "full"  # answers it
    PARTIAL = "partial"  # answers part and leaves the rest
    ASKS_BACK = "asks_back"  # replies with a question of their own
    DECLINES = "declines"  # says it is not theirs, names nobody
    MISTAKEN = "mistaken"  # answers confidently from an out-of-date fact


HISTORY_TURNS = 30
"""The messages a model is shown word for word when a person declares no `history_turns`."""


class Speaks(Model):
    """How a person's words are written, by a model, whatever plan they follow: what they do with a question, how
    they write, and which model writes it. Their facts and stale facts are the person's own (`Person`)."""

    delay: DelayRange = DelayRange()
    helpfulness: Helpfulness = Helpfulness.FULL
    voice: str | None = Field(default=None, description="How they write: terse, formal, chatty")
    model: str | None = Field(default=None, description="None uses the run's default model")
    temperature: float = Field(default=0.6, ge=0, le=2)
    history_turns: int = Field(
        default=HISTORY_TURNS,
        ge=1,
        description="How many messages of what they can see the model is shown word for word, the newest; older ones "
        "reach it as one summary, written by a model once and kept",
    )


class Answers(Speaks):
    """No plan: every message is answered by a model from this person's facts, or left unanswered when the model
    reads it as needing no answer.

    Each reply is stored with the world the first time it is written; a rerun or a fork replays it, so only a new
    ask costs a model call."""

    kind: Literal["answers"] = "answers"


class ScriptedDecision(Model):
    """What this person decides on an item waiting on them in the agent's own product (`domain.inboxes`): the nth
    such item (`to_item`), or every one (`to_item` None), in one inbox or in any. A decision naming its item wins
    over one for every item. What they give with it (a reason, an answer) is written by a model from `facts`,
    unless `inputs` fixes the words."""

    to_item: int | None = Field(default=None, ge=1, description="The nth item waiting on them, from 1; None: every one")
    inbox: ProviderKey | None = Field(
        default=None, description="The inbox's `name`; None: any. With one, `to_item` counts that inbox's items only"
    )
    decision: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="The name of one of the inbox's decisions")
    facts: list[str] = Field(default=[], description="Why they decide so, as facts the model writes each input from")
    inputs: dict[str, str] = Field(
        default={}, description="Each input the decision takes, by its name, in exact words: no model for those"
    )
    within: Window | None = Field(
        default=None, description="When this decision is made, in place of the person's own `reply_within` or delay"
    )


class AfterScript(StrEnum):
    """What a scripted person does once every step of their script is used."""

    ANSWERS = "answers"  # goes on conversing: a model answers from their facts, as for `Answers`
    SILENT = "silent"  # says nothing more, by the author's word


class Scripted(Speaks):
    """A plan of what this person knows, decides and does, step by step, in answer to the agent's messages. The
    words are a model's, written from each step's facts in the person's voice, unless a step is `verbatim`.

    A message whose number has no step, before the last step, gets no answer: the script says so. Once the steps
    are used, the person keeps conversing (`then: answers`, the default), a model writing from their facts, until
    the run ends; `then: silent` is the author's word that they say nothing more."""

    kind: Literal["scripted"] = "scripted"
    replies: list[ScriptedReply] = []
    then: AfterScript = AfterScript.ANSWERS
    decisions: list[ScriptedDecision] | None = Field(
        default=None,
        description="What they decide on items waiting on them in the agent's own product, after their delay like "
        "a reply. None says nothing: with `then: answers` a model decides every item, and with `then: silent` a run "
        "whose agent declares an inbox they can receive items in is refused. `[]` leaves every item to `then`",
    )
    presses_every: ScriptedPress | None = Field(
        default=None,
        description="On every message the agent sends them that carries a control reading this label (an approval "
        "card's Approve), they press it after their delay, however many there are; a scripted reply to that ask "
        "is used instead when there is one",
    )

    @model_validator(mode="after")
    def _steps_once(self) -> Scripted:
        asks = [r.to_ask for r in self.replies]
        twice = sorted({n for n in asks if asks.count(n) > 1})
        if twice:
            raise ValueError(f"two scripted steps answer ask {', '.join(str(n) for n in twice)}")
        return self

    @property
    def last_ask(self) -> int:
        """The number of the last ask the script has a step for; 0 with none."""
        return max((r.to_ask for r in self.replies), default=0)


class FactChange(Model):
    """What a person knows from a moment on, by the author's word: their facts (and stale facts, when given) are
    these from `after` the scenario's start. Nothing else changes what they know; what they said before stays said."""

    after: timedelta = Field(gt=timedelta(0), description="Offset from the scenario's start")
    facts: list[str] = Field(description="Every fact they hold from then on")
    stale_facts: list[str] | None = Field(default=None, description="Their stale facts from then on; None: unchanged")


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


class Take(Model):
    """A transition a person is pinned to take on an item pending on them in a provider the people engine plays
    (`Scenario.transitions_on`, docs/design-transitions.md), instead of the one a model picks; and, with `after`,
    when. The words it carries are a model's from `facts`, or exactly `verbatim`."""

    provider: ProviderKey
    nth: int | None = Field(
        default=None, ge=1, description="The nth item pending on them in that provider, from 1; None: every one"
    )
    take: str = Field(
        min_length=1, description="The offer, by its name or the state it reaches, in any case: 'Done', 'declined'"
    )
    after: timedelta | None = Field(
        default=None,
        ge=timedelta(0),
        description="Exactly this long after the item became pending on them; None: drawn as their answers are",
    )
    verbatim: str | None = Field(
        default=None, min_length=1, description="Exact words for the offer's text (its comment); no model is called"
    )
    facts: list[str] = Field(default=[], description="What its words carry, which a model writes them from")

    @model_validator(mode="after")
    def _one_source_of_words(self) -> Take:
        if self.verbatim is not None and self.facts:
            raise ValueError(f"a take of {self.take!r} gives `verbatim` words or `facts` to write them from, not both")
        return self


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
    reply_within: Window | None = Field(
        default=None,
        description="When their answers land: drawn within this much of their available time after each ask; "
        "None: their reply's `delay`. A scripted step or decision's own `within` wins",
    )
    fact_changes: list[FactChange] = Field(
        default=[], description="What they know from later moments on, replacing `facts` (and `stale_facts`) then"
    )
    reminded: Reminded | None = Field(
        default=None,
        description="What a follow-up does to an answer they owe; None: nothing, the answer lands when it was drawn",
    )
    working_hours: WorkingHours | None = None
    absences: list[Absence] = []
    early_follow_ups: int = Field(
        default=2,
        ge=0,
        description="How many follow-ups on one ask this person takes, each sent before their answer was due, "
        "before it is nagging",
    )
    credential: SigningSecret | None = Field(
        default=None,
        description="How Minutehand signs in to the agent's own product as this person, to read what waits on them "
        "and decide it (`AgentUnderTest.inboxes`, `{credential}` in `as_person`): generated per run and handed to the "
        "agent's command in its variable, or read from Minutehand's own environment. Never stored",
    )
    takes: list[Take] = Field(
        default=[],
        description="Transitions this person is pinned to take on items pending on them in a provider the people "
        "engine plays (`Scenario.transitions_on`); without one, a model picks among the legal ones",
    )

    def knows_at(self, at: datetime, starts_at: datetime) -> tuple[list[str], list[str]]:
        """Their facts and stale facts at `at`: the latest `fact_changes` reached by then, else their own."""
        facts, stale = list(self.facts), list(self.stale_facts)
        for change in sorted(self.fact_changes, key=lambda c: c.after):
            if starts_at + change.after <= at:
                facts = list(change.facts)
                if change.stale_facts is not None:
                    stale = list(change.stale_facts)
        return facts, stale


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


class Commented(Model):
    """The person writes a comment on the document, not in it."""

    kind: Literal["commented"] = "commented"
    text: str = Field(min_length=1)


class FieldSet(Model):
    """The person sets one field of a record-shaped document (a database row's property) to a value, written as
    text the provider reads by the field's type: an option's name, a number, a date, `true` or `false`, or
    comma-separated names for a field that holds several."""

    kind: Literal["field_set"] = "field_set"
    field: str = Field(min_length=1)
    value: str


DocumentAction = Annotated[
    Edited | Renamed | Moved | Shared | Trashed | Commented | FieldSet, Field(discriminator="kind")
]


class DocumentHappening(Model):
    """A person changes a seeded document at a moment, with no agent involved: edits, renames, moves, shares,
    trashes, comments on it, or sets one of its fields.

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


class PersonAddsAgent(_MessagingHappening):
    """A person adds the agent to a conversation it is not in: invites its bot to a channel (Slack), or installs
    its app in a team, a chat, or with no channel their own chat with it (Teams)."""

    kind: Literal["adds_agent"] = "adds_agent"
    channel: ChannelName | None = None


class PersonCommands(_MessagingHappening):
    """A person runs one of the agent's commands (a Slack slash command), in a channel or their DM with it."""

    kind: Literal["commands"] = "commands"
    command: str = Field(pattern=r"^/[a-z0-9_-]+$")
    text: str = ""
    channel: ChannelName | None = None


MessagingHappening = (
    PersonPosts
    | PersonEdits
    | PersonDeletes
    | PersonReacts
    | PersonJoins
    | PersonAddsAgent
    | PersonOpensAgent
    | PersonCommands
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
    """What happens to a ticket the agent hands to a person: it reaches a state, or its assignee deletes it."""

    assignee: str = Field(description="Person.key")
    becomes: TicketState | None = Field(default=None, description="The state it reaches; None when it is deleted")
    deleted: bool = Field(default=False, description="Its assignee deletes it instead, through `DeletesTickets`")
    after: timedelta

    @model_validator(mode="after")
    def _one_outcome(self) -> Self:
        if (self.becomes is None) == (not self.deleted):
            raise ValueError("a ticket's fate is a state it becomes or its deletion; give exactly one")
        return self


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
    """The agent passed on what a person told it: a message from the agent to `to` holds every value of `holding`,
    a fact the scenario's author gave `said_by` to say, matched in any case.

    The values are facts, not the person's wording: a model writes the person's words, and the check reads whether
    the fact reached `to`. A scenario is refused when its goal, a direction, a seeded ticket or document, or anyone
    else's script or facts holds a value, and when `said_by` could never say it: silent, or none of their script's
    steps or facts holds it. In a run, the agent's message must come after a reply of `said_by`'s that carries the
    value (in its words, or in the facts its step was written from); an agent message that held it before means the
    agent did not hear it from them, and no message counts."""

    kind: Literal["relayed"] = "relayed"
    said_by: str = Field(description="Person.key of whoever says it")
    to: str = Field(description="Person.key the agent must pass it on to")
    holding: list[str] = Field(min_length=1, description="Fact values only `said_by`'s answer carries")

    @field_validator("holding")
    @classmethod
    def _not_blank(cls, values: list[str]) -> list[str]:
        if any(not v.strip() for v in values):
            raise ValueError("a relayed value is a phrase, not blank")
        return values


class DocumentCreated(Bound):
    """The agent created a document: with these words in its title, in this shared place, owned by this person, and
    holding these words as it last read (by `by`, when given). One document counts once, however often it changed."""

    kind: Literal["document_created"] = "document_created"
    provider: ProviderKey | None = Field(default=None, description="None matches any document provider")
    titled: list[str] = Field(default=[], description="Words the title must contain, any case")
    space: str | None = Field(default=None, description="SharedSpace.name it must be in; None matches anywhere")
    owner: str | None = Field(default=None, description="Person.key who must own it; None matches any owner")
    holds: list[str] = Field(
        default=[], description="Words its text must hold, any case, as it last read: a tell only one answer carries"
    )


class DocumentShared(Bound):
    """The agent gave this person access to a document, at least as `role`."""

    kind: Literal["document_shared"] = "document_shared"
    person: str = Field(description="Person.key")
    titled: list[str] = Field(default=[], description="Words the document's title must contain, any case")
    role: AccessRole = Field(default=AccessRole.READER, description="The least access that counts")


class FileRemoved(Bound):
    """The agent removed a file from a watched folder of its machine (`AgentUnderTest.watches`): moved it away or
    deleted it. `at_most: 0` says it must not."""

    kind: Literal["file_removed"] = "file_removed"
    path: str = Field(min_length=1, description="A glob the file's absolute path must match, e.g. '*/Downloads/*.zip'")


class ToolCalled(Bound):
    """The agent called this tool on an MCP server (`ToolCallSnapshot`), with these words in its arguments."""

    kind: Literal["tool_called"] = "tool_called"
    tool: str = Field(min_length=1)
    server: str | None = Field(default=None, description="The server's host, or its relay's --name; None: any")
    mentions: list[str] = Field(default=[], description="Words the arguments must contain, any case")
    succeeded: bool | None = Field(default=None, description="True: only calls the server did not answer with an error")


Expectation = Annotated[
    PersonAsked
    | TicketCreated
    | TicketDeleted
    | TicketInState
    | Relayed
    | DocumentCreated
    | DocumentShared
    | FileRemoved
    | ToolCalled,
    Field(discriminator="kind"),
]


class MachineCommand(Model):
    """Something that happens to the agent's own machine at a moment: a file appears, a cache grows, a repository
    goes stale. Minutehand runs the command then, in the folder the run was started from, with MINUTEHAND_NOW set
    to the simulated moment (ISO 8601), so it can stamp what it makes; nobody is woken, as on a real machine."""

    after: timedelta = Field(ge=timedelta(0), description="Offset from the scenario's start")
    argv: list[str] = Field(min_length=1)
    said: str = Field(min_length=1, description="What it does, in the run's record")
    limit: timedelta = Field(default=timedelta(seconds=60), gt=timedelta(0), description="Real time it may take")


class PlannedBy(StrEnum):
    """How the agent asked for one of its own wakes: which of them a dispatch rule is about."""

    REPORTED = "reported"  # `AgentReport.next_wake`
    BOOKED = "booked"  # a booking with a scheduler provider
    POLLED = "polled"  # the declared rhythm of a `Polled` agent


class DispatchFault(StrEnum):
    """What goes wrong delivering one of the agent's own wakes, as real schedulers go wrong."""

    LATE = "late"  # delivered `by` after the moment it was asked for
    TWICE = "twice"  # delivered at its moment, and again `by` after: an at-least-once queue
    DROPPED = "dropped"  # never delivered


class DispatchRule(Model):
    """A fault in delivering the agent's own wakes: the nth of one kind to fall due, or every one.

    Only the agent's own wakes are covered: when a person answers is their `reply`, and a ticket's pace its fate."""

    wakes: PlannedBy
    nth: int | None = Field(default=None, ge=1, description="The nth wake of that kind to fall due, from 1; None: each")
    fault: DispatchFault
    by: timedelta | None = Field(
        default=None, description="How late, or how long after the first the second delivery comes; not for dropped"
    )

    @property
    def which(self) -> str:
        """The wakes it is about, as a reader says it: "the 2nd reported wake", "each polled wake"."""
        if self.nth is None:
            return f"each {self.wakes.value} wake"
        n = self.nth
        suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"the {n}{suffix} {self.wakes.value} wake"

    @model_validator(mode="after")
    def _deliverable(self) -> Self:
        if self.fault is DispatchFault.DROPPED and self.by is not None:
            raise ValueError("a dropped wake is never delivered, so it takes no `by`")
        if self.fault is not DispatchFault.DROPPED and (self.by is None or self.by <= timedelta(0)):
            raise ValueError(f"a {self.fault.value} wake needs `by`, a duration after the moment it was asked for")
        return self


class ExpectedOutcome(StrEnum):
    """The verdict a scenario is written to reach (`domain.run.VerdictKind`): a scenario whose point is that nobody
    answers may be written to end unfinished, and one that shows a known failure to end failed."""

    PASSED = "passed"
    FAILED = "failed"
    UNFINISHED = "unfinished"
    NOT_JUDGED = "not_judged"


_RATE = re.compile(r"^\s*(?P<op>>=|<=|==|>|<)\s*(?P<value>[0-9]*\.?[0-9]+)\s*$")


class Comparison(StrEnum):
    AT_LEAST = ">="
    MORE_THAN = ">"
    AT_MOST = "<="
    LESS_THAN = "<"
    EXACTLY = "=="


class Rate(Model):
    """A share of a scenario's samples, compared: `>= 0.9` is at least nine in ten."""

    op: Comparison
    value: float = Field(ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _written(cls, said: object) -> object:
        if not isinstance(said, str):
            return said
        found = _RATE.match(said)
        if found is None:
            raise ValueError(f'{said!r} is not a rate: write a comparison and a share, e.g. ">= 0.9"')
        return {"op": found["op"], "value": float(found["value"])}

    def met(self, share: float) -> bool:
        match self.op:
            case Comparison.AT_LEAST:
                return share >= self.value
            case Comparison.MORE_THAN:
                return share > self.value
            case Comparison.AT_MOST:
                return share <= self.value
            case Comparison.LESS_THAN:
                return share < self.value
            case Comparison.EXACTLY:
                return share == self.value

    def __str__(self) -> str:
        return f"{self.op.value} {self.value:g}"


class OutcomeRate(Model):
    """The share of a scenario's samples (`minutehand run-all --samples N`) that must reach one verdict:
    `{passed: ">= 0.9"}`. Exactly one verdict is named."""

    passed: Rate | None = None
    failed: Rate | None = None
    unfinished: Rate | None = None
    not_judged: Rate | None = None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if len(self.named()) != 1:
            raise ValueError("an outcome rate names exactly one verdict: passed, failed, unfinished or not_judged")
        return self

    def named(self) -> list[tuple[ExpectedOutcome, Rate]]:
        said = [
            (ExpectedOutcome.PASSED, self.passed),
            (ExpectedOutcome.FAILED, self.failed),
            (ExpectedOutcome.UNFINISHED, self.unfinished),
            (ExpectedOutcome.NOT_JUDGED, self.not_judged),
        ]
        return [(outcome, rate) for outcome, rate in said if rate is not None]

    @property
    def outcome(self) -> ExpectedOutcome:
        return self.named()[0][0]

    @property
    def rate(self) -> Rate:
        return self.named()[0][1]


class _ScenarioBody(Model):
    """Everything a scenario says but when it starts."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    goal: str = Field(description="The text handed to the agent, verbatim")
    owner: str = Field(description="Person.key of whoever gave the goal")
    deadline_after: timedelta | None = None
    max_wakes: int | None = Field(
        default=None,
        ge=1,
        description="The most wakes a run plays before it stops; absent, sized from the deadline and the agent's "
        "rhythm (`domain.run.wake_limit`)",
    )
    protected_names: list[str] = Field(default=[], description="Names the agent must spell exactly as given")
    people: list[Person]
    tickets: list[SeededTicket] = []
    documents: list[SeededDocument] = []
    spaces: list[SharedSpace] = []
    sign_ins: list[SignIn] = []
    ticket_fates: list[TicketFate] = []
    transitions_on: list[ProviderKey] = Field(
        default=[],
        description="Providers whose people the people engine plays (docs/design-transitions.md): what waits on a "
        "person there is pending on them, and at their moment they take one of its legal transitions",
    )
    services: list[Service] = Field(
        default=[],
        description="Hosts no provider fakes that the agent files things with and people respond through: each "
        "item's state held and moved along a machine, what the agent is answered rendered from it (docs/services.md)",
    )
    directions: list[Direction] = []
    channels: list[SeededChannel] = Field(default=[], description="Conversations that exist when the run starts")
    happenings: list[Happening] = Field(
        default=[], description="What people do by themselves, unprompted, at moments the scenario sets"
    )
    provider_seeds: list[ProviderSeed] = Field(
        default=[], description="Each provider's own seed beyond people, tickets and documents; one per provider"
    )
    expect: list[Expectation] = Field(default=[], description="What must be true of the world for this run to be right")
    assess: list[Rule] = Field(
        default=[],
        description="The team's rules for how the agent behaves in this scenario, over the facts of the run "
        "(`docs/assessments.md`); one with the id of an agent file's rule replaces it",
    )
    assess_off: list[str] = Field(
        default=[], description="Ids of the agent file's rules this scenario does not judge by"
    )
    fail_on_integrity: list[IntegrityCheck] = Field(
        default=[],
        description="Integrity facts that fail the run, besides those the agent file names, when they are found (`around_proxy`, "
        "`agent_contract_changed`, `unmatched_call`); unnamed, each is stated as `review` and never changes the verdict",
    )
    expect_outcome: ExpectedOutcome | OutcomeRate = Field(
        default=ExpectedOutcome.PASSED,
        description="The verdict this scenario is written to reach: every sample's, or, as a rate, the share of its "
        'samples that must (`{passed: ">= 0.9"}`); `minutehand run-all` exits 1 when it is missed',
    )
    machine: list[MachineCommand] = Field(
        default=[], description="What happens to the agent's own machine, at moments the scenario sets"
    )
    dispatch: list[DispatchRule] = Field(
        default=[],
        description="Faults in delivering the agent's own wakes: late, twice or dropped. Without any, each is "
        "delivered at the moment it was asked for",
    )
    memory: list[SeededMemory] = Field(
        default=[],
        description="What the agent's memory (`minutehand.agent.store`) holds when the run starts: each key and its "
        "value. Nothing else is in it; the agent's own production database is never read",
    )

    @model_validator(mode="after")
    def _one_value_per_key(self) -> Self:
        keys = [(m.collection, m.key) for m in self.memory]
        twice = sorted({f"{c}/{k}" for c, k in keys if keys.count((c, k)) > 1})
        if twice:
            raise ValueError(f"the memory seeds a key twice: {', '.join(twice)}")
        return self

    @model_validator(mode="after")
    def _one_rule_per_wake(self) -> Self:
        said = [(r.wakes, r.nth) for r in self.dispatch]
        if len(said) != len(set(said)):
            raise ValueError("two dispatch rules name the same wake; a rule for the nth wins over one for each")
        return self

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
        named += [e.person for e in self.expect if isinstance(e, DocumentShared)]
        named += [e.owner for e in self.expect if isinstance(e, DocumentCreated) and e.owner is not None]
        named += [h.action.to for h in self._on_tickets() if isinstance(h.action, Reassigns) and h.action.to]
        named += [k for d in self.documents for k in (d.owner, d.modified_by) if k is not None]
        named += [a.person for d in self.documents for a in d.shared_with]
        named += [a.person for s in self.spaces for a in s.members]
        named += [s.person for s in self.sign_ins if s.person is not None]
        named += [h.action.access.person for h in self._on_documents() if isinstance(h.action, Shared)]
        missing = sorted(set(named) - known)
        if missing:
            raise ValueError(f"no such person: {', '.join(missing)}")
        for happening in self._on_tickets():
            self.happening_ticket(happening)
        self._places_resolve()
        seeded = [s.provider for s in self.provider_seeds]
        twice = sorted({p for p in seeded if seeded.count(p) > 1})
        if twice:
            raise ValueError(f"more than one provider seed for {', '.join(twice)}")
        for relayed in (e for e in self.expect if isinstance(e, Relayed)):
            self._refuse_tell(relayed)
        refuse_repeated_rules(self.assess)
        refuse_unknown_people(self.assess, keys)
        refuse_unknown_responders(self.services, keys)
        self._takes_resolve()
        return self

    def played(self) -> list[ProviderKey]:
        """Every provider the people engine plays: those `transitions_on` names, and every declared service."""
        return [*self.transitions_on, *(s.key for s in self.services)]

    def _takes_resolve(self) -> None:
        """A pinned transition is on a provider the engine plays, and names one item, or every one, once."""
        played = self.played()
        if len(played) != len(set(played)):
            raise ValueError("transitions_on names a provider twice, or one a declared service is named")
        for person in self.people:
            said: list[tuple[str, int | None]] = []
            for take in person.takes:
                if take.provider not in played:
                    raise ValueError(
                        f"{person.key} takes {take.take!r} on {take.provider}, which the people engine does not play: "
                        f"add it to `transitions_on`, or declare it under `services`"
                    )
                said.append((take.provider, take.nth))
            twice = sorted({f"{p} {n or 'every'}" for p, n in said if said.count((p, n)) > 1})
            if twice:
                raise ValueError(f"{person.key} pins two takes on the same item: {', '.join(twice)}")

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

    def _on_tickets(self) -> list[TicketHappening]:
        return [h for h in self.happenings if isinstance(h, TicketHappening)]

    def _on_documents(self) -> list[DocumentHappening]:
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
            places = (PersonPosts, PersonReacts, PersonCommands, PersonAddsAgent)
            channel = happening.channel if isinstance(happening, places) else None
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
        for happening in self._on_documents():
            self.happening_document(happening)

    def _refuse_tell(self, relayed: Relayed) -> None:
        """A value the agent could write without hearing it from `said_by`, or that `said_by` can never say."""
        if relayed.said_by == relayed.to:
            raise ValueError(f"a relayed fact goes from one person to another; {relayed.said_by} is both")
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
            for h in self._on_documents()
            if isinstance(h.action, Edited)
        ]
        elsewhere += [
            (f"{h.person}'s comment on {h.document!r}", h.action.text)
            for h in self._on_documents()
            if isinstance(h.action, Commented)
        ]
        elsewhere += [
            (f"{h.person}'s {h.action.field} on {h.document!r}", h.action.value)
            for h in self._on_documents()
            if isinstance(h.action, FieldSet)
        ]
        elsewhere += [
            (f"{h.person}'s comment on {h.ticket!r}", h.action.text)
            for h in self._on_tickets()
            if isinstance(h.action, Comments)
        ]
        for person in self.people:
            if person.key == relayed.said_by:
                continue
            elsewhere += [(f"what {person.key} says or knows", text) for text in _knowable(person)]
        speaker = next(p for p in self.people if p.key == relayed.said_by)
        for value in relayed.holding:
            told = value.casefold()
            for where, text in elsewhere:
                if told in text.casefold():
                    raise ValueError(
                        f"the relayed fact {value!r} appears in {where}, so the agent could write it without hearing "
                        f"it from {relayed.said_by}"
                    )
            if isinstance(speaker.reply, Silent):
                raise ValueError(f"{relayed.said_by} is silent and can never say {value!r}")
            if not any(told in text.casefold() for text in _knowable(speaker)):
                raise ValueError(f"no step of {relayed.said_by}'s script and none of their facts holds {value!r}")


def _knowable(person: Person) -> list[str]:
    """Everything a person may say: their script's words and step facts, their facts at any moment, their stale
    facts, and the inputs and reasons of their scripted decisions."""
    said: list[str] = [*person.facts, *person.stale_facts]
    said += [f for c in person.fact_changes for f in [*c.facts, *(c.stale_facts or [])]]
    if isinstance(person.reply, Scripted):
        said += [text for r in person.reply.replies for text in r.said]
        for d in person.reply.decisions or []:
            said += [*d.facts, *d.inputs.values()]
    return said


def _every_post(posts: list[SeededPost]) -> list[SeededPost]:
    """Posts and their thread replies, depth first."""
    return [found for post in posts for found in (post, *_every_post(post.replies))]


class WrittenScenario(_ScenarioBody):
    """A scenario as its file states it. With no `starts_at` it starts at the moment the run does."""

    starts_at: AwareDatetime | None = Field(default=None, description="None: the moment the run starts")
    seed: int | None = Field(
        default=None,
        description="Where every draw of the run comes from (each person's reply moments); None: one derived from the "
        "scenario's name. `minutehand run --seed` overrides it, and the run records the one it played",
    )

    @model_validator(mode="after")
    def _dates_read(self) -> Self:
        """Every `{{start...}}` in the text names a moment: refused at load, not when a run begins."""
        _dated(self.model_dump(), self.starts_at or datetime(2000, 1, 1, tzinfo=UTC))
        return self

    def starting(self, now: datetime) -> Scenario:
        """The scenario a run plays: its own `starts_at`, or `now`, the moment the run starts, when it has none.
        Every `{{start+<ISO 8601 duration>}}` in its text becomes that moment (`DATED`), so a goal can name a date
        two days from a start nobody knows when the file is written."""
        start = self.starts_at or now
        return Scenario.model_validate(_dated({**self.model_dump(), "starts_at": start}, start))


DATED = re.compile(r"\{\{start(?:(?P<sign>[+-])(?P<offset>P[0-9A-Za-z.]+))?(?::(?P<form>date|iso|time))?\}\}")
"""A moment counted from the scenario's start, written in any of its text: `{{start+P2D}}` reads "Thursday 3 September
2026", `{{start+P2D:iso}}` reads "2026-09-03", `{{start+P1DT5H:time}}` reads "Wednesday 2 September 2026, 14:00 UTC".
Resolved once, when the run's start is known (`WrittenScenario.starting`), in UTC."""

_OFFSET: TypeAdapter[timedelta] = TypeAdapter(timedelta)


def _dated(value: object, start: datetime) -> object:
    if isinstance(value, str):
        return DATED.sub(lambda m: _moment(m, start), value)
    if isinstance(value, dict):
        return {k: _dated(v, start) for k, v in value.items()}
    if isinstance(value, list):
        return [_dated(v, start) for v in value]
    return value


def _moment(found: re.Match[str], start: datetime) -> str:
    offset = _OFFSET.validate_python(found["offset"]) if found["offset"] else timedelta(0)
    at = (start + (-offset if found["sign"] == "-" else offset)).astimezone(UTC)
    day = f"{at:%A} {at.day} {at:%B %Y}"
    if found["form"] == "iso":
        return at.date().isoformat()
    if found["form"] == "time":
        return f"{day}, {at:%H:%M} UTC"
    return day


class Seed(WrittenScenario):
    """What a standing world (`minutehand serve`) starts from: a scenario file's people, tickets, documents,
    fates and directions, with nothing to achieve. The goal and the expectations may be left out, so a scenario
    file without them loads, and a whole scenario file loads too: its expectations are what the checks hold the
    world to when they are asked. The owner, when not named, is the first person; a seed names at least one,
    since every provider writes the world as someone."""

    name: str = Field(default="world", pattern=r"^[a-z][a-z0-9_]*$")
    goal: str = Field(default="", description="Handed to nobody: a standing world has no run loop")
    people: list[Person] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_dispatch(self) -> Self:
        if self.machine:
            raise ValueError(
                "machine commands run at moments of the run loop's clock, and a standing world's is driven from "
                "outside: play this scenario with `minutehand run`, or leave `machine` out of the seed"
            )
        if self.dispatch:
            raise ValueError(
                "dispatch rules need the run loop's clock, and a standing world's is driven from outside: play this "
                "scenario with `minutehand run`, or leave `dispatch` out of the seed"
            )
        return self

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


def derived_seed(name: str) -> int:
    """The seed of a scenario that declares none: fixed by its name, so every run of it draws alike."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:4], "big")


class Scenario(_ScenarioBody):
    """A scenario as a run plays it and records it: its start is an instant, and its seed a number."""

    starts_at: AwareDatetime = Field(description="Simulated; every other moment is an offset from it")
    seed: int = Field(
        default=0, description="Where every draw of the run comes from; given none, one derived from the name"
    )

    @model_validator(mode="before")
    @classmethod
    def _seeded(cls, written: object) -> object:
        if not isinstance(written, dict) or "name" not in written or not isinstance(written["name"], str):
            return written
        if "seed" not in written or written["seed"] is None:
            return {**written, "seed": derived_seed(written["name"])}
        return written

    def seeded(self, seed: int | None) -> Scenario:
        """This scenario under another seed; itself when `seed` is None."""
        return self if seed is None else self.model_copy(update={"seed": seed})

    def starting(self, now: datetime) -> Scenario:
        """Itself: its start is already an instant."""
        return self

    @property
    def deadline(self) -> datetime | None:
        return self.starts_at + self.deadline_after if self.deadline_after else None
