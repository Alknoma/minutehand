"""The real `asana` SDK, over a real socket, at its real `/api/1.0` URLs, against the app served by uvicorn.

The app is served behind a wrapper that strips the manifest's path prefix with the
proxy's own `strip_prefix`, so the paths the SDK builds are the ones the proxy hands on.
The server runs on the test's own event loop; the blocking SDK runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlparse

import asana  # pyright: ignore[reportMissingTypeStubs]
import pytest
import uvicorn
from asana.rest import ApiException  # pyright: ignore[reportMissingTypeStubs]

from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.proxy.addon import strip_prefix
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, TicketSnapshot
from minutehand.ports.provider import ASGIApp, Message, Scope
from tests.providers.asana.asana_workspace import TOKEN, VENUE, WS, Workspace

T = TypeVar("T")


def behind_the_proxy(app: ASGIApp) -> ASGIApp:
    """The app as the proxy serves it: the real API's prefix removed from the path."""

    async def serve(scope: Scope, receive: Callable[[], Awaitable[Message]],
                    send: Callable[[Message], Awaitable[None]]) -> None:
        if scope["type"] == "http":
            path = scope["path"]
            assert isinstance(path, str)
            stripped = strip_prefix(path, MANIFEST.path_prefix)
            scope = {**scope, "path": stripped, "raw_path": stripped.encode()}
        await app(scope, receive, send)

    return serve


@dataclass
class Sdk:
    tasks: Any
    users: Any
    projects: Any
    stories: Any


@pytest.fixture
async def sdk(workspace: Workspace) -> AsyncIterator[Sdk]:
    server = uvicorn.Server(uvicorn.Config(
        behind_the_proxy(workspace.provider.app(workspace.store, workspace.clock)), host="127.0.0.1", port=0,
        log_level="warning", lifespan="off",
    ))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    configuration = asana.Configuration()
    configuration.access_token = TOKEN
    configuration.host = f"http://127.0.0.1:{port}{urlparse(configuration.host).path}"  # the SDK's own /api/1.0
    client = asana.ApiClient(configuration)
    yield Sdk(tasks=asana.TasksApi(client), users=asana.UsersApi(client), projects=asana.ProjectsApi(client),
              stories=asana.StoriesApi(client))
    server.should_exit = True
    await serving


async def off_loop(call: Callable[[], T]) -> T:
    return await asyncio.to_thread(call)


async def test_the_sdk_hands_a_task_to_a_person_and_reads_it_back(sdk: Sdk, workspace: Workspace) -> None:
    me = await off_loop(lambda: sdk.users.get_user("me", {}))
    projects = await off_loop(lambda: list(sdk.projects.get_projects_for_workspace(WS, {})))
    venue = next(p["gid"] for p in projects if p["name"] == "Venue Move")
    made = await off_loop(lambda: sdk.tasks.create_task(
        {"data": {"name": "Collect the badges", "projects": [venue], "assignee": "tomas@example.com"}}, {}))
    read = await off_loop(lambda: sdk.tasks.get_task(made["gid"], {"opt_fields": "name,assignee.email,completed"}))
    await off_loop(lambda: sdk.stories.create_story_for_task({"data": {"text": "By Friday please"}}, made["gid"], {}))
    found = await off_loop(lambda: list(sdk.tasks.search_tasks_for_workspace(WS, {"text": "badges"})))

    assert me["gid"] == state.AGENT_GID
    assert venue == VENUE
    assert read == {"gid": made["gid"], "name": "Collect the badges", "completed": False,
                    "assignee": {"gid": state.user_gid("tomas"), "email": "tomas@example.com"}}
    assert [t["gid"] for t in found] == [made["gid"]]
    created = next(e for e in workspace.store.events() if e.entity.external_id == made["gid"])
    assert created.actor is Actor.AGENT and created.after == TicketSnapshot(
        title="Collect the badges", project="Venue Move", assignee_email="tomas@example.com", state=TicketState.OPEN)


async def test_the_sdk_follows_next_page_to_the_last_page(sdk: Sdk, workspace: Workspace) -> None:
    for n in range(3):
        await off_loop(lambda n=n: sdk.tasks.create_task({"data": {"name": f"page {n}", "projects": [VENUE]}}, {}))
    before = workspace.store.head()
    tasks = await off_loop(lambda: list(sdk.tasks.get_tasks_for_project(VENUE, {"limit": 2})))
    names = [t["name"] for t in tasks]
    assert len(workspace.store.events(since=before)) == 3  # one listing per page
    assert names == ["Book the freight lift", "Return the old keys", "page 0", "page 1", "page 2"]


async def test_the_sdk_completes_and_deletes_a_task(sdk: Sdk) -> None:
    made = await off_loop(lambda: sdk.tasks.create_task({"data": {"name": "Short-lived", "workspace": WS}}, {}))
    done = await off_loop(lambda: sdk.tasks.update_task({"data": {"completed": True}}, made["gid"], {}))
    await off_loop(lambda: sdk.tasks.delete_task(made["gid"]))
    with pytest.raises(ApiException) as gone:
        await off_loop(lambda: sdk.tasks.get_task(made["gid"], {}))
    assert done["completed"] is True
    assert gone.value.status == 404


async def test_a_refusal_reaches_the_sdk_as_its_own_error(sdk: Sdk) -> None:
    with pytest.raises(ApiException) as refused:
        await off_loop(lambda: sdk.tasks.create_task({"data": {"name": "x", "projects": [VENUE],
                                                               "assignee": "jsmith"}}, {}))
    assert refused.value.status == 400
    answered = refused.value.body
    assert answered is not None
    assert json.loads(answered)["errors"][0]["message"] == "assignee: Not a Recognized ID"
