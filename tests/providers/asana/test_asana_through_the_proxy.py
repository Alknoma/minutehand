"""A tracker client's whole Asana conversation, through the real proxy, over TLS, at app.asana.com/api/1.0.

The first test is the discovery a client makes when it holds nothing but a token: who am I, which
workspace, which team, which project, which sections and custom fields, then a task created with a
priority and a status, moved three ways and read back. No gid is written into it: each one is read off
an earlier answer, by name, the way the client's own code does. The query strings and bodies are the ones
that client sends (every collection with `limit=100`, `opt_fields` as it names them, `addMembers` as one
comma-separated string).

The second drives the same surface with the official `asana` SDK.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import asana  # pyright: ignore[reportMissingTypeStubs]
import httpx
import pytest
from asana.rest import ApiException  # pyright: ignore[reportMissingTypeStubs]

from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.provider import build
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, Operation, TicketSnapshot
from tests.providers.asana.asana_workspace import START
from tests.providers.asana.rich_workspace import AGENT_TOKEN, SCENARIO

T = TypeVar("T")
API = "https://app.asana.com/api/1.0"
PAGE = {"limit": "100"}

TICKET_OPT_FIELDS = ",".join(
    [
        "name",
        "notes",
        "completed",
        "completed_at",
        "created_at",
        "modified_at",
        "due_at",
        "due_on",
        "assignee.name",
        "assignee.email",
        "assignee.gid",
        "created_by.name",
        "created_by.email",
        "created_by.gid",
        "memberships.project.name",
        "memberships.project.gid",
        "memberships.section.name",
        "memberships.section.gid",
        "parent.gid",
        "parent.name",
        "tags.name",
        "tags.gid",
        "custom_fields.name",
        "custom_fields.enum_value.name",
        "custom_fields.number_value",
        "permalink_url",
    ]
)
SETTING_FIELDS = (
    "custom_field.name,custom_field.resource_subtype,custom_field.type,"
    "custom_field.enum_options.name,custom_field.enum_options.enabled"
)


@dataclass
class Through:
    http: httpx.AsyncClient
    sdk: Any
    store: SqliteStore


@pytest.fixture
async def through(tmp_path: Path) -> AsyncIterator[Through]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO, store)
    registry = Registry()
    registry.register(MANIFEST, lambda: provider)
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        trust = ssl.create_default_context(cafile=str(proxy.ca_cert))
        configuration: Any = asana.Configuration()
        configuration.access_token = AGENT_TOKEN
        configuration.proxy = proxy.url
        configuration.ssl_ca_cert = str(proxy.ca_cert)
        async with httpx.AsyncClient(
            base_url=API,
            proxy=proxy.url,
            verify=trust,
            trust_env=False,
            headers={"Authorization": f"Bearer {AGENT_TOKEN}", "Accept": "application/json"},
        ) as http:
            yield Through(http=http, sdk=asana.ApiClient(configuration), store=store)


async def off_loop(call: Callable[[], T]) -> T:
    return await asyncio.to_thread(call)


async def read(
    http: httpx.AsyncClient, method: str, path: str, params: dict[str, str] | None = None, body: object = None
) -> Any:
    response = await http.request(method, path, params=params, json=body)
    assert response.is_success, f"{method} {path}: {response.status_code} {response.text}"
    return response.json()["data"] if response.content else None


async def every(http: httpx.AsyncClient, path: str, params: dict[str, str]) -> list[Any]:
    """The client's page walk: `limit=100`, then `next_page.offset` until there is none."""
    found: list[Any] = []
    asked = {**params, **PAGE}
    while True:
        page = (await http.get(path, params=asked)).json()
        found += page["data"]
        if page["next_page"] is None:
            return found
        asked = {**asked, "offset": page["next_page"]["offset"]}


def named(records: list[Any], name: str) -> Any:
    return next(r for r in records if r["name"] == name)


async def test_a_client_holding_only_a_token_discovers_its_workspace_and_runs_a_task_to_done(through: Through) -> None:
    http = through.http
    me = await read(http, "GET", "/users/me", {"opt_fields": "name,email"})
    workspace = (await every(http, "/workspaces", {"opt_fields": "name"}))[0]
    detail = await read(http, "GET", f"/workspaces/{workspace['gid']}", {"opt_fields": "name,is_organization"})
    assert detail["is_organization"] is True
    teams = await every(http, "/users/me/teams", {"organization": workspace["gid"], "opt_fields": "name"})
    people = await every(http, "/users", {"workspace": workspace["gid"], "opt_fields": "name,email"})
    projects = await every(
        http,
        f"/workspaces/{workspace['gid']}/projects",
        {"archived": "false", "opt_fields": "name,archived,team.name,team.gid"},
    )
    project = named(projects, "Backend Services")
    assert project["team"]["gid"] in [t["gid"] for t in teams]

    settings = await every(http, f"/projects/{project['gid']}/custom_field_settings", {"opt_fields": SETTING_FIELDS})
    fields = {s["custom_field"]["name"].lower(): s["custom_field"] for s in settings}
    status, priority = fields["status"], fields["priority"]
    sections = await every(http, f"/projects/{project['gid']}/sections", {"opt_fields": "name"})
    bob = next(p for p in people if p["email"] == "bob.taylor@company.com")

    made = await read(
        http,
        "POST",
        "/tasks",
        body={
            "data": {
                "name": "Rotate the signing keys",
                "notes": "Before Friday.",
                "projects": [project["gid"]],
                "assignee": bob["gid"],
                "due_at": "2026-08-28T17:00:00+00:00",
                "custom_fields": {
                    priority["gid"]: named(priority["enum_options"], "High")["gid"],
                    status["gid"]: named(status["enum_options"], "Open")["gid"],
                },
            }
        },
    )
    gid = made["gid"]
    await read(
        http,
        "PUT",
        f"/tasks/{gid}",
        body={"data": {"custom_fields": {status["gid"]: named(status["enum_options"], "In Progress")["gid"]}}},
    )
    await read(http, "POST", f"/sections/{named(sections, 'In Review')['gid']}/addTask", body={"data": {"task": gid}})
    await read(http, "PUT", f"/tasks/{gid}", body={"data": {"completed": True}})
    await read(http, "POST", f"/tasks/{gid}/stories", body={"data": {"text": "Done and verified."}})

    task = await read(http, "GET", f"/tasks/{gid}", {"opt_fields": TICKET_OPT_FIELDS})
    values = {f["name"]: f for f in task["custom_fields"]}
    assert (task["completed"], task["assignee"]["email"], task["created_by"]["gid"]) == (
        True,
        "bob.taylor@company.com",
        me["gid"],
    )
    assert values["Priority"]["enum_value"]["name"] == "High"
    assert values["Status"]["enum_value"]["name"] == "In Progress", "completing moves neither the field nor the section"
    assert task["memberships"][0]["section"]["name"] == "In Review"
    assert (task["due_on"], task["due_at"]) == ("2026-08-28", "2026-08-28T17:00:00+00:00")
    assert task["permalink_url"] == f"https://app.asana.com/0/{project['gid']}/{gid}"

    changes = [e for e in through.store.events() if e.entity.external_id == gid and isinstance(e.after, TicketSnapshot)]
    assert [(e.actor, e.operation) for e in changes] == [(Actor.AGENT, Operation.CREATE)] + [
        (Actor.AGENT, Operation.UPDATE)
    ] * 3
    last = changes[-1].after
    assert isinstance(last, TicketSnapshot) and last.state is TicketState.DONE
    assert changes[0].exchange is not None and changes[0].exchange.path == "/api/1.0/tasks"


async def test_a_client_creates_a_project_in_its_team_settles_fields_and_adds_people(through: Through) -> None:
    http = through.http
    workspace = (await every(http, "/workspaces", {"opt_fields": "name"}))[0]
    live = await every(
        http,
        f"/workspaces/{workspace['gid']}/projects",
        {"archived": "false", "opt_fields": "name,archived,team.name,team.gid"},
    )
    team = live[0]["team"]["gid"]
    project = await read(
        http,
        "POST",
        "/projects",
        {"opt_fields": "name,notes,archived,team.name,team.gid"},
        {"data": {"workspace": workspace["gid"], "name": "Partner Launch", "notes": "Why", "team": team}},
    )
    assert project["team"]["gid"] == team
    backend = named(live, "Backend Services")
    wanted = await every(http, f"/projects/{backend['gid']}/custom_field_settings", {"opt_fields": SETTING_FIELDS})
    for setting in wanted[:2]:
        await read(
            http,
            "POST",
            f"/projects/{project['gid']}/addCustomFieldSetting",
            body={"data": {"custom_field": setting["custom_field"]["gid"]}},
        )
    settled = await every(http, f"/projects/{project['gid']}/custom_field_settings", {"opt_fields": SETTING_FIELDS})
    assert {s["custom_field"]["name"] for s in settled} == {w["custom_field"]["name"] for w in wanted[:2]}
    people = await every(http, "/users", {"workspace": workspace["gid"], "opt_fields": "name,email"})
    alice = next(p for p in people if p["email"] == "alice.chen@company.com")
    await read(
        http, "POST", f"/projects/{project['gid']}/addMembers", body={"data": {"members": ",".join([alice["gid"]])}}
    )
    members = await every(
        http, f"/projects/{project['gid']}/project_memberships", {"opt_fields": "user.name,user.email"}
    )
    assert sorted(m["user"]["email"] for m in members) == ["agent@workspace.example", "alice.chen@company.com"]
    assert [s["name"] for s in await every(http, f"/projects/{project['gid']}/sections", {"opt_fields": "name"})] == [
        "Untitled section"
    ]
    got = await read(
        http, "GET", f"/projects/{project['gid']}", {"opt_fields": "name,notes,archived,team.name,team.gid"}
    )
    assert (got["name"], got["notes"]) == ("Partner Launch", "Why")


async def test_a_client_labels_parents_searches_and_deletes(through: Through) -> None:
    http = through.http
    workspace = (await every(http, "/workspaces", {"opt_fields": "name"}))[0]
    project = named(
        await every(http, f"/workspaces/{workspace['gid']}/projects", {"archived": "false", "opt_fields": "name"}),
        "Backend Services",
    )
    tasks = await every(
        http, f"/projects/{project['gid']}/tasks", {"opt_fields": TICKET_OPT_FIELDS, "completed_since": "now"}
    )
    incident = named(tasks, "API timeout in production")
    assert [t["name"] for t in incident["tags"]] == ["performance", "production"]
    tags = await every(http, "/tags", {"workspace": workspace["gid"], "opt_fields": "name"})
    assert "urgent" not in [t["name"] for t in tags]
    urgent = await read(
        http, "POST", "/tags", {"opt_fields": "name"}, {"data": {"name": "urgent", "workspace": workspace["gid"]}}
    )
    child = await read(
        http,
        "POST",
        "/tasks",
        body={"data": {"name": "Page the on-call", "notes": "", "projects": [project["gid"]], "due_on": "2026-08-25"}},
    )
    await read(http, "POST", f"/tasks/{child['gid']}/setParent", body={"data": {"parent": incident["gid"]}})
    await read(http, "POST", f"/tasks/{child['gid']}/addTag", body={"data": {"tag": urgent["gid"]}})
    found = await read(
        http,
        "GET",
        f"/workspaces/{workspace['gid']}/tasks/search",
        {
            "limit": "25",
            "text": "on-call",
            "completed": "false",
            "projects.any": project["gid"],
            "opt_fields": TICKET_OPT_FIELDS,
        },
    )
    assert [(t["parent"]["name"], [g["name"] for g in t["tags"]]) for t in found] == [
        ("API timeout in production", ["urgent"])
    ]
    hits = await read(
        http,
        "GET",
        f"/workspaces/{workspace['gid']}/typeahead",
        {"resource_type": "task", "query": "page the", "count": "25"},
    )
    assert [h["gid"] for h in hits] == [child["gid"]]
    await read(http, "POST", f"/tasks/{child['gid']}/removeTag", body={"data": {"tag": urgent["gid"]}})
    assert (await read(http, "GET", f"/tasks/{child['gid']}", {"opt_fields": "tags.name"}))["tags"] == []
    mine = await read(
        http,
        "GET",
        f"/workspaces/{workspace['gid']}/tasks/search",
        {"limit": "100", "assignee.any": "bob.taylor@company.com", "opt_fields": "name"},
    )
    assert [t["name"] for t in mine] == ["API timeout in production"]
    await read(http, "DELETE", f"/tasks/{child['gid']}")
    gone = await http.get(f"/tasks/{child['gid']}", params={"opt_fields": TICKET_OPT_FIELDS})
    assert gone.status_code == 404


async def test_the_official_sdk_walks_the_same_surface(through: Through) -> None:
    sdk = through.sdk
    users: Any = asana.UsersApi(sdk)
    workspaces: Any = asana.WorkspacesApi(sdk)
    teams: Any = asana.TeamsApi(sdk)
    projects: Any = asana.ProjectsApi(sdk)
    memberships: Any = asana.ProjectMembershipsApi(sdk)
    sections: Any = asana.SectionsApi(sdk)
    settings: Any = asana.CustomFieldSettingsApi(sdk)
    tags: Any = asana.TagsApi(sdk)
    tasks: Any = asana.TasksApi(sdk)
    stories: Any = asana.StoriesApi(sdk)
    typeahead: Any = asana.TypeaheadApi(sdk)

    me = await off_loop(lambda: users.get_user("me", {"opt_fields": "name,email"}))
    ws = (await off_loop(lambda: list(workspaces.get_workspaces({"opt_fields": "name,is_organization"}))))[0]
    team = (await off_loop(lambda: list(teams.get_teams_for_user("me", ws["gid"], {"opt_fields": "name"}))))[0]
    assert (me["name"], ws["is_organization"], team["name"]) == ("Agent", True, "Engineering")
    backend = next(
        p
        for p in await off_loop(
            lambda: list(
                projects.get_projects_for_workspace(ws["gid"], {"archived": False, "opt_fields": "name,team.name"})
            )
        )
        if p["name"] == "Backend Services"
    )
    bound = await off_loop(
        lambda: list(settings.get_custom_field_settings_for_project(backend["gid"], {"opt_fields": SETTING_FIELDS}))
    )
    priority = next(s["custom_field"] for s in bound if s["custom_field"]["name"] == "Priority")
    columns = await off_loop(lambda: list(sections.get_sections_for_project(backend["gid"], {"opt_fields": "name"})))
    made = await off_loop(
        lambda: tasks.create_task(
            {
                "data": {
                    "name": "Audit tokens",
                    "projects": [backend["gid"]],
                    "custom_fields": {priority["gid"]: priority["enum_options"][1]["gid"]},
                }
            },
            {"opt_fields": "name,custom_fields.display_value"},
        )
    )
    assert made["custom_fields"][1]["display_value"] == "High"
    await off_loop(lambda: sections.add_task_for_section(columns[-1]["gid"], {"body": {"data": {"task": made["gid"]}}}))
    label = await off_loop(
        lambda: tags.create_tag({"data": {"name": "security", "workspace": ws["gid"]}}, {"opt_fields": "name"})
    )
    await off_loop(lambda: tasks.add_tag_for_task({"data": {"tag": label["gid"]}}, made["gid"]))
    await off_loop(lambda: tasks.set_parent_for_task({"data": {"parent": None}}, made["gid"], {}))
    child = await off_loop(lambda: tasks.create_subtask_for_task({"data": {"name": "List them"}}, made["gid"], {}))
    await off_loop(lambda: stories.create_story_for_task({"data": {"text": "Started"}}, made["gid"], {}))
    read = await off_loop(lambda: tasks.get_task(made["gid"], {"opt_fields": "tags.name,memberships.section.name"}))
    under = await off_loop(lambda: list(tasks.get_subtasks_for_task(made["gid"], {"opt_fields": "name"})))
    assert ([t["name"] for t in read["tags"]], [s["name"] for s in under]) == (["security"], ["List them"])
    assert read["memberships"][0]["section"]["name"] == "Cancelled"
    await off_loop(lambda: tasks.remove_tag_for_task({"data": {"tag": label["gid"]}}, made["gid"]))
    project = await off_loop(
        lambda: projects.create_project(
            {"data": {"name": "SDK project", "workspace": ws["gid"], "team": team["gid"]}}, {"opt_fields": "name"}
        )
    )
    await off_loop(
        lambda: projects.add_custom_field_setting_for_project(
            {"data": {"custom_field": priority["gid"]}}, project["gid"], {}
        )
    )
    await off_loop(
        lambda: projects.add_members_for_project({"data": {"members": "alice.chen@company.com"}}, project["gid"], {})
    )
    joined = await off_loop(
        lambda: list(memberships.get_project_memberships_for_project(project["gid"], {"opt_fields": "user.email"}))
    )
    assert sorted(m["user"]["email"] for m in joined) == ["agent@workspace.example", "alice.chen@company.com"]
    found = await off_loop(
        lambda: list(tasks.search_tasks_for_workspace(ws["gid"], {"text": "audit", "opt_fields": "name"}))
    )
    hits = await off_loop(lambda: list(typeahead.typeahead_for_workspace(ws["gid"], "task", {"query": "list them"})))
    assert ([t["name"] for t in found], [h["gid"] for h in hits]) == (["Audit tokens"], [child["gid"]])
    await off_loop(lambda: tasks.delete_task(made["gid"]))
    with pytest.raises(ApiException) as refused:
        await off_loop(lambda: tasks.get_task(child["gid"], {}))
    assert refused.value.status == 404, "a subtask is deleted with its parent"
