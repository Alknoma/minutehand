"""The one place an exception leaving a provider's app becomes the agent's answer, and how each call was answered.

A provider's request handler lets out three kinds of exception (`domain.errors`), and `convert` answers each:

- a `ServiceRefusal`: rendered as the real service renders it, logged at debug;
- a `NotImplementedError`: 501 in the vendor's error shape (`ports.provider.RendersErrors`), its message saying the
  fake does not implement the operation and naming the method and path, logged at info; a `NotServed` (the
  provider saying so by name) is also noted as not served (`Outcome.not_served`), so the proxy may hand the call to
  the run's own declaration for the host instead (`domain.outbound`);
- anything else: Minutehand's own bug, 500 in the vendor's error shape, its message beginning
  "minutehand internal error while answering <provider> <METHOD> <path>:", logged at error with its traceback,
  which is kept on the recorded call. Never raised on into the proxy.

`Guarded` wraps the app the proxy serves: mitmproxy's `asgiapp.serve` turns an exception into a bare 500
"ASGI Error.", and Starlette's `ServerErrorMiddleware` sends its own 500 before it re-raises, so the guard holds back
what the app sends and, when the app raises, sends the converted answer instead.

How a call was answered is noted on the `Outcome` the proxy sets for it (`OUTCOME`): by `convert`, by a provider
whose own fault fires (`injected`), by a provider that refuses with a status that does not say so (`refused`), or
by the control API's armed fault; a call nobody noted is refused when its status is 400 or more, else answered.
"""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.requests import Request

from minutehand.domain.errors import Asked, GrpcRefusal, NotServed, Rendered, ServiceRefusal
from minutehand.domain.world import CallFailure, CallOutcome, GrpcCode
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, Message, RendersErrors, Scope

logger = logging.getLogger(__name__)

NOT_IMPLEMENTED_CODE = "not_implemented"
INTERNAL_ERROR_CODE = "internal_error"
INTERNAL_PREFIX = "minutehand internal error while answering"


@dataclass
class Outcome:
    """How one call was answered; `kind` None when nothing noted it, and its status decides (`kind_of`)."""

    kind: CallOutcome | None = None
    failure: CallFailure | None = None
    not_served: bool = False
    """The provider said by name that it does not serve the call (`NotServed`, or its own rendered 501 through
    `unimplemented`): the proxy may answer it from the run's declaration for the host instead."""


OUTCOME: ContextVar[Outcome | None] = ContextVar("minutehand_call_outcome", default=None)
"""The call being answered: set by the proxy around each call it answers from a provider."""


def _note(kind: CallOutcome, failure: CallFailure | None = None) -> None:
    outcome = OUTCOME.get()
    if outcome is not None:
        outcome.kind = kind
        outcome.failure = failure


def injected() -> None:
    """The call is answered by a fault armed on purpose: a provider's own declared fault, or the control API's."""
    _note(CallOutcome.INJECTED_FAULT)


def refused() -> None:
    """The provider refused the call in a shape whose status does not say so (Slack's `ok: false` at 200)."""
    outcome = OUTCOME.get()
    if outcome is not None and outcome.kind is None:
        outcome.kind = CallOutcome.REFUSED


def unimplemented(error: Exception, message: str) -> None:
    """The provider answered, in its own words, that it does not implement what the call asks (AWS's 501
    `NotImplemented`, rendered by the provider before it reaches anything)."""
    outcome = OUTCOME.get()
    if outcome is not None:
        outcome.not_served = True
    _note(
        CallOutcome.NOT_IMPLEMENTED,
        CallFailure(kind=CallOutcome.NOT_IMPLEMENTED, message=message, exception_type=_qualified(error)),
    )


def kind_of(outcome: Outcome, status: int) -> CallOutcome:
    """How the call was answered: as noted, else refused when its status is 400 or more, else answered."""
    if outcome.kind is not None:
        return outcome.kind
    return CallOutcome.REFUSED if status >= 400 else CallOutcome.ANSWERED


def _qualified(error: BaseException) -> str:
    return f"{type(error).__module__}.{type(error).__qualname__}"


def convert(error: Exception, renders: RendersErrors, *, provider: str, asked: Asked) -> Rendered:
    """THE converter: `error`, raised while answering `asked` for `provider`, as the answer the agent gets, noted on
    the call's `Outcome` and logged by its kind."""
    where = f"{provider} {asked.method} {asked.path}"
    if isinstance(error, ServiceRefusal):
        rendered = error.render(asked)
        logger.debug("%s refused %s: %s", provider, where, rendered.status)
        if isinstance(error, NotServed):
            unimplemented(
                error, f"minutehand's {provider} fake does not implement {asked.method} {asked.path}: {error}"
            )
        else:
            _note(CallOutcome.REFUSED)
        return rendered
    if isinstance(error, NotImplementedError):
        said = f": {error}" if str(error) else ""
        message = f"minutehand's {provider} fake does not implement {asked.method} {asked.path}{said}"
        logger.info("%s", message)
        _note(
            CallOutcome.NOT_IMPLEMENTED,
            CallFailure(kind=CallOutcome.NOT_IMPLEMENTED, message=message, exception_type=_qualified(error)),
        )
        outcome = OUTCOME.get()
        if outcome is not None:
            outcome.not_served = isinstance(error, NotServed)
        return renders.error(501, NOT_IMPLEMENTED_CODE, message)
    message = f"{INTERNAL_PREFIX} {where}: {type(error).__name__}: {error}"
    logger.error("%s", message, exc_info=error)
    _note(
        CallOutcome.INTERNAL_ERROR,
        CallFailure(
            kind=CallOutcome.INTERNAL_ERROR,
            message=message,
            exception_type=_qualified(error),
            traceback="".join(traceback.format_exception(error)),
        ),
    )
    return renders.error(500, INTERNAL_ERROR_CODE, message)


@dataclass(frozen=True)
class GrpcEnded:
    """How a gRPC method that raised ends: its status, its `grpc-message`, and why Minutehand answered in the fake's
    place when it did."""

    code: GrpcCode
    message: str
    failure: CallFailure | None = None


def grpc_ended(error: Exception, *, provider: str, path: str) -> GrpcEnded:
    """THE converter for a gRPC method (`ports.provider.GrpcMethod`): `error`, raised while answering `path` for
    `provider`, as the status the call ends with, logged by its kind as `convert` logs."""
    if isinstance(error, GrpcRefusal):
        logger.debug("%s refused gRPC %s: %s", provider, path, error.code)
        return GrpcEnded(error.code, error.message)
    if isinstance(error, NotImplementedError):
        said = f": {error}" if str(error) else ""
        message = f"minutehand's {provider} fake does not implement gRPC {path}{said}"
        logger.info("%s", message)
        failure = CallFailure(kind=CallOutcome.NOT_IMPLEMENTED, message=message, exception_type=_qualified(error))
        return GrpcEnded(GrpcCode.UNIMPLEMENTED, message, failure)
    message = f"{INTERNAL_PREFIX} {provider} gRPC {path}: {type(error).__name__}: {error}"
    logger.error("%s", message, exc_info=error)
    failure = CallFailure(
        kind=CallOutcome.INTERNAL_ERROR,
        message=message,
        exception_type=_qualified(error),
        traceback="".join(traceback.format_exception(error)),
    )
    return GrpcEnded(GrpcCode.INTERNAL, message, failure)


GRPC_INTERNAL = frozenset({GrpcCode.INTERNAL, GrpcCode.UNKNOWN, GrpcCode.DATA_LOSS})


def grpc_outcome(code: GrpcCode, failure: CallFailure | None) -> CallOutcome:
    """What a gRPC call's answer was, by the status it ended with: as its failure says when Minutehand answered in
    the fake's place, else answered on OK, not implemented on UNIMPLEMENTED, Minutehand's error on a status that
    says the server broke, and refused on any other."""
    if failure is not None:
        return failure.kind
    if code is GrpcCode.OK:
        return CallOutcome.ANSWERED
    if code is GrpcCode.UNIMPLEMENTED:
        return CallOutcome.NOT_IMPLEMENTED
    return CallOutcome.INTERNAL_ERROR if code in GRPC_INTERNAL else CallOutcome.REFUSED


class _Plain:
    """The error shape for a provider with none of its own (`RendersErrors`): `{"error": code, "message": message}`."""

    def error(self, status: int, code: str, message: str) -> Rendered:
        return Rendered(
            status=status,
            content_type="application/json",
            body=json.dumps({"error": code, "message": message}).encode(),
        )


PLAIN = _Plain()


def answer_for(error: Exception, renders: RendersErrors, *, provider: str, asked: Asked) -> Rendered:
    """`convert`; when rendering fails in turn (a refusal or a renderer with a bug of its own), that failure is
    Minutehand's internal error, answered in the plain shape so the agent still gets an answer."""
    try:
        return convert(error, renders, provider=provider, asked=asked)
    except Exception as failed:
        failed.__context__ = error
        return convert(failed, PLAIN, provider=provider, asked=asked)


async def send_rendered(rendered: Rendered, send: Callable[[Message], Awaitable[None]]) -> None:
    """`rendered` as ASGI messages."""
    headers = [
        (b"content-type", rendered.content_type.encode("latin-1")),
        (b"content-length", str(len(rendered.body)).encode("latin-1")),
        *((name.encode("latin-1"), value.encode("latin-1")) for name, value in rendered.headers),
    ]
    await send({"type": "http.response.start", "status": rendered.status, "headers": headers})
    await send({"type": "http.response.body", "body": rendered.body})


class NoAnswer(RuntimeError):
    """A provider's app returned without sending an answer."""


class Guarded:
    """`app`, every exception it lets out of a request answered by `convert` in `renders`' shape."""

    def __init__(self, app: ASGIApp, renders: RendersErrors, *, provider: str, clock: Clock) -> None:
        self._app = app
        self._renders = renders
        self._provider = provider
        self._clock = clock

    async def __call__(
        self, scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]
    ) -> None:
        held: list[Message] = []

        async def hold(message: Message) -> None:
            held.append(message)

        try:
            await self._app(scope, receive, hold)
            if not any(m["type"] == "http.response.start" for m in held):
                raise NoAnswer(f"the {self._provider} app returned without answering")
        except Exception as error:
            asked = self._asked(scope)
            await send_rendered(answer_for(error, self._renders, provider=self._provider, asked=asked), send)
            return
        for message in held:
            await send(message)

    def _asked(self, scope: Scope) -> Asked:
        request = Request(scope)
        return Asked(
            method=request.method,
            url=str(request.url),
            path=request.url.path,
            headers=[(k.lower(), v) for k, v in request.headers.items()],
            now=self._clock.now(),
        )
