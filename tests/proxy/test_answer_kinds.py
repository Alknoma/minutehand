"""The proxy's boundary: each kind of exception a provider's app lets out becomes an answer in the vendor's shape,
logged at its level and recorded with its kind, and nothing is raised into mitmproxy.

The provider here (`tests.proxy.faulty`) is deliberately unguarded, so the proxy's guard is the only converter."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from minutehand.adapters.answering import ANSWER_HEADER, DEBUG_VARIABLE
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.errors import AnswerKind, FailureKind
from tests.proxy.support import client, exchanges

ANSWERING = "minutehand.adapters.answering"


@dataclass(frozen=True)
class Case:
    path: str
    status: int
    code: str | None
    message: str | None
    kind: AnswerKind
    failure: FailureKind | None
    level: int | None
    """The level the guard logs the call at; None: it logs nothing."""
    marked: bool


CASES = [
    Case("/fine", 200, None, None, AnswerKind.ANSWERED, None, None, marked=False),
    Case("/missing", 404, "not_found", "no such thing", AnswerKind.REFUSED, None, None, marked=False),
    Case(
        "/refuse", 403, "forbidden_here", "you may not", AnswerKind.REFUSED, FailureKind.REFUSED, logging.DEBUG, False
    ),
    Case(
        "/fault",
        403,
        "forbidden_here",
        "you may not",
        AnswerKind.INJECTED_FAULT,
        FailureKind.INJECTED_FAULT,
        logging.DEBUG,
        marked=False,
    ),
    Case(
        "/later",
        501,
        "minutehand_not_implemented",
        "minutehand's faulty fake does not implement this operation: GET faulty.example/later (later).",
        AnswerKind.NOT_IMPLEMENTED,
        FailureKind.NOT_IMPLEMENTED,
        logging.INFO,
        marked=True,
    ),
    Case(
        "/boom",
        500,
        "minutehand_internal_error",
        "minutehand internal error while answering faulty GET /boom: RuntimeError: kaboom",
        AnswerKind.INTERNAL_ERROR,
        FailureKind.INTERNAL,
        logging.ERROR,
        marked=True,
    ),
]


@pytest.fixture
def faulty() -> Registry:
    found = Registry()
    found.discover("tests.proxy.faulty")
    return found


@pytest.mark.parametrize("case", CASES, ids=[c.path.strip("/") for c in CASES])
async def test_each_kind_is_answered_logged_and_recorded(
    case: Case,
    faulty: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    world_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=ANSWERING)
    async with Proxy(Routing(faulty), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            response = await http.get(f"https://faulty.example{case.path}")
    assert response.status_code == case.status
    if case.code is None:
        assert response.json() == {"ok": True}
    else:
        said = response.json()["fault"]
        assert said["code"] == case.code
        assert case.message is not None
        assert said["message"].startswith(case.message)
    assert (response.headers.get(ANSWER_HEADER) == case.kind.value) is case.marked
    [(_, _, exchange)] = exchanges(world_path)
    assert exchange.answer is case.kind
    assert (exchange.failure.kind if exchange.failure is not None else None) is case.failure
    logged = [r.levelno for r in caplog.records if r.name == ANSWERING]
    assert logged == ([] if case.level is None else [case.level])


async def test_an_internal_error_keeps_its_traceback_on_the_record_and_out_of_the_body(
    faulty: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, world_path: Path
) -> None:
    async with Proxy(Routing(faulty), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            response = await http.get("https://faulty.example/boom")
    assert "Traceback" not in response.text
    [(_, _, exchange)] = exchanges(world_path)
    assert exchange.failure is not None and exchange.failure.traceback is not None
    assert 'raise RuntimeError("kaboom")' in exchange.failure.traceback
    assert exchange.failure.where == "GET /boom"
    assert exchange.failure.causes[0].type == "builtins.RuntimeError"


async def test_under_debug_an_internal_error_s_traceback_is_in_the_body(
    faulty: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEBUG_VARIABLE, "1")
    async with Proxy(Routing(faulty), store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            response = await http.get("https://faulty.example/boom")
    message = json.loads(response.text)["fault"]["message"]
    assert "Traceback" in message and 'raise RuntimeError("kaboom")' in message
