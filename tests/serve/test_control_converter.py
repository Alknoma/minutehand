"""The control API's one converter (`adapters.control.app._guarded`): each typed refusal answers its status, and
anything else is Minutehand's own failure, 500 `internal_error` logged at error, never a refusal of the caller's
request. A bare `KeyError` or `ValueError` was a 404 or a 409 before, so a bug read as the caller's mistake."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.control.app import _BadQuery, _guarded  # pyright: ignore[reportPrivateUsage]
from minutehand.adapters.control.wire import CreateWorld, Refusal, RefusalKind
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.refusals import AgentFailed, RunRefused
from minutehand.application.run_clock import RunClock
from minutehand.application.standing import NotFound, StandingWorld, UnknownWorld, Unsupported, WorldRefused
from minutehand.domain.scenario import Seed, TicketState
from minutehand.domain.world import EntityKind, EntityRef
from minutehand.testing.client import Refused
from tests.serve.support import Served

CONTROL = "minutehand.adapters.control.app"
START = datetime(2026, 9, 1, 9, tzinfo=UTC)
PEOPLE = [{"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}}]


def _invalid() -> Exception:
    try:
        CreateWorld.model_validate_json("{}")
    except ValidationError as e:
        return e
    raise AssertionError("an empty body is not a world")


@dataclass(frozen=True)
class Row:
    raised: Exception
    status: int
    kind: RefusalKind | None


ROWS = {
    "body_not_the_model": Row(_invalid(), 422, None),
    "bad_query": Row(_BadQuery("?since= is a whole number"), 422, None),
    "unknown_world": Row(UnknownWorld("no open world abc"), 404, None),
    "not_held": Row(NotFound("no Slack message 1.2 for owen to answer"), 404, None),
    "pushed_and_refused": Row(AgentFailed("the service answered 500"), 502, None),
    "unsupported": Row(Unsupported("github pushes no events"), 409, RefusalKind.UNSUPPORTED),
    "world_refused": Row(WorldRefused("the clock only moves forward"), 409, None),
    "run_refused": Row(RunRefused("no installed provider is named x"), 409, None),
    "bare_key_error": Row(KeyError("slack"), 500, RefusalKind.INTERNAL_ERROR),
    "bare_value_error": Row(ValueError("not a world"), 500, RefusalKind.INTERNAL_ERROR),
    "bare_lookup_error": Row(LookupError("nothing"), 500, RefusalKind.INTERNAL_ERROR),
    "a_bug": Row(TypeError("'NoneType' object is not subscriptable"), 500, RefusalKind.INTERNAL_ERROR),
}


@pytest.mark.parametrize("name", sorted(ROWS))
async def test_each_kind_raised_behind_the_control_api_is_answered_by_its_kind(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    row = ROWS[name]

    async def handler(_: Request) -> Response:
        raise row.raised

    app = Starlette(routes=[Route("/v1/worlds/w1/act", _guarded(handler), methods=["POST"])])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as http:
        answered = await http.post("/v1/worlds/w1/act")

    assert answered.status_code == row.status
    refusal = Refusal.model_validate_json(answered.content)
    assert refusal.kind is row.kind
    errors = [r for r in caplog.records if r.name == CONTROL and r.levelno >= logging.ERROR]
    if row.status == 500:
        assert refusal.error.startswith(
            "minutehand internal error while answering the control API POST /v1/worlds/w1/act: "
            f"{type(row.raised).__name__}: "
        )
        assert refusal.exception_type == f"builtins.{type(row.raised).__name__}"
        assert len(errors) == 1 and errors[0].exc_info is not None
    else:
        assert refusal.exception_type is None and errors == []
        assert refusal.error == (str(row.raised) if not isinstance(row.raised, ValidationError) else refusal.error)


def test_an_unknown_world_is_refused_404_by_the_running_server(served: Served) -> None:
    with pytest.raises(Refused) as refused:
        served.client.close_world("no-such-world", quiet=False)
    assert refused.value.status == 404 and refused.value.error == "no open world no-such-world"


@pytest.mark.parametrize(
    ("ticket", "raised", "words"),
    [
        (EntityRef(provider="asana", kind=EntityKind.TICKET, external_id="999"), NotFound,
         "no asana task 999 in the world: it was deleted or never created"),
        (EntityRef(provider="asana", kind=EntityKind.MESSAGE, external_id="999"), WorldRefused,
         "is not an asana task"),
    ],
)  # fmt: skip
def test_a_port_methods_lookup_or_value_error_reaches_the_control_api_as_its_typed_refusal(
    ticket: EntityRef, raised: type[WorldRefused], words: str, tmp_path: Path
) -> None:
    """A provider refuses what a world cannot do with `LookupError` or `ValueError`; the standing world turns each
    into the typed refusal the converter answers 404 or 409, with the provider's words, since a bare one is now 500."""
    registry = Registry.installed()
    manifests = {m.key: m for m in registry.manifests}
    scenario = Seed.model_validate({"starts_at": START.isoformat(), "people": PEOPLE}).starting(START)
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(tmp_path / "world.db", "world", clock)
    try:
        world = StandingWorld(
            scenario=scenario,
            store=store,
            clock=clock,
            provider=lambda k: registry.provider(manifests[k]),
            inbound=[],
            signing={},
            scripted=False,
        )
        world.open(["asana"])
        with pytest.raises(raised) as refused:
            world.move_ticket(ticket, TicketState.DONE)
        assert words in str(refused.value)
        assert type(refused.value) is raised
    finally:
        store.close()
