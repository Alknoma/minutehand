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

A host a provider claims, or a model API, cannot also be declared. A host nobody declares or claims is still
refused, unless the run captures unknown hosts (`--capture-unknown`).

A path names a value in a request body read as JSON (or a form, whose fields are its top level): dotted keys,
`[n]` for one item of a list and `[*]` for every item, e.g. `personalizations[*].to[*].email`.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from minutehand.domain.scenario import Model, ProviderKey, SigningSecret

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
    body: JsonValue = Field(description="The body, as structure, with `{name}` placeholders in its strings")
    form: bool = Field(default=False, description="Sent as application/x-www-form-urlencoded: `body` is flat")
    thread: BodyPath | None = None
    signing: ReplySigning | None = None

    @model_validator(mode="after")
    def _names_known_fields(self) -> Self:
        named = set(placeholders(self.body))
        unknown = sorted(named - set(REPLY_FIELDS))
        if unknown:
            raise ValueError(f"a reply's body names {', '.join(unknown)}; it may name {', '.join(REPLY_FIELDS)}")
        if self.form and not (isinstance(self.body, dict) and all(isinstance(v, str) for v in self.body.values())):
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


OutboundHost = Annotated[Acknowledge | PassThrough | Replay | Forward, Field(discriminator="kind")]


def refuse_repeats(declared: list[Acknowledge | PassThrough | Replay | Forward]) -> None:
    """Two declarations of one host, or one name, would leave a call's mode to their order."""
    hosts = [d.host for d in declared]
    keys = [d.key for d in declared]
    for label, values in (("host", hosts), ("name", keys)):
        twice = sorted({v for v in values if values.count(v) > 1})
        if twice:
            raise ValueError(f"outbound {label} declared twice: {', '.join(twice)}")
