"""What `notion-client` raises when the fake cannot answer: an endpoint Notion has and the fake does not build, and
Minutehand's own error. Each is the SDK's own `APIResponseError` with the message as its text, in Notion's error
object, marked `x-minutehand-answer` so nobody mistakes it for Notion's answer; a refusal Notion itself makes is not
marked, and a fault the scenario armed is recorded as one."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from notion_client import APIResponseError, AsyncClient

from minutehand.adapters.answering import ANSWER_HEADER, INTERNAL_PREFIX, OUTCOME, Outcome
from minutehand.domain.errors import AnswerKind
from tests.providers.notion.notion_world import AGENT_TOKEN, NOTION, Sdk, World, scenario, seeded


@asynccontextmanager
async def over_asgi(world: World) -> AsyncIterator[AsyncClient]:
    """The official async SDK, straight into the provider's app."""
    transport = httpx.ASGITransport(app=world.provider.app(world.store, world.clock))
    client = AsyncClient(auth=AGENT_TOKEN, client=httpx.AsyncClient(transport=transport))
    try:
        yield client
    finally:
        await client.aclose()


async def test_an_endpoint_the_fake_does_not_build_is_raised_by_the_sdk_naming_it(sdk: Sdk) -> None:
    with pytest.raises(APIResponseError) as raised:
        await sdk(lambda c: c.request(path="file_uploads", method="POST", body={"mode": "single_part"}))

    error = raised.value
    assert error.status == 501
    assert error.headers[ANSWER_HEADER] == "not_implemented"
    assert "does not implement this operation: POST api.notion.com/v1/file_uploads" in str(error)


async def test_minutehands_own_error_is_raised_by_the_sdk_in_notions_shape(tmp_path: Path) -> None:
    world = seeded(tmp_path)
    async with over_asgi(world) as client:
        world.store.close()
        with pytest.raises(APIResponseError) as raised:
            await client.users.me()

    error = raised.value
    assert error.status == 500
    assert error.headers[ANSWER_HEADER] == "internal_error"
    assert str(error).startswith(f"{INTERNAL_PREFIX} notion GET /v1/users/me: ProgrammingError")


async def test_an_unknown_page_is_refused_404_unmarked(tmp_path: Path) -> None:
    world = seeded(tmp_path)
    try:
        async with over_asgi(world) as client:
            with pytest.raises(APIResponseError) as raised:
                await client.pages.retrieve("00000000-0000-4000-8000-000000000000")
    finally:
        world.store.close()

    assert (raised.value.status, raised.value.code) == (404, "object_not_found")
    assert ANSWER_HEADER not in raised.value.headers


async def test_a_seeded_rate_limit_is_refused_as_an_injected_fault(tmp_path: Path) -> None:
    world = seeded(tmp_path, scenario({**NOTION, "faults": [{"kind": "rate_limited", "retry_after": 3}]}))
    outcome = Outcome()
    token = OUTCOME.set(outcome)
    try:
        async with over_asgi(world) as client:
            with pytest.raises(APIResponseError) as raised:
                await client.search()
    finally:
        OUTCOME.reset(token)
        world.store.close()

    assert (raised.value.status, raised.value.code) == (429, "rate_limited")
    assert raised.value.headers["retry-after"] == "3"
    assert ANSWER_HEADER not in raised.value.headers
    assert outcome.kind is AnswerKind.INJECTED_FAULT
