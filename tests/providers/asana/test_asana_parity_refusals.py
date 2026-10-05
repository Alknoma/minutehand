"""The refusals a client's tests rely on: a token nobody seeded, a project the token cannot see, a free plan,
throttling, an expired token, and custom fields and options that do not exist. Each with Asana's status and
message, and none of them recording anything."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.provider import build
from minutehand.adapters.providers.asana.seed import AsanaSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import ProviderSeed
from tests.providers.asana.asana_workspace import SCENARIO as PLAIN
from tests.providers.asana.asana_workspace import Workspace, error
from tests.providers.asana.rich_workspace import (
    ALICE_TOKEN,
    BACKEND,
    DESIGN,
    ENGINEERING,
    INCIDENT,
    PRICING,
    PRIORITY,
    REFRESH_TOKEN,
    ROADMAP,
    SHORT_TOKEN,
    STATUS_FIELD,
    THROTTLED_AFTER,
    THROTTLED_FOR,
    UNUSED,
    WS,
    agent,
    client_as,
    got,
    option,
    rich,
)

__all__ = ["agent", "rich"]


async def test_a_token_nobody_seeded_is_refused_not_authorized(rich: Workspace) -> None:
    before = rich.store.head()
    async with client_as(rich, "pat-guessed") as stranger:
        assert error(await stranger.get("/users/me"), 401) == "Not Authorized"
    assert rich.store.head() == before


async def test_an_expired_token_is_refused_and_a_refresh_mints_one_that_works(rich: Workspace) -> None:
    async with client_as(rich, SHORT_TOKEN) as short:
        assert (await short.get("/users/me")).status_code == 200
        rich.clock.jump(rich.clock.now() + timedelta(hours=1))
        assert error(await short.get("/users/me"), 401).startswith("The bearer token has expired")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rich.provider.app(rich.store, rich.clock)), base_url="https://app.asana.com"
    ) as oauth:
        refreshed = await oauth.post(
            "/-/oauth_token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": REFRESH_TOKEN,
                "client_id": "1",
                "client_secret": "s",
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        token = refreshed.json()
        assert (token["token_type"], token["expires_in"], token["data"]["gid"]) == ("bearer", 3600, state.AGENT_GID)
        refused = await oauth.post("/-/oauth_token", data={"grant_type": "refresh_token", "refresh_token": "stolen"})
        assert (refused.status_code, refused.json()["error"]) == (400, "invalid_grant")
        code = await oauth.post("/-/oauth_token", data={"grant_type": "authorization_code", "code": "x"})
        assert (code.status_code, code.json()["error"]) == (400, "unsupported_grant_type")
    async with client_as(rich, token["access_token"]) as fresh:
        assert got(await fresh.get("/users/me"))["gid"] == state.AGENT_GID
        rich.clock.jump(rich.clock.now() + timedelta(hours=1))
        assert error(await fresh.get("/users/me"), 401).startswith("The bearer token has expired")


async def test_a_private_project_is_refused_403_and_hidden_from_listings(
    rich: Workspace, agent: httpx.AsyncClient
) -> None:
    before = rich.store.head()
    for path in (
        f"/projects/{ROADMAP}",
        f"/projects/{ROADMAP}/tasks",
        f"/projects/{ROADMAP}/project_memberships",
        f"/projects/{ROADMAP}/custom_field_settings",
        f"/tasks/{PRICING}",
    ):
        assert error(await agent.get(path), 403) == "Forbidden", path
    assert rich.store.head() == before
    assert ROADMAP not in [p["gid"] for p in got(await agent.get("/projects", params={"workspace": WS}))]
    search = got(await agent.get(f"/workspaces/{WS}/tasks/search", params={"text": "pricing"}))
    assert search == []
    async with client_as(rich, ALICE_TOKEN) as alice:
        assert got(await alice.get(f"/projects/{ROADMAP}"))["gid"] == ROADMAP
        assert got(await alice.get(f"/tasks/{PRICING}"))["gid"] == PRICING


async def test_a_project_in_a_team_the_caller_is_not_in_is_refused_403(agent: httpx.AsyncClient) -> None:
    refused = await agent.post("/projects", json={"data": {"name": "Brand", "workspace": WS, "team": DESIGN}})
    assert error(refused, 403) == "Forbidden"


async def test_an_organization_refuses_a_project_with_no_team(agent: httpx.AsyncClient) -> None:
    refused = await agent.post("/projects", json={"data": {"name": "Loose", "workspace": WS}})
    assert error(refused, 400) == (
        "If the workspace for your project is an organization, you must also supply a team to share the project with."
    )
    assert error(await agent.post("/projects", json={"data": {"workspace": WS, "team": ENGINEERING}}), 400) == (
        "name: Missing input"
    )


async def test_my_teams_need_an_organization(agent: httpx.AsyncClient) -> None:
    assert error(await agent.get("/users/me/teams"), 400) == "organization: Missing input"
    unknown = "1999999999999999"
    assert error(await agent.get("/users/me/teams", params={"organization": unknown}), 400) == (
        f"organization: Unknown object: {unknown}"
    )


async def test_an_unknown_custom_field_or_option_is_refused_400(rich: Workspace, agent: httpx.AsyncClient) -> None:
    before = rich.store.head()
    unknown = "1999999999999999"
    cases = [
        ({unknown: "1"}, f"custom_fields: Unknown object: {unknown}"),
        ({PRIORITY: unknown}, f"custom_fields.{PRIORITY}: Not a recognized enum option: {unknown}"),
        (
            {PRIORITY: option("Status", "Done")},
            f"custom_fields.{PRIORITY}: Not a recognized enum option: {option('Status', 'Done')}",
        ),
        ({UNUSED: "x"}, f"custom_fields: Custom field {UNUSED} is not on given task"),
        ({state.field_gid("Story Points"): "five"}, f"custom_fields.{state.field_gid('Story Points')}: Not a number"),
        (
            {state.field_gid("Reviewers"): ["nobody@company.com"]},
            f"custom_fields.{state.field_gid('Reviewers')}: Unknown object: nobody@company.com",
        ),
        (
            {state.field_gid("Launch"): {"date": "30/09/2026"}},
            f"custom_fields.{state.field_gid('Launch')}.date: Invalid date",
        ),
        ({"Priority": "High"}, "custom_fields: Not a Recognized ID"),
    ]
    for sent, message in cases:
        assert error(await agent.put(f"/tasks/{INCIDENT}", json={"data": {"custom_fields": sent}}), 400) == message
    created = await agent.post(
        "/tasks", json={"data": {"name": "x", "projects": [BACKEND], "custom_fields": {unknown: "1"}}}
    )
    assert error(created, 400) == f"custom_fields: Unknown object: {unknown}"
    assert rich.store.head() == before


async def test_a_field_already_on_a_project_or_unknown_is_refused_400(agent: httpx.AsyncClient) -> None:
    again = await agent.post(
        f"/projects/{BACKEND}/addCustomFieldSetting", json={"data": {"custom_field": STATUS_FIELD}}
    )
    assert error(again, 400) == f"custom_field: Custom field {STATUS_FIELD} is already applied to this project"
    nothing = await agent.post(
        f"/projects/{BACKEND}/addCustomFieldSetting", json={"data": {"custom_field": "1999999999999999"}}
    )
    assert error(nothing, 400) == "custom_field: Unknown object: 1999999999999999"
    missing = await agent.post(f"/projects/{BACKEND}/addCustomFieldSetting", json={"data": {}})
    assert error(missing, 400) == "custom_field: Missing input"


async def test_a_tag_or_parent_that_names_nothing_is_refused(agent: httpx.AsyncClient) -> None:
    unknown = "1999999999999999"
    assert (
        error(await agent.post(f"/tasks/{INCIDENT}/addTag", json={"data": {"tag": "urgent"}}), 400)
        == "tag: Not a Recognized ID"
    )
    assert (
        error(await agent.post(f"/tasks/{INCIDENT}/addTag", json={"data": {"tag": unknown}}), 400)
        == f"tag: Unknown object: {unknown}"
    )
    assert error(await agent.post("/tags", json={"data": {"name": "x"}}), 400) == "workspace: Missing input"
    assert error(await agent.post(f"/tasks/{INCIDENT}/setParent", json={"data": {}}), 400) == "parent: Missing input"
    assert error(await agent.post(f"/tasks/{INCIDENT}/setParent", json={"data": {"parent": INCIDENT}}), 400) == (
        "parent: A task cannot be a subtask of itself or of its own subtask"
    )
    sub = state.task_gid(1)
    assert error(await agent.post(f"/tasks/{INCIDENT}/setParent", json={"data": {"parent": sub}}), 400) == (
        "parent: A task cannot be a subtask of itself or of its own subtask"
    ), "a task cannot go under its own subtask"
    assert error(await agent.put(f"/tasks/{INCIDENT}", json={"data": {"parent": sub}}), 400) == (
        "parent: Cannot write this property"
    )


async def test_a_section_move_needs_a_task_that_exists(agent: httpx.AsyncClient) -> None:
    unknown = "1999999999999999"
    section = state.section_gid(BACKEND, "Done")
    assert error(await agent.post(f"/sections/{section}/addTask", json={"data": {"task": unknown}}), 400) == (
        f"task: Unknown object: {unknown}"
    )
    assert error(await agent.post(f"/sections/{unknown}/addTask", json={"data": {"task": INCIDENT}}), 404) == (
        f"section: Unknown object: {unknown}"
    )


async def test_every_call_is_answered_429_with_retry_after_while_throttled(
    rich: Workspace, agent: httpx.AsyncClient
) -> None:
    rich.clock.jump(rich.clock.now() + THROTTLED_AFTER + timedelta(seconds=30))
    before = rich.store.head()
    throttled = await agent.get(f"/tasks/{INCIDENT}")
    assert error(throttled, 429) == "You have made too many requests recently. Please, be chill."
    assert throttled.headers["Retry-After"] == str(int(THROTTLED_FOR.total_seconds()) - 30)
    assert rich.store.head() == before
    rich.clock.jump(rich.clock.now() + THROTTLED_FOR)
    assert (await agent.get(f"/tasks/{INCIDENT}")).status_code == 200


async def test_webhooks_are_said_to_be_unserved(agent: httpx.AsyncClient) -> None:
    refused = await agent.post(
        "/webhooks", json={"data": {"resource": INCIDENT, "target": "https://agent.example/hook"}}
    )
    assert error(refused, 501) == "webhooks: Not supported by this simulation of Asana"


@pytest.fixture
def free(tmp_path: Path) -> Workspace:
    clock = RunClock(PLAIN.starts_at)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    seed = AsanaSeed.model_validate(
        {
            "workspace": {"premium": False},
            "custom_fields": [{"name": "Priority", "kind": "enum", "options": [{"name": "High"}]}],
        }
    )
    scenario = PLAIN.model_copy(
        update={"provider_seeds": [ProviderSeed(provider="asana", body=seed.model_dump_json())]}
    )
    provider.seed(scenario, store)
    return Workspace(provider=provider, store=store, clock=clock, path=tmp_path / "world.db")


async def test_a_free_workspace_answers_402_to_search_and_custom_fields(free: Workspace) -> None:
    async with client_as(free, "any-token-at-all") as c:
        search = await c.get(f"/workspaces/{WS}/tasks/search", params={"text": "x"})
        assert error(search, 402) == "Search is only available to premium Asana workspaces."
        venue = state.project_gid("Venue Move")
        setting = await c.post(
            f"/projects/{venue}/addCustomFieldSetting", json={"data": {"custom_field": state.field_gid("Priority")}}
        )
        assert error(setting, 402) == "Custom fields are only available to premium Asana workspaces."
