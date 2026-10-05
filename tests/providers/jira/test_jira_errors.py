"""What a Jira client sees when the fake cannot answer as Jira would: an operation Jira Cloud has and the fake does
not is a 501, and Minutehand's own failure a 500, both in Jira's `{"errorMessages": [...], "errors": {}}`, so the
client raises the error it raises for any Jira failure with the message intact."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.jira.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.errors import AnswerKind
from tests.providers.jira.jira_site import AGENT_EMAIL, AGENT_TOKEN, API, SCENARIO, SITE, START, Site, basic


def _failed(response: httpx.Response) -> str:
    """The error httpx raises for the answer, and the one message Jira's body carries."""
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    body = raised.value.response.json()
    assert set(body) == {"errorMessages", "errors"} and body["errors"] == {}, body
    [message] = body["errorMessages"]
    return message


async def test_worklogs_the_fake_does_not_serve_are_refused_501_naming_the_operation(site: Site) -> None:
    """Deliberately asks for an operation Jira Cloud has (an issue's worklogs) and the fake does not."""
    response = await site.http.get(f"{API}/issue/LAUNCH-1/worklog")
    assert response.status_code == 501
    assert response.headers["x-minutehand-answer"] == "not_implemented"
    message = _failed(response)
    assert "does not implement this operation: GET lanternworks.atlassian.net/rest/api/3/issue/LAUNCH-1/worklog" in (
        message
    )
    [call] = [c for c in site.store.calls() if c.exchange.path.endswith("/worklog")]
    assert call.exchange.answer is AnswerKind.NOT_IMPLEMENTED


async def test_a_method_the_fake_does_not_serve_on_a_known_path_is_refused_501_naming_the_closest(
    site: Site,
) -> None:
    """Deliberately updates a project: Jira Cloud has `PUT /project/{key}`, the fake only reads one."""
    message = _failed(await site.http.put(f"{API}/project/LAUNCH", json={"name": "Launch again"}))
    assert "does not implement this operation: PUT" in message
    assert "The closest it has is POST /rest/api/3/project." in message


async def test_a_gateway_path_the_fake_does_not_serve_is_refused_501(site: Site) -> None:
    """Deliberately asks the OAuth gateway for something other than a Jira site or the accessible resources."""
    response = await site.http.get("https://api.atlassian.com/ex/confluence/somewhere/wiki/rest/api/space")
    assert response.headers["x-minutehand-answer"] == "not_implemented"
    assert "does not implement this operation" in _failed(response)


async def test_a_declared_rate_limit_is_recorded_as_an_injected_fault_and_a_404_as_refused(site: Site) -> None:
    body = {"body": {"type": "doc", "version": 1, "content": []}}
    limited = await site.http.post(f"{API}/issue/LAUNCH-2/comment", json=body)
    assert limited.status_code == 429 and "x-minutehand-answer" not in limited.headers
    missing = await site.http.get(f"{API}/issue/LAUNCH-99")
    assert missing.status_code == 404 and "x-minutehand-answer" not in missing.headers
    answers = {c.exchange.path: c.exchange.answer for c in site.store.calls()}
    assert answers["/rest/api/3/issue/LAUNCH-2/comment"] is AnswerKind.INJECTED_FAULT
    assert answers["/rest/api/3/issue/LAUNCH-99"] is AnswerKind.REFUSED


async def test_minutehands_own_failure_is_a_500_in_jiras_shape(tmp_path: Path) -> None:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO, store)
    app = provider.app(store, clock)
    store.close()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=SITE,
        headers={"Authorization": basic(AGENT_EMAIL, AGENT_TOKEN)},
    ) as http:
        response = await http.get("/rest/api/3/myself")
    assert response.status_code == 500
    assert response.headers["x-minutehand-answer"] == "internal_error"
    message = _failed(response)
    assert message.startswith("minutehand internal error while answering jira GET /rest/api/3/myself: ")
    assert "ProgrammingError" in message
