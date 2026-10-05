"""What an httpx caller of YouTrack gets when the fake cannot answer: a route the fake does not have, and Minutehand's
own error. Each is an `httpx.HTTPStatusError` whose body is YouTrack's `{"error": …, "error_description": …}`
carrying the message, marked `x-minutehand-answer` so nobody mistakes it for YouTrack's answer; a refusal YouTrack
itself makes is not marked, and a fault the scenario armed is recorded as one."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from minutehand.adapters.answering import ANSWER_HEADER, INTERNAL_PREFIX, OUTCOME, Outcome
from minutehand.domain.errors import AnswerKind
from tests.providers.youtrack.caller_world import scenario_with, seeded
from tests.providers.youtrack.youtrack_instance import Instance, client_for


def _raised(response: httpx.Response) -> httpx.HTTPStatusError:
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    return raised.value


async def test_a_youtrack_route_the_fake_does_not_have_is_an_http_error_naming_it(client: httpx.AsyncClient) -> None:
    error = _raised(await client.get("/api/issues/LAUNCH-1/attachments", params={"fields": "id,name"}))

    assert error.response.status_code == 501
    assert error.response.headers[ANSWER_HEADER] == "not_implemented"
    answer = error.response.json()
    assert answer["error"] == "minutehand_not_implemented"
    assert "does not implement this operation" in answer["error_description"]
    assert "GET lanternworks.youtrack.cloud/api/issues/LAUNCH-1/attachments" in answer["error_description"]
    assert "The closest it has is GET /api/issues/{issue}/" in answer["error_description"]


async def test_a_hub_route_the_fake_does_not_have_is_an_http_error_naming_it(client: httpx.AsyncClient) -> None:
    error = _raised(await client.get("/hub/api/rest/roles"))

    assert (error.response.status_code, error.response.headers[ANSWER_HEADER]) == (501, "not_implemented")
    assert "GET lanternworks.youtrack.cloud/hub/api/rest/roles" in error.response.json()["error_description"]


async def test_minutehands_own_error_is_an_http_error_in_youtracks_shape(instance: Instance) -> None:
    async with client_for(instance.provider, instance.store, instance.clock) as c:
        instance.store.close()
        error = _raised(await c.get("/api/users/me", params={"fields": "login"}))

    assert error.response.status_code == 500
    assert error.response.headers[ANSWER_HEADER] == "internal_error"
    answer = error.response.json()
    assert answer["error"] == "minutehand_internal_error"
    assert answer["error_description"].startswith(f"{INTERNAL_PREFIX} youtrack GET /api/users/me: ProgrammingError")


async def test_an_unknown_issue_is_refused_404_unmarked(client: httpx.AsyncClient) -> None:
    error = _raised(await client.get("/api/issues/LAUNCH-99"))

    assert error.response.status_code == 404
    assert ANSWER_HEADER not in error.response.headers
    assert error.response.json() == {"error": "Not Found", "error_description": "Entity with id LAUNCH-99 not found"}


async def test_a_seeded_fault_is_refused_as_an_injected_fault(tmp_path: Path) -> None:
    team = seeded(tmp_path, scenario_with(faults=[{"method": "DELETE", "path": "/issues/*", "status": 503}]))
    outcome = Outcome()
    token = OUTCOME.set(outcome)
    try:
        async with client_for(team.provider, team.store, team.clock) as c:
            error = _raised(await c.delete("/api/issues/LAUNCH-1"))
    finally:
        OUTCOME.reset(token)

    assert error.response.status_code == 503
    assert ANSWER_HEADER not in error.response.headers
    assert error.response.json() == {
        "error": "Service Unavailable",
        "error_description": "Service is temporarily unavailable",
    }
    assert outcome.kind is AnswerKind.INJECTED_FAULT
