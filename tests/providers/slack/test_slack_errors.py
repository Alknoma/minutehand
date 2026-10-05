"""What a `slack_sdk` caller gets when the fake cannot answer: a method or a path the fake does not have, and
Minutehand's own error. Each reaches the SDK as `SlackApiError` over the Web API's `{"ok": false, "error": …}`, the
message in `response_metadata.messages`, marked `x-minutehand-answer` so nobody mistakes it for Slack's answer; a
refusal Slack itself makes is not marked, and a fault the scenario armed is recorded as one."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import uvicorn
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from minutehand.adapters.answering import ANSWER_HEADER, INTERNAL_PREFIX, OUTCOME, Outcome
from minutehand.adapters.providers.slack.provider import build
from minutehand.adapters.providers.slack.seed import FaultSeed, Refused, SlackSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.errors import AnswerKind
from minutehand.domain.scenario import ProviderSeed
from minutehand.ports.provider import ASGIApp
from tests.providers.slack.slack_workspace import GENERAL, SCENARIO, START, TOKEN, Workspace, client_for


async def _serving(app: ASGIApp) -> tuple[uvicorn.Server, asyncio.Task[None], WebClient]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical", lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, serving, WebClient(token=TOKEN, base_url=f"http://127.0.0.1:{port}/api/")


@pytest.fixture
async def sdk(workspace: Workspace) -> AsyncIterator[WebClient]:
    server, serving, client = await _serving(workspace.provider.app(workspace.store, workspace.clock))
    yield client
    server.should_exit = True
    await serving


async def _raised(call: Callable[[], object]) -> SlackApiError:
    def calling() -> SlackApiError:
        with pytest.raises(SlackApiError) as raised:
            call()
        return raised.value

    return await asyncio.to_thread(calling)


def _said(error: SlackApiError) -> str:
    """What the answer says beside its code: `response_metadata.messages`, Slack's place for it."""
    metadata = error.response["response_metadata"]
    assert isinstance(metadata, dict)
    [message] = metadata["messages"]
    assert isinstance(message, str)
    return message


async def test_a_slack_method_the_fake_does_not_answer_is_a_slack_api_error_naming_it(sdk: WebClient) -> None:
    error = await _raised(lambda: sdk.chat_scheduleMessage(channel=GENERAL, text="later", post_at=1_900_000_000))

    assert error.response.status_code == 501
    assert error.response.headers[ANSWER_HEADER] == "not_implemented"
    assert error.response["error"] == "minutehand_not_implemented"
    message = _said(error)
    assert "does not implement this operation: POST" in message and "/api/chat.scheduleMessage" in message
    assert "The closest it has is POST /api/chat.postMessage" in message
    assert "does not implement" in str(error)


async def test_minutehands_own_error_is_a_slack_api_error_in_slacks_shape(sdk: WebClient, workspace: Workspace) -> None:
    workspace.store.close()
    error = await _raised(sdk.auth_test)

    assert error.response.status_code == 500
    assert error.response.headers[ANSWER_HEADER] == "internal_error"
    assert error.response["error"] == "minutehand_internal_error"
    assert _said(error).startswith(f"{INTERNAL_PREFIX} slack POST /api/auth.test: ProgrammingError")
    assert INTERNAL_PREFIX in str(error)


async def test_a_path_the_fake_does_not_serve_is_not_implemented_naming_it(client: httpx.AsyncClient) -> None:
    response = await client.get("/oauth/v2/authorize", params={"client_id": "1.2"})

    assert (response.status_code, response.headers[ANSWER_HEADER]) == (501, "not_implemented")
    assert "GET slack.com/oauth/v2/authorize" in response.json()["response_metadata"]["messages"][0]


async def test_a_channel_nobody_has_is_refused_unmarked(sdk: WebClient) -> None:
    error = await _raised(lambda: sdk.conversations_info(channel="C0NOBODY"))

    assert error.response.status_code == 200
    assert ANSWER_HEADER not in error.response.headers
    assert error.response.data == {"ok": False, "error": "channel_not_found"}


async def test_a_declared_fault_is_refused_as_an_injected_fault(tmp_path: Path) -> None:
    seed = ProviderSeed(
        provider="slack",
        body=SlackSeed(faults=[FaultSeed(call="auth.test", answer=Refused(error="token_revoked"))]).model_dump_json(),
    )
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "faulted.db", "faulted", clock)
    provider = build()
    provider.seed(SCENARIO.model_copy(update={"provider_seeds": [seed]}), store)
    outcome = Outcome()
    token = OUTCOME.set(outcome)
    try:
        async with client_for(provider, store, clock) as c:
            response = await c.post("/api/auth.test", headers={"Authorization": f"Bearer {TOKEN}"})
    finally:
        OUTCOME.reset(token)

    assert response.status_code == 200 and ANSWER_HEADER not in response.headers
    assert response.json() == {"ok": False, "error": "token_revoked"}
    assert outcome.kind is AnswerKind.INJECTED_FAULT
