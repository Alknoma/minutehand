"""What leaves a provider's app, answered at the proxy by the one converter (`adapters.answering`): a refusal in the
vendor's own shape, an operation the fake does not implement as 501 in the vendor's error shape, anything else as
Minutehand's own 500 in the vendor's error shape; each logged by its kind and recorded on the call with its kind.

`Brittle` is a test-only provider: each route lets one kind out of a real Starlette app, so the guard meets what
Starlette's `ServerErrorMiddleware` does (send its own 500, then re-raise) rather than a bare exception."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from minutehand.adapters import answering
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.provider import Manifest, Tier
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import CallOutcome
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store
from tests.proxy.support import START, client, exchanges

HOST = "brittle.test"
MANIFEST = Manifest(key="brittle", tier=Tier.FINISHED, hosts=[HOST])
CONVERTER = "minutehand.adapters.answering"


class TurnedAway(ServiceRefusal):
    """The test service's own refusal, in its own shape."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def render(self, asked: Asked) -> Rendered:
        body = json.dumps({"turned_away": self.reason, "path": asked.path}).encode()
        return Rendered(status=409, content_type="application/json", body=body, headers=[("x-brittle", "refused")])


class Brittle:
    """A test-only provider: each route lets one kind of answer, or exception, out of its app."""

    manifest = MANIFEST

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        async def answered(_: Request) -> JSONResponse:
            return JSONResponse({"ok": True})

        async def refused(_: Request) -> JSONResponse:
            raise TurnedAway("closed on sundays")

        async def refused_in_app(_: Request) -> JSONResponse:
            return JSONResponse({"error": "no such thing"}, status_code=404)

        async def injected(_: Request) -> JSONResponse:
            answering.injected()
            return JSONResponse({"error": "failed on purpose"}, status_code=503)

        async def unimplemented(_: Request) -> JSONResponse:
            raise NotImplementedError("frobnicating a widget")

        async def broken(_: Request) -> JSONResponse:
            raise RuntimeError("the ledger is torn")

        return Starlette(
            routes=[
                Route("/answered", answered),
                Route("/refused", refused),
                Route("/refused-in-app", refused_in_app),
                Route("/injected", injected),
                Route("/unimplemented", unimplemented, methods=["POST"]),
                Route("/broken", broken),
            ]
        )

    def seed(self, scenario: Scenario, world: Store) -> None:
        """Brittle keeps nothing a scenario could seed."""

    def error(self, status: int, code: str, message: str) -> Rendered:
        body = json.dumps({"brittle_error": {"code": code, "message": message}}).encode()
        return Rendered(status=status, content_type="application/json", body=body)


@dataclass(frozen=True)
class Row:
    method: str
    path: str
    status: int
    body: dict[str, object] | None
    """The whole answer, when it is fixed; the message's start is checked otherwise."""
    message: str | None
    """For Minutehand's own answers: how `brittle_error.message` begins."""
    kind: CallOutcome
    logged: int | None
    """The level the converter logs it at; None when the converter is not involved."""


ROWS = [
    Row("GET", "/answered", 200, {"ok": True}, None, CallOutcome.ANSWERED, None),
    Row("GET", "/refused", 409, {"turned_away": "closed on sundays", "path": "/refused"}, None, CallOutcome.REFUSED,
        logging.DEBUG),
    Row("GET", "/refused-in-app", 404, {"error": "no such thing"}, None, CallOutcome.REFUSED, None),
    Row("GET", "/injected", 503, {"error": "failed on purpose"}, None, CallOutcome.INJECTED_FAULT, None),
    Row("POST", "/unimplemented", 501, None,
        "minutehand's brittle fake does not implement POST /unimplemented: frobnicating a widget",
        CallOutcome.NOT_IMPLEMENTED, logging.INFO),
    Row("GET", "/broken", 500, None,
        "minutehand internal error while answering brittle GET /broken: RuntimeError: the ledger is torn",
        CallOutcome.INTERNAL_ERROR, logging.ERROR),
]  # fmt: skip


@pytest.mark.parametrize("row", ROWS, ids=[r.path.strip("/") for r in ROWS])
async def test_each_kind_leaving_a_providers_app_is_answered_logged_and_recorded_by_its_kind(
    row: Row, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=CONVERTER)
    registry = Registry()
    registry.register(MANIFEST, Brittle)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            answered = await http.request(row.method, f"https://{HOST}{row.path}")
    store.close()

    assert answered.status_code == row.status, answered.text
    if row.body is not None:
        assert answered.json() == row.body
    if row.message is not None:
        said = answered.json()["brittle_error"]
        assert said["code"] == ("not_implemented" if row.status == 501 else "internal_error")
        assert said["message"].startswith(row.message)
    if row.kind is CallOutcome.REFUSED and row.logged is not None:
        assert answered.headers["x-brittle"] == "refused"
    assert "ASGI Error" not in answered.text

    levels = [r.levelno for r in caplog.records if r.name == CONVERTER]
    assert levels == ([] if row.logged is None else [row.logged])
    if row.kind is not CallOutcome.INTERNAL_ERROR:
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "only Minutehand's own error is one"

    [(_, _, exchange)] = exchanges(tmp_path / "world.db")
    assert exchange.status == row.status
    assert exchange.outcome is row.kind
    if row.kind is CallOutcome.INTERNAL_ERROR:
        failure = exchange.failure
        assert failure is not None and failure.kind is CallOutcome.INTERNAL_ERROR
        assert failure.exception_type == "builtins.RuntimeError"
        assert failure.message.startswith(row.message or "")
        assert failure.traceback is not None and "the ledger is torn" in failure.traceback
        assert "Traceback" in failure.traceback
    elif row.kind is CallOutcome.NOT_IMPLEMENTED:
        failure = exchange.failure
        assert failure is not None and failure.kind is CallOutcome.NOT_IMPLEMENTED
        assert failure.exception_type == "builtins.NotImplementedError" and failure.traceback is None
    else:
        assert exchange.failure is None


class Unbuildable:
    """A test-only provider whose `build()` fails: what the proxy answered as "failed to load", and re-raised."""

    manifest = Manifest(key="unbuildable", tier=Tier.FINISHED, hosts=["unbuildable.test"])

    def __init__(self) -> None:
        raise ImportError("the provider's module is missing a dependency")

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        raise AssertionError("never built")

    def seed(self, scenario: Scenario, world: Store) -> None:
        raise AssertionError("never built")


async def test_a_provider_that_cannot_be_built_is_answered_as_minutehands_own_error_and_recorded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=CONVERTER)
    registry = Registry()
    registry.register(Unbuildable.manifest, Unbuildable)
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            answered = await http.get("https://unbuildable.test/anything")
    store.close()

    assert answered.status_code == 500
    assert answered.json()["error"] == "internal_error"
    assert answered.json()["message"].startswith(
        "minutehand internal error while answering unbuildable GET /anything: ImportError: the provider's module"
    )
    assert [r.levelno for r in caplog.records if r.name == CONVERTER] == [logging.ERROR]
    [(_, _, exchange)] = exchanges(tmp_path / "world.db")
    assert exchange.outcome is CallOutcome.INTERNAL_ERROR
    assert exchange.failure is not None and exchange.failure.exception_type == "builtins.ImportError"
