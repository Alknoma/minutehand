"""The one place an exception leaving a provider's app becomes an answer.

A provider's request handler lets out only three kinds of exception (`domain.errors`): a `ServiceRefusal`, rendered
as the real service renders it; a `NotImplementedError`, answered 501 in the vendor's error shape; anything else,
Minutehand's own error, answered 500 in the vendor's error shape. `guarded` wraps the provider's ASGI app: it holds
back what the app sends, and when the app raises, throws that away and sends the converted answer instead, so the
agent always gets an answer and nothing is raised into the server (mitmproxy's `asgiapp.serve` would turn it into a
bare "ASGI Error." 500, and Starlette's `ServerErrorMiddleware` sends its own 500 before it re-raises).

Every provider's `app()` returns its app guarded, so a test driving the app directly sees what the agent sees. The
proxy guards each call again, for what fails outside the app (a provider that cannot be built, a fault the control
API armed), and reads how the call was answered from `OUTCOME`, set for the call by whichever guard converted it.

`MINUTEHAND_DEBUG=1` in the environment puts an internal error's traceback in the answer's body too; it is always
in the log and on the recorded call.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass

from minutehand.domain.errors import (
    AnswerKind,
    Failure,
    FailureKind,
    NotImplementedByFake,
    Rendered,
    ServiceRefusal,
)
from minutehand.ports.provider import ASGIApp, DeliversInBackground, Message, RendersErrors, Scope

logger = logging.getLogger(__name__)

DEBUG_VARIABLE = "MINUTEHAND_DEBUG"
"""Set to 1 to put an internal error's traceback in the body of the answer the agent gets."""

ANSWER_HEADER = "x-minutehand-answer"
"""Set on an answer Minutehand made in place of the service's: `not_implemented` or `internal_error`. A faithful
refusal never carries it, so nobody mistakes Minutehand's failure for the vendor's."""

NOT_IMPLEMENTED_CODE = "minutehand_not_implemented"
INTERNAL_ERROR_CODE = "minutehand_internal_error"
INTERNAL_PREFIX = "minutehand internal error while answering"


@dataclass
class Outcome:
    """How one call was answered, filled by the guard that converted an exception; ANSWERED when none did."""

    kind: AnswerKind = AnswerKind.ANSWERED
    failure: Failure | None = None


OUTCOME: ContextVar[Outcome | None] = ContextVar("minutehand_answer_outcome", default=None)
"""The call being answered: set by whoever serves it (the proxy), filled by the guard that converts."""


def debugging() -> bool:
    """Whether an internal error's traceback goes in the answer's body (`MINUTEHAND_DEBUG=1`)."""
    return os.environ.get(DEBUG_VARIABLE, "") not in ("", "0")


def closest(asked: str, routes: Sequence[str]) -> str | None:
    """The route among `routes` nearest to `asked`, when one is near enough to be worth naming."""
    found = difflib.get_close_matches(asked, list(routes), n=1, cutoff=0.5)
    return found[0] if found else None


def unrouted(method: str, path: str, routes: Sequence[str]) -> NotImplementedByFake:
    """No route of the fake answers `method path`; `routes` are the ones it has, as `METHOD /path`."""
    operation = f"{method} {path}"
    return NotImplementedByFake(operation, closest=closest(operation, routes))


def convert(error: Exception, renders: RendersErrors, *, provider: str, host: str, method: str, path: str) -> Rendered:
    """THE converter: `error`, raised while answering `method path` for `provider`, as the answer the agent gets.
    Records how on `OUTCOME` and logs it: a refusal below error level, an unimplemented operation at info, and
    Minutehand's own error at error, with its traceback."""
    where = f"{method} {path}"
    if isinstance(error, ServiceRefusal):
        kind, failure_kind = (
            (AnswerKind.INJECTED_FAULT, FailureKind.INJECTED_FAULT)
            if error.deliberate
            else (AnswerKind.REFUSED, FailureKind.REFUSED)
        )
        logger.debug("%s refused %s on %s: %s %s", provider, where, host, error.status, error.code)
        failure = Failure.of(error, kind=failure_kind, code=error.code, message=error.message, where=where)
        _record(kind, failure)
        return error.render()
    if isinstance(error, NotImplementedError):
        nearest = error.closest if isinstance(error, NotImplementedByFake) else None
        said = f" ({error})" if str(error) and not isinstance(error, NotImplementedByFake) else ""
        message = (
            f"minutehand's {provider} fake does not implement this operation: {method} {host}{path}{said}."
            + (f" The closest it has is {nearest}." if nearest else "")
            + f" What the fake covers is listed in src/minutehand/adapters/providers/{provider}/ of minutehand."
        )
        logger.info("%s", message)
        failure = Failure.of(
            error, kind=FailureKind.NOT_IMPLEMENTED, code=NOT_IMPLEMENTED_CODE, message=message, where=where
        )
        _record(AnswerKind.NOT_IMPLEMENTED, failure)
        return _marked(renders.error(501, NOT_IMPLEMENTED_CODE, message), AnswerKind.NOT_IMPLEMENTED)
    message = f"{INTERNAL_PREFIX} {provider} {method} {path}: {type(error).__name__}: {error}"
    failure = Failure.of(error, kind=FailureKind.INTERNAL, code=INTERNAL_ERROR_CODE, message=message, where=where)
    logger.error("%s", message, exc_info=error)
    said = f"{message}\n{failure.traceback}" if debugging() and failure.traceback else message
    _record(AnswerKind.INTERNAL_ERROR, failure)
    return _marked(renders.error(500, INTERNAL_ERROR_CODE, said), AnswerKind.INTERNAL_ERROR)


def answer_for(
    error: Exception, renders: RendersErrors | None, *, provider: str, host: str, method: str, path: str
) -> Rendered:
    """`convert`, and when there is no renderer (the provider could not be built) or the renderer itself fails,
    the internal error answered in a plain JSON body, so the agent still gets an answer."""
    if renders is not None:
        try:
            return convert(error, renders, provider=provider, host=host, method=method, path=path)
        except Exception as failed:
            error = failed
    return convert(error, PLAIN, provider=provider, host=host, method=method, path=path)


class _Plain:
    """The error shape for a provider that has none to give: `{"error": code, "message": message}`."""

    def error(self, status: int, code: str, message: str) -> Rendered:
        return Rendered(
            status=status,
            content_type="application/json",
            body=json.dumps({"error": code, "message": message}).encode(),
        )


PLAIN = _Plain()


def _record(kind: AnswerKind, failure: Failure) -> None:
    outcome = OUTCOME.get()
    if outcome is not None:
        outcome.kind = kind
        outcome.failure = failure


def _marked(rendered: Rendered, kind: AnswerKind) -> Rendered:
    return rendered.model_copy(update={"headers": [*rendered.headers, (ANSWER_HEADER, kind.value)]})


class Guarded:
    """`app`, with every exception it lets out of a request turned into an answer by `convert`."""

    def __init__(self, app: ASGIApp, renders: RendersErrors, *, provider: str) -> None:
        self.app = app
        self._renders = renders
        self._provider = provider

    async def __call__(
        self, scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        held: list[Message] = []

        async def hold(message: Message) -> None:
            held.append(message)

        try:
            await self.app(scope, receive, hold)
        except Exception as error:
            await send_rendered(
                answer_for(
                    error,
                    self._renders,
                    provider=self._provider,
                    host=_host(scope),
                    method=str(scope["method"]),
                    path=str(scope["path"]),
                ),
                send,
            )
            return
        for message in held:
            await send(message)


class GuardedDelivering(Guarded):
    """A guarded app that pushes in the background, and still says so (`ports.provider.DeliversInBackground`)."""

    def __init__(self, app: ASGIApp, delivers: DeliversInBackground, renders: RendersErrors, *, provider: str) -> None:
        super().__init__(app, renders, provider=provider)
        self._delivers = delivers

    def delivering(self) -> int:
        return self._delivers.delivering()


def guarded(app: ASGIApp, renders: RendersErrors, *, provider: str) -> Guarded:
    """`app` behind the guard; an app that `DeliversInBackground` stays one."""
    delivers = app
    if isinstance(delivers, DeliversInBackground):
        return GuardedDelivering(app, delivers, renders, provider=provider)
    return Guarded(app, renders, provider=provider)


async def send_rendered(rendered: Rendered, send: Callable[[Message], Awaitable[None]]) -> None:
    """`rendered` as ASGI messages."""
    headers = [
        (b"content-type", rendered.content_type.encode("latin-1")),
        (b"content-length", str(len(rendered.body)).encode("latin-1")),
    ]
    headers += [(name.encode("latin-1"), value.encode("latin-1")) for name, value in rendered.headers]
    await send({"type": "http.response.start", "status": rendered.status, "headers": headers})
    await send({"type": "http.response.body", "body": rendered.body})


def _host(scope: Scope) -> str:
    headers = scope["headers"] if "headers" in scope else []
    assert isinstance(headers, list)
    for name, value in headers:
        if isinstance(name, bytes) and name.lower() == b"host" and isinstance(value, bytes):
            return value.decode("latin-1")
    return ""
