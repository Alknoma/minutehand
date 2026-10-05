"""An external emulator: a fake of a service that lives outside Minutehand, in any language, as its own process or
container, which a declared outbound host is forwarded to (`domain.outbound.Forward`).

Minutehand cannot see inside it. It routes the agent's calls to it, keeps each call verbatim, starts and watches it
(or attaches to one already running), and tells its faithful errors and its missing operations from its own
failures. It does not score what the emulator holds, and it cannot rewind it: a run that used one has state
outside its record, and a fork after its first use is refused.

    emulators:
      - name: payments
        upstream: {url: "http://127.0.0.1:{port}"}           # or https://... with `ca`, or unix:///path/to.sock
        command: [docker, run, --rm, -p, "{port}:12111", stripe/stripe-mock]   # none: it is already running
        ready: {kind: http, path: /v1/charges, status: 401}  # or {kind: tcp}, or {kind: log, line: "Listening"}
        health: {every: PT1S, fails: 2}
        faithful: [{status: 402}]
        not_implemented: [{status: 501}]

`{port}` in the upstream, the command or its environment is a free port of this machine Minutehand picks for each
start, so two worlds' emulators never collide.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from minutehand.domain.scenario import Model, ProviderKey

PORT = "{port}"
"""Replaced, in an emulator's upstream, command and environment, by a free port picked for each start."""

WORLD_HEADER = "x-minutehand-world"
WAKE_HEADER = "x-minutehand-wake"
TIME_HEADER = "x-minutehand-time"
ADDED_HEADERS = (WORLD_HEADER, WAKE_HEADER, TIME_HEADER)
"""What Minutehand adds to the forwarded copy of a call, and to nothing else: the world (the run, or the standing
world) it belongs to, the wake in progress, and the simulated time, ISO 8601. `traceparent` is also set on the
copy, continuing the agent's trace with Minutehand's span of the call as the parent. The agent's own request and
the answer it gets carry none of them."""

BodyPath = Annotated[
    str, Field(min_length=1, pattern=r"^[A-Za-z0-9_\-$@]+(\[(\*|\d+)\])*(\.[A-Za-z0-9_\-$@]+(\[(\*|\d+)\])*)*$")
]
"""A value in a JSON body: dotted keys, `[n]` for one list item, `[*]` for every item."""


class Upstream(Model):
    """Where the emulator listens: `http://host:port`, `https://host:port` (verified against `ca` when given, else
    the system's roots), or `unix:///path/to.sock`. A path on an http(s) URL is put before every forwarded path."""

    url: str = Field(min_length=1)
    ca: str | None = Field(default=None, description="A PEM file of the CAs an https upstream is verified against")

    @model_validator(mode="after")
    def _known_scheme(self) -> Self:
        scheme = urlsplit(self.url.replace(PORT, "1")).scheme
        if scheme not in ("http", "https", "unix"):
            raise ValueError(f"an emulator's upstream is http://, https:// or unix://, not {self.url!r}")
        if self.ca is not None and scheme != "https":
            raise ValueError("a CA is given only for an https upstream")
        return self


class ReadyHttp(Model):
    """Ready once `path` answers `status` (Testcontainers' `forHttp(path).forStatusCode(status)`)."""

    kind: Literal["http"] = "http"
    path: str = Field(default="/", pattern=r"^/")
    status: int = Field(default=200, ge=100, le=599)


class ReadyTcp(Model):
    """Ready once its upstream accepts a connection (Testcontainers' `forListeningPort()`)."""

    kind: Literal["tcp"] = "tcp"


class ReadyLog(Model):
    """Ready once its log has printed a line matching `line`, a regular expression, `times` times
    (Testcontainers' `forLogMessage`). Only for an emulator Minutehand starts, whose output it reads."""

    kind: Literal["log"] = "log"
    line: str = Field(min_length=1)
    times: int = Field(default=1, ge=1)


Readiness = Annotated[ReadyHttp | ReadyTcp | ReadyLog, Field(discriminator="kind")]


class Health(Model):
    """Polled for as long as the emulator is in use: `path` answering `status`, or, with no path, its upstream
    accepting a connection. `fails` failures in a row make it unhealthy."""

    path: str | None = Field(default=None, pattern=r"^/")
    status: int = Field(default=200, ge=100, le=599)
    every: timedelta = Field(default=timedelta(seconds=1), gt=timedelta(0))
    fails: int = Field(default=2, ge=1)
    answer_within: timedelta = Field(default=timedelta(seconds=2), gt=timedelta(0))


class ErrorMarker(Model):
    """An answer that is an error of a known kind: its status, and, with `at`, a value in its JSON body at that path
    (`equals` it, or present at all when `equals` is None). For GraphQL: `errors[*].extensions.code`."""

    status: int | None = Field(default=None, ge=100, le=599)
    at: BodyPath | None = None
    equals: str | None = None

    @model_validator(mode="after")
    def _says_something(self) -> Self:
        if self.status is None and self.at is None:
            raise ValueError("an error marker names a status, a path in the body, or both")
        if self.equals is not None and self.at is None:
            raise ValueError("`equals` is compared with the value `at` a path; give the path")
        return self


class ExternalEmulator(Model):
    """One service's fake that Minutehand does not hold: started with the run (or, in `minutehand serve`, with the
    first world that declares it, and shared by every world declaring it the same, until the server stops), or
    attached to when it has no command. Minutehand never restarts one: an emulator that dies or turns unhealthy
    stays down, every call to it is answered 502 or 504, and the run's environment has failed."""

    name: ProviderKey = Field(description="What its calls and findings are recorded under")
    upstream: Upstream
    command: list[str] | None = Field(
        default=None, min_length=1, description="Started with the run and stopped with it; None: already running"
    )
    env: dict[str, str] = Field(default={}, description="Added to its environment, beside the OTLP variables")
    directory: str | None = Field(default=None, description="Where its command runs; None: Minutehand's own")
    ready: Readiness = Field(default=ReadyTcp(), description="When it counts as up")
    ready_within: timedelta = Field(default=timedelta(seconds=30), gt=timedelta(0))
    health: Health = Health()
    answer_within: timedelta = Field(
        default=timedelta(seconds=30),
        gt=timedelta(0),
        description="A forwarded call with no byte of answer after this is answered 504, and the emulator failed",
    )
    faithful: list[ErrorMarker] = Field(
        default=[], description="Errors it answers on purpose, as the real API would; a 4xx is always one"
    )
    not_implemented: list[ErrorMarker] = Field(
        default=[ErrorMarker(status=501)], description="Answers that mean it has no implementation of the call"
    )

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if isinstance(self.ready, ReadyLog) and self.command is None:
            raise ValueError(
                f"emulator {self.name} is ready by a log line, and Minutehand reads only the log of one "
                "it starts: give its command, or another readiness"
            )
        uses_port = PORT in self.upstream.url
        if uses_port and self.command is None:
            raise ValueError(f"emulator {self.name}'s upstream names {PORT}, which only one Minutehand starts is given")
        return self


class EmulatorHealth(StrEnum):
    """A transition in an external emulator's health, recorded in the world's log and exported as it happens."""

    READY = "ready"  # it came up and passed its readiness check
    UNHEALTHY = "unhealthy"  # its health check failed `Health.fails` times in a row, or a forwarded call got no answer
    DIED = "died"  # its process exited while the run used it
    STOPPED = "stopped"  # Minutehand stopped it: the run, the world or the server ended


class EmulatorChange(Model):
    """One health transition, as the world's log keeps it."""

    emulator: str
    health: EmulatorHealth
    reason: str | None = Field(default=None, description="Why, in words, for anything but READY and STOPPED")
    log_tail: str | None = Field(default=None, description="The end of its log, for one Minutehand started")


def refuse_unknown_emulators(names: list[str], declared: list[ExternalEmulator]) -> None:
    """Every forwarded host names an emulator declared beside it, and no two emulators share a name."""
    own = [e.name for e in declared]
    twice = sorted({n for n in own if own.count(n) > 1})
    if twice:
        raise ValueError(f"emulator declared twice: {', '.join(twice)}")
    unknown = sorted(set(names) - set(own))
    if unknown:
        raise ValueError(f"a host is forwarded to {', '.join(unknown)}, and no emulator of that name is declared")
