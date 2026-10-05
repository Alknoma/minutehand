"""What the fake answers when it is the fake that cannot: a GitHub operation it does not serve (501), a path GitHub
does not have (GitHub's own 404), and a failure of its own (500), each in GitHub's REST error shape so a client
reads the message where it reads GitHub's."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from minutehand.adapters.answering import ANSWER_HEADER
from minutehand.adapters.providers.github import wire
from minutehand.adapters.providers.github.provider import build
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.errors import AnswerKind
from tests.providers.github.github_world import API, HEADERS, IRIS, SCENARIO, START, Hub, github_seed, refusal


def _failed(response: httpx.Response) -> httpx.HTTPStatusError:
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    return raised.value


def _message(error: httpx.HTTPStatusError) -> str:
    found = error.response.json()
    assert isinstance(found, dict) and isinstance(found["documentation_url"], str), found
    message = found["message"]
    assert isinstance(message, str)
    return message


async def test_listing_pull_requests_is_refused_as_not_implemented_naming_the_operation(hub: Hub) -> None:
    """`GET /repos/{owner}/{repo}/pulls` is GitHub's and not this fake's: 501, not a 404 a client would read as
    "no such repository"."""
    async with hub.client() as http:
        error = _failed(await http.get("/repos/lanternworks/ledger/pulls"))
    assert error.response.status_code == 501
    assert error.response.headers[ANSWER_HEADER] == "not_implemented"
    message = _message(error)
    assert "does not implement" in message and "GET api.github.com/repos/lanternworks/ledger/pulls" in message


async def test_a_write_to_a_served_path_is_refused_as_not_implemented(hub: Hub) -> None:
    """`PATCH /user` is GitHub's (update the user); the fake serves only `GET /user`, and says so."""
    async with hub.client() as http:
        error = _failed(await http.patch("/user", json={"name": "Iris"}))
    assert error.response.status_code == 501
    assert error.response.headers[ANSWER_HEADER] == "not_implemented"
    assert "does not implement" in _message(error) and "GET /user" in _message(error)


async def test_a_path_under_no_root_github_has_is_refused_with_github_s_own_404(hub: Hub) -> None:
    async with hub.client() as http:
        answer = await http.get("/pull-requests/lanternworks/ledger")
    refusal(answer, 404, "Not Found")
    assert ANSWER_HEADER not in answer.headers


async def test_a_failure_of_the_fake_s_own_is_answered_500_in_github_s_shape(tmp_path: Path) -> None:
    """The store closed under the app: the first read fails, and the client gets GitHub's error shape, not a
    bare "Internal Server Error"."""
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed_with(github_seed(), SCENARIO, store)
    app = provider.app(store, clock)
    store.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=API, headers={**HEADERS, "Authorization": f"Bearer {IRIS}"}
    ) as http:
        error = _failed(await http.get("/user"))
    assert error.response.status_code == 500
    assert error.response.headers[ANSWER_HEADER] == "internal_error"
    assert _message(error).startswith("minutehand internal error while answering github GET /user: ProgrammingError")


@pytest.mark.parametrize("seeded", [github_seed(faults=[wire.ServerError(resource=wire.Resource.CORE, status=502)])])
async def test_an_armed_fault_is_recorded_as_injected_and_github_s_refusal_as_refused(hub: Hub) -> None:
    """The armed 502 answers as GitHub's, and is kept apart from the 404 GitHub itself would answer."""
    async with hub.client() as http:
        faulted = await http.get("/user")
        missing = await http.get("/repos/lanternworks/nothing")
    assert (faulted.status_code, faulted.json()) == (502, {"message": "Server Error"})
    assert ANSWER_HEADER not in faulted.headers and ANSWER_HEADER not in missing.headers
    refusal(missing, 404, "Not Found")
    answers = {call.exchange.path: call.exchange.answer for call in hub.store.calls()}
    assert answers == {"/user": AnswerKind.INJECTED_FAULT, "/repos/lanternworks/nothing": AnswerKind.REFUSED}
