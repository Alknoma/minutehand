"""The fake refuses what Asana refuses, with Asana's status and message, and records nothing for it.

Ported from the refusal suite of the emulator this provider replaces. Each case
there was a leniency that hid a real defect: an assignee spelled as a handle
accepted, a write without its `data` wrapper stored. The cases about project
creation, custom fields, tags, teams, the free-workspace 402 and throttling
are in `test_asana_parity_refusals.py`.
"""

from __future__ import annotations

import httpx
import pytest

from minutehand.adapters.providers.asana import state
from tests.providers.asana.asana_workspace import VENUE, WS, Workspace, create, error, items, unserved

UNKNOWN = "1999999999999999"


async def test_a_call_with_no_token_or_any_token_acts_as_the_agent(client: httpx.AsyncClient) -> None:
    """Minutehand does not enforce credentials: no Authorization, a Basic one, an empty bearer or any bearer token
    is answered, as the agent."""
    for headers in (
        {"Authorization": ""},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer anything-at-all"},
    ):
        answered = await client.get("/users/me", headers=headers)
        assert answered.status_code == 200, (headers, answered.text)
        assert answered.json()["data"]["gid"] == state.AGENT_GID


@pytest.mark.parametrize(
    "path, resource",
    [
        ("/tasks/{}", "task"),
        ("/projects/{}", "project"),
        ("/users/{}", "user"),
        ("/workspaces/{}", "workspace"),
        ("/projects/{}/tasks", "project"),
        ("/tasks/{}/stories", "task"),
        ("/workspaces/{}/tasks/search", "workspace"),
    ],
)
async def test_an_unknown_gid_is_refused_404(client: httpx.AsyncClient, path: str, resource: str) -> None:
    assert error(await client.get(path.format(UNKNOWN)), 404) == f"{resource}: Unknown object: {UNKNOWN}"


@pytest.mark.parametrize(
    "path, resource",
    [
        ("/tasks/{}", "task"),
        ("/projects/{}", "project"),
        ("/users/{}", "user"),
        ("/workspaces/{}/projects", "workspace"),
    ],
)
async def test_a_malformed_gid_is_refused_400(client: httpx.AsyncClient, path: str, resource: str) -> None:
    assert error(await client.get(path.format("not-a-gid")), 400) == f"{resource}: Not a Recognized ID"


async def test_a_gid_of_another_kind_is_refused_as_unknown(client: httpx.AsyncClient) -> None:
    assert error(await client.get(f"/projects/{state.user_gid('iris')}"), 404).endswith(
        "Unknown object: " + state.user_gid("iris")
    )


async def test_put_and_delete_of_an_unknown_task_are_refused_404(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    before = workspace.store.head()
    put = await client.put(f"/tasks/{UNKNOWN}", json={"data": {"name": "x"}})
    assert error(put, 404) == f"task: Unknown object: {UNKNOWN}"
    assert error(await client.delete(f"/tasks/{UNKNOWN}"), 404) == f"task: Unknown object: {UNKNOWN}"
    assert workspace.store.head() == before


@pytest.mark.parametrize("sent", [{}, {"data": "not an object"}, ["data"]])
async def test_a_write_without_its_data_wrapper_is_refused(
    workspace: Workspace, client: httpx.AsyncClient, sent: object
) -> None:
    before = workspace.store.head()
    assert error(await client.post("/tasks", json=sent), 400) == "Missing input: data"
    assert workspace.store.head() == before


async def test_a_body_that_is_not_json_is_refused(client: httpx.AsyncClient) -> None:
    refused = await client.post("/tasks", content=b"{not json", headers={"Content-Type": "application/json"})
    assert error(refused, 400) == "Could not parse request data, invalid JSON"


async def test_a_task_needs_a_workspace_or_a_project_is_refused_without(client: httpx.AsyncClient) -> None:
    assert error(await client.post("/tasks", json={"data": {"name": "x"}}), 400) == "Missing input: workspace"


@pytest.mark.parametrize(
    "assignee, message",
    [
        ("jsmith", "assignee: Not a Recognized ID"),
        (42, "assignee: Not a Recognized ID"),
        ("nobody@example.com", "assignee: Not a user in Organization: nobody@example.com"),
        (UNKNOWN, f"assignee: Not a user in Organization: {UNKNOWN}"),
    ],
)
async def test_an_unknown_assignee_is_refused(
    workspace: Workspace, client: httpx.AsyncClient, assignee: object, message: str
) -> None:
    before = workspace.store.head()
    created = await client.post("/tasks", json={"data": {"name": "x", "projects": [VENUE], "assignee": assignee}})
    assert error(created, 400) == message
    task = items(await client.get(f"/projects/{VENUE}/tasks"))[0]
    updated = await client.put(f"/tasks/{task['gid']}", json={"data": {"assignee": assignee}})
    assert error(updated, 400) == message
    assert workspace.store.head() == before + 1  # the one listing


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"projects": VENUE}, "projects: Not an array"),
        ({"projects": ["Venue Move"]}, "projects: Not a Recognized ID"),
        ({"projects": [UNKNOWN]}, f"projects: Unknown object: {UNKNOWN}"),
        ({"projects": [state.user_gid("iris")]}, f"projects: Unknown object: {state.user_gid('iris')}"),
        ({"workspace": UNKNOWN}, f"workspace: Unknown object: {UNKNOWN}"),
        ({"workspace": "my workspace"}, "workspace: Not a Recognized ID"),
        ({"workspace": WS, "completed": "yes"}, "completed: Not a boolean"),
        ({"workspace": WS, "due_on": "next friday"}, "due_on: Invalid date"),
        ({"workspace": WS, "due_on": "2026-02-30"}, "due_on: Invalid date"),
        ({"workspace": WS, "due_at": "soon"}, "due_at: Invalid datetime"),
        (
            {"workspace": WS, "due_on": "2026-09-01", "due_at": "2026-09-01T10:00:00Z"},
            "You may only provide one of due_on or due_at!",
        ),
    ],
)
async def test_a_create_is_refused_the_way_asana_refuses_it(
    client: httpx.AsyncClient, fields: dict[str, object], message: str
) -> None:
    assert error(await client.post("/tasks", json={"data": {"name": "x", **fields}}), 400) == message


async def test_a_project_not_in_the_workspace_is_refused(client: httpx.AsyncClient) -> None:
    refused = await client.post("/tasks", json={"data": {"name": "x", "workspace": WS, "projects": [UNKNOWN]}})
    assert error(refused, 400) == f"projects: Unknown object: {UNKNOWN}"


async def test_moving_a_task_between_projects_or_workspaces_by_put_is_refused(client: httpx.AsyncClient) -> None:
    made = await create(client, name="x", workspace=WS)
    for name, value in (("projects", [VENUE]), ("workspace", WS)):
        refused = await client.put(f"/tasks/{made['gid']}", json={"data": {name: value}})
        assert error(refused, 400) == f"{name}: Cannot write this property"


async def test_a_field_this_simulation_does_not_serve_is_refused_by_name(client: httpx.AsyncClient) -> None:
    refused = await client.post("/tasks", json={"data": {"workspace": WS, "followers": ["me"]}})
    assert unserved(refused) == "followers"


async def test_a_comment_with_nothing_to_say_is_refused(client: httpx.AsyncClient) -> None:
    made = await create(client, name="x", workspace=WS)
    for sent in ({"data": {}}, {"data": {"text": "   "}}):
        assert error(await client.post(f"/tasks/{made['gid']}/stories", json=sent), 400) == "Missing input: text"
    assert error(await client.post(f"/tasks/{UNKNOWN}/stories", json={"data": {"text": "hi"}}), 404) == (
        f"task: Unknown object: {UNKNOWN}"
    )


async def test_listing_tasks_without_a_filter_is_refused(client: httpx.AsyncClient) -> None:
    message = "Must specify exactly one of project, tag, section, user task list, or assignee + workspace"
    assert error(await client.get("/tasks"), 400) == message
    assert error(await client.get("/tasks", params={"assignee": "me"}), 400) == message
    assert error(await client.get("/tasks", params={"project": VENUE, "assignee": "me", "workspace": WS}), 400) == (
        message
    )
    assert error(await client.get("/tasks", params={"project": UNKNOWN}), 400) == f"project: Unknown object: {UNKNOWN}"


async def test_search_rejects_what_it_does_not_know_and_its_limit(client: httpx.AsyncClient) -> None:
    search = f"/workspaces/{WS}/tasks/search"
    assert error(await client.post(search, json={"data": {}}), 404) == "No matching route for request"
    assert error(await client.get(search, params={"jql": "x"}), 400) == "jql: Unrecognized parameter"
    assert error(await client.get(search, params={"limit": "101"}), 400) == "limit: Must be between 1 and 100"
    assert error(await client.get(search, params={"assignee.any": "jsmith"}), 400) == (
        "assignee.any: Not a Recognized ID"
    )
    assert error(await client.get(search, params={"completed": "maybe"}), 400) == "completed: Not a boolean"
    assert unserved(await client.get(search, params={"due_on.before": "2026-01-01"})) == "due_on.before"


async def test_typeahead_needs_a_resource_type(client: httpx.AsyncClient) -> None:
    at = f"/workspaces/{WS}/typeahead"
    assert error(await client.get(at, params={"query": "x"}), 400) == "resource_type: Missing input"
    assert error(await client.get(at, params={"resource_type": "task", "count": "0"}), 400) == (
        "count: Must be between 1 and 100"
    )


@pytest.mark.parametrize(
    "params, message",
    [
        ({"limit": "0"}, "limit: Must be between 1 and 100"),
        ({"limit": "abc"}, "limit: Must be between 1 and 100"),
        ({"offset": "abc"}, "offset: Cannot be used without limit"),
        ({"limit": "1", "offset": "not-a-token"}, "offset: Invalid offset token"),
    ],
)
async def test_a_bad_page_is_refused(client: httpx.AsyncClient, params: dict[str, str], message: str) -> None:
    assert error(await client.get(f"/projects/{VENUE}/tasks", params=params), 400) == message


async def test_an_unknown_route_and_a_method_a_path_does_not_take_are_refused_no_matching_route(
    client: httpx.AsyncClient,
) -> None:
    """OBSERVED: Asana answers both 404 "No matching route for request", never 405
    (`tests/data/asana_rest_1_0/real-service-without-a-token-2026-10-08.txt`)."""
    assert error(await client.get("/nothing/here"), 404) == "No matching route for request"
    assert error(await client.patch(f"/tasks/{UNKNOWN}", json={}), 404) == "No matching route for request"
    assert error(await client.delete("/workspaces"), 404) == "No matching route for request"
