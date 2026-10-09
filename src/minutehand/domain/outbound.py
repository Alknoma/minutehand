"""Outbound hosts the agent calls that are not places it keeps state: an email API, a webhook, a search, a page.

None of these is faked. Each is declared in the agent file (or per world in the standing mode) with one mode,
and every call to it is captured with the run:

    outbound:
      - host: api.mail.example
        kind: acknowledge                       # never leaves the machine; answered as declared
        answer: {status: 202, json_body: {id: queued}}
        message: {recipients: ["personalizations[*].to[*].email"], text: ["content[0].value"]}
      - host: search.example
        kind: pass_through                      # sent to the real host; request and answer kept
      - host: "*.weather.example"
        kind: replay                            # answered from an earlier run's recording
        source: {run: 3f2a9c1e07bb}
        on_miss: pass_through
      - host: api.tracker.example
        kind: forward                           # sent to an external emulator (`domain.emulator`)
        emulator: tracker
      - host: api.crm.example
        kind: store                             # what the agent writes is kept as sent and read back unchanged
        collections:
          - {path: /v1/contacts, id: {at: id, format: uuid}, list: {items_at: results}}

A model API cannot also be declared. A host a provider claims can: the provider answers every call it serves, and
a call it says it does not serve (`domain.errors.NotServed`) falls through to the declaration. A host nobody
declares or claims is still refused, unless the run captures unknown hosts (`--capture-unknown`).

A path names a value in a request body read as JSON (or a form, whose fields are its top level): dotted keys,
`[n]` for one item of a list and `[*]` for every item, e.g. `personalizations[*].to[*].email`.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

from minutehand.domain.common import ProviderKey, SigningSecret
from minutehand.domain.model import Model


class UnknownHosts(StrEnum):
    """What becomes of a call to a host no provider claims and nothing declares."""

    REFUSE = "refuse"  # answered 502 and recorded: the default
    READS = "reads"  # a GET, HEAD or OPTIONS passed through and kept; anything else refused as above
    ALL = "all"  # passed through and kept, whatever it is: a first run, to see what an agent calls
    MODEL = "model"  # a read passed through and kept until the host is written to; a write, and every call after
    #                  it, answered by a model standing in for the service, never sent

    def captures(self, method: str) -> bool:
        """Whether a call with this method is passed through and kept rather than refused, before a model answers
        anything of its host."""
        if self is UnknownHosts.ALL:
            return True
        return self in (UnknownHosts.READS, UnknownHosts.MODEL) and method.upper() in READ_METHODS


READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods that read by HTTP's own definition. A read sent as a POST (GraphQL, an RPC) is not one of them."""

BODY_LIMIT = 1024 * 1024
"""Bytes of a text or JSON body kept by default. A longer body is kept up to this and marked truncated."""

BodyPath = Annotated[
    str, Field(min_length=1, pattern=r"^[A-Za-z0-9_\-$@]+(\[(\*|\d+)\])*(\.[A-Za-z0-9_\-$@]+(\[(\*|\d+)\])*)*$")
]
"""A value in a JSON or form body: dotted keys, `[n]` for one list item, `[*]` for every item."""


MESSAGE_ID = "{message_id}"
"""In a string of an acknowledged answer, replaced by an id made for each call: the id a real email API hands
back for the message it queued, which a reply then names (`ReplyDelivery.thread`)."""


class Answer(Model):
    """What an acknowledged call is answered with. With neither body, the JSON `{}`. Any `{message_id}` in its
    strings is replaced by an id made for that call."""

    status: int = Field(default=200, ge=100, le=599)
    headers: dict[str, str] = Field(default={}, description="Sent as given; content-type follows the body")
    json_body: JsonValue = Field(default=None, description="Answered as JSON")
    text: str | None = Field(default=None, description="Answered as text/plain")

    @model_validator(mode="after")
    def _one_body(self) -> Self:
        if self.json_body is not None and self.text is not None:
            raise ValueError("an answer has one body: json_body or text, not both")
        return self


class Route(Model):
    """A different answer for the calls that match: `method` (any when None) and `path`, a shell-style pattern
    over the path without its query (`/v3/mail/*`). The first route that matches answers."""

    method: str | None = None
    path: str = Field(min_length=1)
    answer: Answer


class HtmlAt(Model):
    """A text candidate whose value is HTML, reduced to its text."""

    html: BodyPath


TextAt = BodyPath | HtmlAt


class MessageReading(Model):
    """How a captured send is read as a message to a person, by paths into its request body.

    Recipients are every value found at every path, a string or a list of strings, each split on commas, with
    `Name <address>` read as the address. Each is matched to a scenario person by email, any case, or by
    `handles` (an SMS number, a webhook's user id); one that matches nobody stays in the message as written.
    The text is the first candidate present and not empty."""

    recipients: list[BodyPath] = Field(min_length=1)
    text: list[TextAt] = Field(min_length=1, description="Candidates, first present wins")
    subject: list[BodyPath] = Field(default=[], description="Candidates, first present wins")
    handles: dict[str, str] = Field(default={}, description="A recipient value -> the Person.key it reaches")


REPLY_FIELDS = ("reply_id", "from", "from_name", "to", "subject", "text", "in_reply_to", "sent_at")
"""What a reply's body template may name, each as `{name}` inside a string: the reply's own id, who sends it (their
email and name), who it goes to (the address the agent sent from, else the first it wrote to), the subject, the
text, the id of the message it answers (`thread`), and the simulated moment it is sent (ISO 8601)."""


DEFAULT_REPLY_BODY: dict[str, JsonValue] = {name: "{" + name + "}" for name in REPLY_FIELDS}
"""The body a reply is delivered with when its declaration writes none: every reply field under its own name, the
shape `DeliveredReply` describes (`deliverReply` in `schemas/agent-api.openapi.json`)."""


class DeliveredReply(Model):
    """The default shape of a person's answer delivered to the agent's inbound webhook (`ReplyDelivery.body` None)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    reply_id: str = Field(description="The reply's own id in the run")
    sender: str = Field(alias="from", description="The email of the person who answers")
    from_name: str = Field(description="Their name")
    to: str = Field(description="The address the agent sent from, else the first it wrote to")
    subject: str = Field(description="'Re: ' and the send's subject; empty when it had none")
    text: str = Field(description="What they wrote")
    in_reply_to: str = Field(description="The id of the send it answers (`ReplyDelivery.thread`)")
    sent_at: str = Field(description="The simulated moment it is sent, ISO 8601")


class ReplySigning(Model):
    """An HMAC-SHA256 over the delivered body, in a header: `format` holds `{hex}` or `{base64}` for the digest,
    and may hold `{timestamp}` (the simulated moment, in Unix seconds), which then also leads what is signed as
    `<timestamp>.<body>`, as several webhook senders do."""

    secret: SigningSecret
    header: str = Field(min_length=1)
    format: str = Field(default="sha256={hex}", pattern=r"\{(hex|base64)\}")
    timestamp_header: str | None = Field(
        default=None, description="A header that carries `{timestamp}` by itself, when the sender sends one"
    )


class ReplyDelivery(Model):
    """How a person's answer to a captured send reaches the agent: the request its own inbound webhook expects.

    `body` is the JSON (or, with `form`, the form fields) the agent's endpoint reads, as structure; each string in
    it may name the reply's fields as `{name}` (`REPLY_FIELDS`). `thread` is a path into the acknowledged send's
    own answer (its `{message_id}`, as the email API answered it), giving `{in_reply_to}`; without it,
    `{in_reply_to}` is the id of the message in the run."""

    url: str = Field(min_length=1)
    method: Literal["POST", "PUT"] = "POST"
    headers: dict[str, str] = {}
    body: JsonValue = Field(
        default=None,
        description="The body, as structure, with `{name}` placeholders in its strings; None: the default shape "
        "(`DeliveredReply`), every reply field under its own name",
    )
    form: bool = Field(default=False, description="Sent as application/x-www-form-urlencoded: `body` is flat")
    thread: BodyPath | None = None
    signing: ReplySigning | None = None

    @model_validator(mode="after")
    def _names_known_fields(self) -> Self:
        named = set(placeholders(self.body))
        unknown = sorted(named - set(REPLY_FIELDS))
        if unknown:
            raise ValueError(f"a reply's body names {', '.join(unknown)}; it may name {', '.join(REPLY_FIELDS)}")
        if (
            self.form
            and self.body is not None
            and not (isinstance(self.body, dict) and all(isinstance(v, str) for v in self.body.values()))
        ):
            raise ValueError("a reply sent as a form has a flat body of strings")
        return self


def placeholders(value: JsonValue) -> list[str]:
    """Every `{name}` inside the strings of a template, in order: what it asks to be filled with."""
    if isinstance(value, str):
        return [m.group(1) for m in PLACEHOLDER.finditer(value)]
    if isinstance(value, list):
        return [n for v in value for n in placeholders(v)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in placeholders(v)]
    return []


PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
"""A name to be filled inside a template's string: `{reply_id}`, `{item_id}`."""


class _Declared(Model):
    host: str = Field(description="An exact lower-case host, or '*.' and a domain for any host under it")
    name: ProviderKey | None = Field(
        default=None, description="What its world events are recorded under; derived from the host when None"
    )
    redact: list[BodyPath] = Field(
        default=[], description="Body fields kept as [redacted], in the request and the answer, besides credentials"
    )
    body_limit: int = Field(default=BODY_LIMIT, ge=0, description="Bytes of a text or JSON body kept")

    @property
    def key(self) -> ProviderKey:
        """The name its messages are recorded under: `name`, or the host with every non-letter an underscore."""
        if self.name is not None:
            return self.name
        spelled = "".join(c if c.isalnum() else "_" for c in self.host.lower().removeprefix("*."))
        return ("any_" if self.host.startswith("*.") else "") + (spelled if spelled[:1].isalpha() else "h_" + spelled)


class Acknowledge(_Declared):
    """The call never leaves the machine: it is kept, and answered as declared. For sends: an email API, a
    webhook, an SMS."""

    kind: Literal["acknowledge"] = "acknowledge"
    answer: Answer = Answer()
    routes: list[Route] = []
    message: MessageReading | None = Field(
        default=None, description="When given, each call is also a message from the agent to a person"
    )
    replies: ReplyDelivery | None = Field(
        default=None,
        description="When given, the people a send reaches can answer it: each answer is delivered to the agent "
        "as declared, and a send to someone who will answer opens a wait like any other ask",
    )

    @model_validator(mode="after")
    def _replies_need_a_reading(self) -> Self:
        if self.replies is not None and self.message is None:
            raise ValueError("a host whose sends are answered must say how a send is read (`message`)")
        return self


KeyPath = Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9_\-$@]+(\.[A-Za-z0-9_\-$@]+)*$")]
"""A field of a JSON object by dotted keys, `paging.next.after`: where Minutehand writes something it assigns."""

COLLECTION_PATH = re.compile(r"^(/([A-Za-z0-9._~\-:@$!]+|\{[a-z_][a-z0-9_]*\}))+$")
"""A collection's path: literal segments and `{name}` segments, each matching one segment of a called path."""


class IdFormat(StrEnum):
    """How a stored item's id is made when it is created."""

    UUID = "uuid"  # a UUID, made from the host, the collection's path and the event that creates the item
    INTEGER = "integer"  # a JSON number: the sequence number of the event that creates the item
    PREFIXED = "prefixed"  # `prefix` and that sequence number, as a string: `ct_42`
    SENT = "sent"  # the id the agent sent at `at`: nothing is made; a create without one is refused 400


class ItemId(Model):
    """Where an item's id is in its JSON, and how it is made: the one field of an item the API assigns besides
    `stamps`. The item is read and written at `<collection path>/<id>`."""

    at: KeyPath = "id"
    format: IdFormat = IdFormat.UUID
    prefix: str = Field(default="", description="Before the sequence number, with `format: prefixed`")

    @model_validator(mode="after")
    def _prefix_is_text(self) -> Self:
        if self.prefix and self.format is not IdFormat.PREFIXED:
            raise ValueError("an id's `prefix` goes with `format: prefixed`")
        return self


class StampOn(StrEnum):
    CREATE = "create"  # written when the item is created, then kept as it was
    WRITE = "write"  # written when the item is created, replaced or patched


class StampFormat(StrEnum):
    ISO8601 = "iso8601"  # `2026-08-24T10:00:00Z`
    EPOCH_SECONDS = "epoch_seconds"  # whole seconds since 1970, a JSON number
    EPOCH_MILLISECONDS = "epoch_milliseconds"  # milliseconds since 1970, a JSON number


class Stamp(Model):
    """A moment the API writes into an item, from the run's clock: a created or updated time."""

    at: KeyPath
    on: StampOn = StampOn.CREATE
    format: StampFormat = StampFormat.ISO8601


class Listing(Model):
    """How a GET of the collection answers: the stored items in the order they were created, in an envelope.

    With `items_at` None the answer is the bare JSON list. With `limit_param`, a page holds at most that many
    items (`default_limit` when the call names none); with `cursor_param`, the call names the id of the last item
    it has, and `next_at` is where the answer puts the id to send for the next page, absent on the last."""

    items_at: KeyPath | None = None
    envelope: dict[str, JsonValue] = Field(default={}, description="Fields answered beside the items, as given")
    limit_param: str | None = None
    default_limit: int = Field(default=100, ge=1)
    cursor_param: str | None = None
    next_at: KeyPath | None = None

    @model_validator(mode="after")
    def _paging_fits(self) -> Self:
        if self.items_at is None and (self.envelope or self.next_at is not None):
            raise ValueError("a listing answered as a bare list has no envelope and no `next_at`: name `items_at`")
        if self.next_at is not None and self.cursor_param is None:
            raise ValueError("a listing with `next_at` reads the next page by `cursor_param`: name it")
        if self.items_at is not None and self.items_at in self.envelope:
            raise ValueError(f"the envelope's field {self.items_at!r} is where the items go")
        return self


class Collection(Model):
    """One REST collection a `store` host keeps: `path` lists and creates (GET, POST), and `<path>/<id>` reads,
    replaces, patches and deletes one item (GET, PUT, PATCH, DELETE). A `{name}` segment of `path` matches any one
    segment, and each value of it is a collection of its own: `/v1/companies/{company}/contacts`."""

    path: str = Field(description="`/v1/contacts`; a `{name}` segment matches any one segment")
    name: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.-]{1,64}$",
        description="What an assessment counts it by (`stored: {collection: ...}`); the last literal segment of "
        "`path` when None",
    )
    id: ItemId = ItemId()
    stamps: list[Stamp] = []
    listing: Listing = Listing()
    created_status: int = Field(default=201, ge=200, le=299)
    deleted_status: int = Field(default=204, ge=200, le=299)

    @model_validator(mode="after")
    def _paths_fit(self) -> Self:
        if not COLLECTION_PATH.match(self.path):
            raise ValueError(f"collection path {self.path!r} is not `/segment/...` with `{{name}}` segments")
        if not self.key:
            raise ValueError(f"collection path {self.path!r} has no literal segment to name it by: give it a `name`")
        named = PLACEHOLDER.findall(self.path)
        if "id" in named or len(set(named)) != len(named):
            raise ValueError(f"collection path {self.path!r} names a segment twice, or `{{id}}`, which is the item's")
        assigned = [self.id.at, *(s.at for s in self.stamps)]
        twice = sorted({a for a in assigned if assigned.count(a) > 1})
        if twice:
            raise ValueError(f"collection {self.key}: {', '.join(twice)} is assigned twice")
        return self

    @property
    def key(self) -> str:
        """Its name, or the last literal segment of its path."""
        if self.name is not None:
            return self.name
        literal = [p for p in self.path.split("/") if p and not p.startswith("{")]
        return literal[-1] if literal else ""


class DeclaredStore(_Declared):
    """What the agent writes is kept exactly as sent, in the run's world, and read back unchanged: for an API the
    agent writes to and reads its writes back from, which no provider fakes. Minutehand adds to an item only what
    its collection says the API assigns (`id`, `stamps`); credentials are never checked.

    A call is answered by the first route that matches it (`routes`, as `acknowledge`), else by the collection
    whose path it is under, else with `answer`."""

    kind: Literal["store"] = "store"
    collections: list[Collection] = Field(min_length=1)
    routes: list[Route] = []
    answer: Answer = Answer()

    @model_validator(mode="after")
    def _collections_named_once(self) -> Self:
        keys = [c.key for c in self.collections]
        paths = [c.path for c in self.collections]
        for label, values in (("name", keys), ("path", paths)):
            twice = sorted({v for v in values if values.count(v) > 1})
            if twice:
                raise ValueError(f"host {self.host}: collection {label} declared twice: {', '.join(twice)}")
        return self


class InForks(StrEnum):
    """What a pass-through host does in a fork of a run."""

    REPLAY = "replay"  # answered from the parent run's recording when it holds the same call; else sent on
    PASS_THROUGH = "pass_through"  # sent to the real host again


class PassThrough(_Declared):
    """The call goes to the real host unchanged; the request and the answer are both kept. For lookups: a
    search, a page fetch. A streamed answer reaches the agent as a stream."""

    kind: Literal["pass_through"] = "pass_through"
    in_forks: InForks = Field(
        default=InForks.REPLAY,
        description="In a fork: replay the parent's answer to the same call, so the fork differs from its parent "
        "only by what it changed; pass_through calls the real host again",
    )
    ignore_query: list[str] = Field(default=[], description="Query parameters a fork's replay does not match on")
    ignore_body: list[BodyPath] = Field(default=[], description="Body fields a fork's replay does not match on")


class RecordedRun(Model):
    kind: Literal["run"] = "run"
    run: str = Field(min_length=1, description="A run id under the state directory")


class RecordingsDirectory(Model):
    kind: Literal["directory"] = "directory"
    directory: str = Field(min_length=1, description="A directory holding a run's captured.jsonl")


RecordingSource = Annotated[RecordedRun | RecordingsDirectory, Field(discriminator="kind")]


class OnMiss(StrEnum):
    PASS_THROUGH = "pass_through"  # sent to the real host, and the call kept as a new recording
    REFUSE = "refuse"  # answered 502, saying the recording holds no such call


class Replay(_Declared):
    """The call is answered from the recording of an earlier run, matched on method, host, path, query and a
    hash of the body with `ignore_query` and `ignore_body` left out (timestamps, nonces). The nth identical call
    gets the nth recorded answer to it, and the last one again after that."""

    kind: Literal["replay"] = "replay"
    source: RecordingSource
    on_miss: OnMiss = OnMiss.REFUSE
    ignore_query: list[str] = []
    ignore_body: list[BodyPath] = []


class HostHeader(StrEnum):
    """The `Host` a forwarded call reaches its emulator with."""

    PRESERVE = "preserve"  # the host the agent called (`api.tracker.example`): one emulator can serve several
    UPSTREAM = "upstream"  # the upstream's own host and port, for a server that checks its own name


class Forward(_Declared):
    """The call is sent to an external emulator the same file declares (`domain.emulator.ExternalEmulator`), and
    kept, both sides verbatim, as a passed-through call is: request and answer streamed through untouched but for
    the headers `domain.emulator.ADDED_HEADERS` lists and `traceparent`, set on the forwarded copy only.

    `strip` is taken off the front of the path and `prefix` put before what is left, so `/graphql` on the agent's
    side can be `/tracker/graphql` at the emulator."""

    kind: Literal["forward"] = "forward"
    emulator: ProviderKey = Field(description="The `name` of the emulator that answers this host")
    strip: str = Field(default="", pattern=r"^(/[^?#]*)?$")
    prefix: str = Field(default="", pattern=r"^(/[^?#]*)?$")
    host_header: HostHeader = HostHeader.PRESERVE


OutboundHost = Annotated[Acknowledge | PassThrough | Replay | Forward | DeclaredStore, Field(discriminator="kind")]


def refuse_repeats(declared: list[Acknowledge | PassThrough | Replay | Forward | DeclaredStore]) -> None:
    """Two declarations of one host, or one name, would leave a call's mode to their order."""
    hosts = [d.host for d in declared]
    keys = [d.key for d in declared]
    for label, values in (("host", hosts), ("name", keys)):
        twice = sorted({v for v in values if values.count(v) > 1})
        if twice:
            raise ValueError(f"outbound {label} declared twice: {', '.join(twice)}")
