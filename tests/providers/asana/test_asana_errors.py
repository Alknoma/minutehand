"""What the fake cannot answer reaches the agent through the real `asana` SDK as the SDK's own `ApiException`,
in Asana's envelope, with the reason intact; and every refusal is recorded as the kind of answer it is."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import asana  # pyright: ignore[reportMissingTypeStubs]
import httpx
import pytest
import uvicorn
from asana.rest import ApiException  # pyright: ignore[reportMissingTypeStubs]
from urllib3.util.retry import Retry

from minutehand.adapters.answering import ANSWER_HEADER, INTERNAL_PREFIX, OUTCOME, Outcome
from minutehand.domain.errors import AnswerKind
from tests.providers.asana.asana_workspace import TOKEN, WS, Workspace
from tests.providers.asana.rich_workspace import AGENT_TOKEN, INCIDENT, THROTTLED_AFTER, client_as, rich
from tests.providers.asana.test_asana_sdk_client import behind_the_proxy, off_loop

__all__ = ["rich"]


@pytest.fixture
async def sdk(workspace: Workspace) -> AsyncIterator[asana.ApiClient]:
    """The real SDK, over a real socket, against the provider's app as `app()` hands it out."""
    server = uvicorn.Server(
        uvicorn.Config(
            behind_the_proxy(workspace.provider.app(workspace.store, workspace.clock)),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    configuration = asana.Configuration()
    configuration.access_token = TOKEN
    configuration.host = f"http://127.0.0.1:{port}{urlparse(configuration.host).path}"
    configuration.retry_strategy = Retry(total=0, status_forcelist=[])  # the SDK retries a 500 with backoff
    yield asana.ApiClient(configuration)
    server.should_exit = True
    await serving


def answer_of(raised: ApiException) -> tuple[int, str, str]:
    """The status, the `x-minutehand-answer` header and the message of Asana's envelope, as the SDK kept them."""
    status, headers, body = raised.status, raised.headers, raised.body
    assert isinstance(status, int) and headers is not None and isinstance(body, str | bytes)
    errors = json.loads(body)["errors"]
    assert len(errors) == 1
    return status, headers[ANSWER_HEADER], errors[0]["message"]


async def test_an_operation_the_fake_does_not_implement_reaches_the_sdk_as_its_own_error(sdk: asana.ApiClient) -> None:
    webhooks: Any = asana.WebhooksApi(sdk)
    with pytest.raises(ApiException) as raised:
        await off_loop(lambda: list(webhooks.get_webhooks(WS, {})))
    status, answer, message = answer_of(raised.value)
    assert (status, answer) == (501, AnswerKind.NOT_IMPLEMENTED.value)
    assert message.startswith("minutehand's asana fake does not implement this operation: GET 127.0.0.1:")
    assert "/webhooks (webhooks: Not supported by this simulation of Asana)" in message
    assert "does not implement" in str(raised.value)


async def test_an_internal_error_reaches_the_sdk_as_its_own_error(sdk: asana.ApiClient, workspace: Workspace) -> None:
    users: Any = asana.UsersApi(sdk)
    workspace.store.close()
    with pytest.raises(ApiException) as raised:
        await off_loop(lambda: users.get_user("me", {}))
    status, answer, message = answer_of(raised.value)
    assert (status, answer) == (500, AnswerKind.INTERNAL_ERROR.value)
    assert message.startswith(f"{INTERNAL_PREFIX} asana GET /users/me: ProgrammingError: ")
    assert INTERNAL_PREFIX in str(raised.value)


async def answered(client: httpx.AsyncClient, method: str, path: str) -> tuple[httpx.Response, AnswerKind]:
    """The answer to one call, and how the guard recorded it."""
    outcome = Outcome()
    token = OUTCOME.set(outcome)
    try:
        response = await client.request(method, path)
    finally:
        OUTCOME.reset(token)
    return response, outcome.kind


async def test_a_throttled_call_is_refused_as_an_injected_fault_and_a_refusal_as_refused(rich: Workspace) -> None:
    async with client_as(rich, AGENT_TOKEN) as agent, client_as(rich, "nobody-seeded-this") as stranger:
        refused, refused_as = await answered(stranger, "GET", "/users/me")
        unrouted, unrouted_as = await answered(agent, "GET", "/portfolios")
        rich.clock.jump(rich.clock.now() + THROTTLED_AFTER)
        throttled, throttled_as = await answered(agent, "GET", f"/tasks/{INCIDENT}")
    assert (refused.status_code, refused_as) == (401, AnswerKind.REFUSED)
    assert (unrouted.status_code, unrouted_as) == (404, AnswerKind.REFUSED)
    assert (throttled.status_code, throttled_as) == (429, AnswerKind.INJECTED_FAULT)
    assert "retry-after" in throttled.headers
    assert all(ANSWER_HEADER not in r.headers for r in (refused, unrouted, throttled))
