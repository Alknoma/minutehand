"""What can go wrong, as a closed set of kinds, and the typed record a failure is kept as.

Copied from LocalStack's `ServiceException` and moto's `ServiceException`: a fake's request handler may let only
three kinds of exception out, and one converter at each boundary (`adapters.answering` for a provider's app; the
control API, the CLI, the MCP tools and the viewer each have their own) turns them into an answer:

- `ServiceRefusal`: the real service would refuse this. It carries the vendor's error code, a message and the HTTP
  status, and a provider subclasses it only to render ITS wire shape (`render`). `deliberate` marks a fault a
  scenario or a test armed on purpose.
- `NotImplementedError` (or `NotImplementedByFake`, which also names the closest route the fake has): the real
  service has this and the fake does not yet. Answered 501 in the vendor's shape.
- anything else: Minutehand's own bug. Answered 500 in the vendor's shape, its message beginning
  "minutehand internal error while answering", and kept with its traceback.

Outside a provider, `EnvironmentFailure` is the machine failing (a port in use, a command that would not start, an
upstream that cannot be reached, a timeout, a CA not trusted): it names the resource and the next step.
"""

from __future__ import annotations

import traceback
from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import Field

from minutehand.domain.scenario import Model


class FailureKind(StrEnum):
    """Every kind of failure Minutehand can produce or report. Closed: nothing fails any other way."""

    REFUSED = "refused"  # the real service would refuse this, and the fake did as it would
    INJECTED_FAULT = "injected_fault"  # a fault a scenario or a test armed on purpose
    NOT_IMPLEMENTED = "not_implemented"  # the real service has this and the fake does not yet
    USAGE = "usage"  # the scenario, the agent file, the request or the command line is wrong
    AGENT = "agent"  # the agent under test could not be reached or answered with an error
    INTERNAL = "internal"  # a bug in Minutehand
    ENVIRONMENT = "environment"  # the machine failed: a port, a process, a network, a certificate


class AnswerKind(StrEnum):
    """How a recorded call was answered, by the fake or for it."""

    ANSWERED = "answered"  # the fake answered it, and not with a refusal
    REFUSED = "refused"  # refused as the real service refuses it (an answer of status 400 or more counts)
    INJECTED_FAULT = "injected_fault"  # failed on purpose by a fault a scenario or a test armed
    NOT_IMPLEMENTED = "not_implemented"  # the fake has no answer for this operation
    INTERNAL_ERROR = "internal_error"  # Minutehand failed while answering it


class Cause(Model):
    """One exception in a failure's cause chain."""

    type: str = Field(description="The exception's qualified class name")
    message: str


class Failure(Model):
    """One failure, kept as a structure: what kind, the code a machine reads, the message a person reads, where it
    happened, and for an internal error the traceback and the chain of causes behind it."""

    kind: FailureKind
    code: str = Field(description="Stable, machine-readable: the vendor's own error code for a refusal")
    message: str
    where: str = Field(description="The request (`POST /api/chat.postMessage`) or the step it belongs to")
    traceback: str | None = None
    causes: list[Cause] = Field(default=[], description="The exception first, then each cause behind it")

    @classmethod
    def of(cls, error: BaseException, *, kind: FailureKind, code: str, message: str, where: str) -> Failure:
        """`error` as a failure; for an internal error, with its traceback."""
        return cls(
            kind=kind,
            code=code,
            message=message,
            where=where,
            traceback="".join(traceback.format_exception(error)) if kind is FailureKind.INTERNAL else None,
            causes=causes(error),
        )


def causes(error: BaseException) -> list[Cause]:
    """`error` and every exception it was raised from or during, outermost first."""
    found: list[Cause] = []
    seen: set[int] = set()
    at: BaseException | None = error
    while at is not None and id(at) not in seen:
        seen.add(id(at))
        found.append(Cause(type=f"{type(at).__module__}.{type(at).__qualname__}", message=str(at)))
        at = at.__cause__ or at.__context__
    return found


class Rendered(Model):
    """An answer as it goes on the wire: what a provider renders a refusal, or an error of its own, as."""

    status: int = Field(ge=100, le=599)
    content_type: str
    body: bytes
    headers: list[tuple[str, str]] = Field(default=[], description="Besides content-type")


class ServiceRefusal(Exception, ABC):
    """The real service would refuse this request. Never raised as itself: each provider subclasses it once per
    wire shape and renders the body the real service sends (`render`), with whatever typed extras that body needs.

    `deliberate` is a fault a scenario or a test armed on purpose: rendered the same way, recorded apart."""

    def __init__(self, code: str, message: str, *, status: int, deliberate: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.status = status
        self.deliberate = deliberate

    @abstractmethod
    def render(self) -> Rendered:
        """The answer the real service gives, exactly."""


class NotImplementedByFake(NotImplementedError):
    """The real service has this and the fake does not: no route of the fake answers `operation`. `closest` is the
    route the fake has that is nearest to it, when one is near enough to name."""

    def __init__(self, operation: str, *, closest: str | None = None) -> None:
        super().__init__(operation)
        self.operation = operation
        self.closest = closest


class EnvironmentFailure(Exception):
    """The machine failed, not Minutehand and not the agent: `resource` (a port, a command, a host, a certificate)
    could not be used, for `problem`, and `next_step` says what a person does about it."""

    def __init__(self, resource: str, problem: str, next_step: str) -> None:
        super().__init__(f"{resource}: {problem}. {next_step}")
        self.resource = resource
        self.problem = problem
        self.next_step = next_step
