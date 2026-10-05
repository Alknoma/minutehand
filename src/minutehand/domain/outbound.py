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

A host a provider claims, or a model API, cannot also be declared. A host nobody declares or claims is still
refused, unless the run captures unknown hosts (`--capture-unknown`).

A path names a value in a request body read as JSON (or a form, whose fields are its top level): dotted keys,
`[n]` for one item of a list and `[*]` for every item, e.g. `personalizations[*].to[*].email`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from minutehand.domain.scenario import Model, ProviderKey

BODY_LIMIT = 1024 * 1024
"""Bytes of a text or JSON body kept by default. A longer body is kept up to this and marked truncated."""

BodyPath = Annotated[
    str, Field(min_length=1, pattern=r"^[A-Za-z0-9_\-$@]+(\[(\*|\d+)\])*(\.[A-Za-z0-9_\-$@]+(\[(\*|\d+)\])*)*$")
]
"""A value in a JSON or form body: dotted keys, `[n]` for one list item, `[*]` for every item."""


class Answer(Model):
    """What an acknowledged call is answered with. With neither body, the JSON `{}`."""

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


OutboundHost = Annotated[Acknowledge | PassThrough | Replay, Field(discriminator="kind")]


def refuse_repeats(declared: list[Acknowledge | PassThrough | Replay]) -> None:
    """Two declarations of one host, or one name, would leave a call's mode to their order."""
    hosts = [d.host for d in declared]
    keys = [d.key for d in declared]
    for label, values in (("host", hosts), ("name", keys)):
        twice = sorted({v for v in values if values.count(v) > 1})
        if twice:
            raise ValueError(f"outbound {label} declared twice: {', '.join(twice)}")
