"""A service call the reliable agent makes that is not answered 2xx is never taken for an answer. On Luna 6 a read
answered 502 just after the approver asked back was taken for "nothing changed": the backoff moved on, and the
ask-back was seen four hours late. A failed read is no read, and the agent tries again in minutes."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "reliable_agent"
NOW = datetime(2026, 8, 27, 11, 0, tzinfo=UTC)  # a Thursday, in working hours


@pytest.fixture
def agent() -> Iterator[ModuleType]:
    names = ("agent", "state", "planner", "moves", "ledger", "sender", "model", "work")
    for name in names:  # another example's `agent` or `model` may be loaded under the same name
        sys.modules.pop(name, None)
    sys.path.insert(0, str(EXAMPLE))
    try:
        import agent as module  # the example's own modules, by their names

        from minutehand.agent import store

        store.configure(store.MemoryBackend())
        yield module
    finally:
        sys.path.remove(str(EXAMPLE))
        for name in names:
            sys.modules.pop(name, None)


def _answering(status: int, body: dict[str, object]) -> object:
    def request(method: str, url: str, **_: object) -> httpx.Response:
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    return request


def _held(agent: ModuleType) -> Any:  # the example's State, loaded by its name
    state = agent.State()
    state.facts = {"cost_centre": {"value": "CC-4410", "source": "sam"}}
    state.request = agent.Request(
        id="req_42", status="pending", read_at=NOW.isoformat(), next_check=NOW.isoformat(), unchanged_reads=2
    )
    return state


def test_a_read_answered_with_an_error_is_no_read(agent: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent.httpx, "request", _answering(502, {"error": "the service could not answer"}))
    state = _held(agent)
    before = state.request

    with pytest.raises(httpx.HTTPStatusError):
        agent.read_request(state, NOW)

    assert state.request == before, "its state is not the item's, and the backoff did not move"


def test_a_wake_whose_read_fails_tries_again_in_minutes_not_on_the_next_quiet_look(
    agent: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent.httpx, "request", _answering(502, {"error": "the service could not answer"}))
    agent.save(_held(agent))

    agent.wake(NOW)

    after = agent.load()
    assert after.request is not None and after.request.unchanged_reads == 2
    assert after.retry_at is not None
    assert datetime.fromisoformat(after.retry_at) - NOW <= timedelta(minutes=15)
    assert any(b.startswith("model or service failed") for b in after.blocked)
