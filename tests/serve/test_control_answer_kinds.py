"""The control API's boundary: every exception a route lets out is one `Refusal` body with a stable `code`, and only
a typed refusal is reported as the request being refused; anything else is Minutehand's own error, 500, logged at
error. A bare `ValueError` or `LookupError` is no longer read as a refusal."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from minutehand.adapters.control.app import _BadQuery, _guarded  # pyright: ignore[reportPrivateUsage]
from minutehand.adapters.control.wire import Refusal, RefusalCode, RefusalKind
from minutehand.application.refusals import AgentFailed, NotFound
from minutehand.application.standing import Unsupported, WorldRefused
from minutehand.domain.errors import EnvironmentFailure

CONTROL = "minutehand.adapters.control.app"


class _Shaped(BaseModel):
    n: int


def _invalid() -> ValidationError:
    try:
        _Shaped.model_validate({"n": "many"})
    except ValidationError as e:
        return e
    raise AssertionError("the model accepted what it should refuse")


@dataclass(frozen=True)
class Case:
    name: str
    error: Exception
    status: int
    code: RefusalCode
    starts: str
    level: int | None
    kind: RefusalKind | None = None


CASES = [
    Case("invalid_body", _invalid(), 422, RefusalCode.INVALID, "1 validation error", None),
    Case("invalid_query", _BadQuery("?since= is a whole number"), 422, RefusalCode.INVALID, "?since=", None),
    Case("not_found", NotFound("no open world w1"), 404, RefusalCode.NOT_FOUND, "no open world w1", None),
    Case("agent_refused", AgentFailed("the agent answered 400"), 502, RefusalCode.AGENT_REFUSED, "the agent", None),
    Case(
        "unsupported",
        Unsupported("slack signs nothing"),
        409,
        RefusalCode.UNSUPPORTED,
        "slack signs nothing",
        None,
        RefusalKind.UNSUPPORTED,
    ),
    Case("refused", WorldRefused("no person zed"), 409, RefusalCode.REFUSED, "no person zed", None),
    Case(
        "environment",
        EnvironmentFailure("port 8080", "in use", "free it"),
        503,
        RefusalCode.ENVIRONMENT,
        "port 8080: in use. free it",
        logging.ERROR,
    ),
    Case(
        "bare_value_error",
        ValueError("bad"),
        500,
        RefusalCode.INTERNAL_ERROR,
        "internal error: ValueError: bad",
        logging.ERROR,
    ),
    Case(
        "bare_lookup_error",
        KeyError("w9"),
        500,
        RefusalCode.INTERNAL_ERROR,
        "internal error: KeyError: 'w9'",
        logging.ERROR,
    ),
    Case(
        "a_bug",
        RuntimeError("kaboom"),
        500,
        RefusalCode.INTERNAL_ERROR,
        "internal error: RuntimeError: kaboom",
        logging.ERROR,
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_each_kind_is_one_refusal_body_with_its_code(case: Case, caplog: pytest.LogCaptureFixture) -> None:
    async def raising(_: Request) -> Response:
        raise case.error

    app = Starlette(routes=[Route("/v1/thing", _guarded(raising))])
    caplog.set_level(logging.DEBUG, logger=CONTROL)
    with TestClient(app, raise_server_exceptions=True) as http:
        answered = http.get("/v1/thing")
    assert answered.status_code == case.status
    refusal = Refusal.model_validate_json(answered.content)
    assert refusal.code is case.code
    assert refusal.kind is case.kind
    assert refusal.error.startswith(case.starts)
    logged = [r.levelno for r in caplog.records if r.name == CONTROL]
    assert logged == ([] if case.level is None else [case.level])
