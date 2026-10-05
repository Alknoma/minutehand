"""Teams, project members, project creation, custom fields, tags, sections, subtasks and tokens, in Asana's
wire shapes, over the ASGI app and a workspace seeded with an Asana seed."""

from __future__ import annotations

import httpx

from minutehand.adapters.providers.asana import state
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, Operation
from tests.providers.asana.asana_workspace import Workspace, error
from tests.providers.asana.rich_workspace import (
    ALICE_TOKEN,
    BACKEND,
    DESIGN,
    ENGINEERING,
    INCIDENT,
    OLD,
    POINTS,
    PRIORITY,
    STATUS_FIELD,
    SUBTASK,
    WS,
    agent,
    client_as,
    got,
    option,
    rich,
    section,
)

__all__ = ["agent", "rich"]

TASK_FIELDS = ",".join(
    [
        "name",
        "completed",
        "assignee.email",
        "memberships.section.name",
        "custom_fields.name",
        "custom_fields.enum_value.name",
        "custom_fields.number_value",
        "custom_fields.display_value",
        "parent.gid",
        "subtasks.name",
        "tags.name",
    ]
)


async def test_me_is_the_user_the_token_maps_to(rich: Workspace, agent: httpx.AsyncClient) -> None:
    async with client_as(rich, ALICE_TOKEN) as alice:
        me = got(await alice.get("/users/me", params={"opt_fields": "name,email"}))
    assert me == {"gid": state.user_gid("alice"), "name": "Alice Chen", "email": "alice.chen@company.com"}
    assert got(await agent.get("/users/me"))["gid"] == state.AGENT_GID


async def test_my_teams_are_the_teams_i_am_in_of_that_organization(rich: Workspace, agent: httpx.AsyncClient) -> None:
    mine = got(await agent.get("/users/me/teams", params={"organization": WS, "opt_fields": "name"}))
    assert mine == [{"gid": ENGINEERING, "name": "Engineering"}]
    async with client_as(rich, ALICE_TOKEN) as alice:
        hers = got(await alice.get("/users/me/teams", params={"organization": WS}))
    assert sorted(t["gid"] for t in hers) == sorted([DESIGN, ENGINEERING])
    assert all(set(t) == {"gid", "resource_type", "name"} for t in hers)
    every = got(await agent.get(f"/workspaces/{WS}/teams"))
    assert sorted(t["name"] for t in every) == ["Design", "Engineering"]


async def test_a_workspace_says_it_is_an_organization_only_when_asked(agent: httpx.AsyncClient) -> None:
    assert got(await agent.get(f"/workspaces/{WS}", params={"opt_fields": "name,is_organization"})) == {
        "gid": WS,
        "name": "Test Workspace",
        "is_organization": True,
    }
    assert got(await agent.get("/workspaces", params={"opt_fields": "name"})) == [{"gid": WS, "name": "Test Workspace"}]


async def test_projects_answer_their_team_and_an_archived_filter(agent: httpx.AsyncClient) -> None:
    listed = got(
        await agent.get(
            f"/workspaces/{WS}/projects", params={"archived": "false", "opt_fields": "name,archived,team.name,team.gid"}
        )
    )
    assert listed == [
        {
            "gid": BACKEND,
            "name": "Backend Services",
            "archived": False,
            "team": {"gid": ENGINEERING, "name": "Engineering"},
        }
    ], "the private project the agent is not in is not listed"
    assert [p["gid"] for p in got(await agent.get(f"/teams/{ENGINEERING}/projects"))] == [BACKEND]


async def test_project_memberships_list_the_members_with_their_emails(agent: httpx.AsyncClient) -> None:
    members = got(
        await agent.get(f"/projects/{BACKEND}/project_memberships", params={"opt_fields": "user.name,user.email"})
    )
    assert sorted(str(m["user"]["email"]) for m in members) == [
        "agent@workspace.example",
        "alice.chen@company.com",
        "bob.taylor@company.com",
        "sarah.williams@company.com",
    ]
    assert all(m["gid"] == state.membership_gid(BACKEND, str(m["user"]["gid"])) for m in members)


async def test_add_members_takes_one_comma_separated_string_and_answers_the_project(
    rich: Workspace, agent: httpx.AsyncClient
) -> None:
    made = got(
        await agent.post("/projects", json={"data": {"name": "Launch", "workspace": WS, "team": ENGINEERING}}), 201
    )
    alice, bob = state.user_gid("alice"), state.user_gid("bob")
    added = await agent.post(f"/projects/{made['gid']}/addMembers", json={"data": {"members": f"{alice},{bob}"}})
    assert got(added)["gid"] == made["gid"]
    members = got(await agent.get(f"/projects/{made['gid']}/project_memberships", params={"opt_fields": "user.gid"}))
    assert sorted(str(m["user"]["gid"]) for m in members) == sorted([state.AGENT_GID, alice, bob])
    removed = await agent.post(
        f"/projects/{made['gid']}/removeMembers", json={"data": {"members": ["bob.taylor@company.com"]}}
    )
    assert got(removed)["gid"] == made["gid"]
    after = got(await agent.get(f"/projects/{made['gid']}/project_memberships", params={"opt_fields": "user.gid"}))
    assert sorted(str(m["user"]["gid"]) for m in after) == sorted([state.AGENT_GID, alice])
    changes = [
        e for e in rich.store.events() if e.entity.external_id == made["gid"] and e.operation is not Operation.SEARCH
    ]
    assert [(e.actor, e.operation) for e in changes] == [
        (Actor.AGENT, Operation.CREATE),
        (Actor.AGENT, Operation.UPDATE),
        (Actor.AGENT, Operation.UPDATE),
    ]


async def test_a_created_project_has_one_untitled_section_no_fields_and_its_creator_as_member(
    agent: httpx.AsyncClient,
) -> None:
    made = got(
        await agent.post(
            "/projects",
            params={"opt_fields": "name,notes,archived,team.name,team.gid"},
            json={"data": {"name": "Q4 Partners", "notes": "Why it exists", "workspace": WS, "team": ENGINEERING}},
        ),
        201,
    )
    assert made == {
        "gid": made["gid"],
        "name": "Q4 Partners",
        "notes": "Why it exists",
        "archived": False,
        "team": {"gid": ENGINEERING, "name": "Engineering"},
    }
    sections = got(await agent.get(f"/projects/{made['gid']}/sections", params={"opt_fields": "name"}))
    assert [s["name"] for s in sections] == ["Untitled section"]
    assert got(await agent.get(f"/projects/{made['gid']}/custom_field_settings")) == []
    members = got(await agent.get(f"/projects/{made['gid']}/project_memberships", params={"opt_fields": "user.gid"}))
    assert [m["user"] for m in members] == [{"gid": state.AGENT_GID}]


async def test_a_project_is_created_under_a_team_or_a_workspace_path(agent: httpx.AsyncClient) -> None:
    by_team = got(await agent.post(f"/teams/{ENGINEERING}/projects", json={"data": {"name": "By team"}}), 201)
    by_workspace = got(
        await agent.post(f"/workspaces/{WS}/projects", json={"data": {"name": "By workspace", "team": ENGINEERING}}),
        201,
    )
    assert {by_team["name"], by_workspace["name"]} == {"By team", "By workspace"}
    section_made = got(await agent.post(f"/projects/{by_team['gid']}/sections", json={"data": {"name": "Doing"}}), 201)
    assert section_made["name"] == "Doing"
    assert got(await agent.get(f"/sections/{section_made['gid']}"))["project"] == {
        "gid": by_team["gid"],
        "resource_type": "project",
        "name": "By team",
    }


async def test_settling_a_custom_field_on_a_project_lists_it_with_the_field_expanded(agent: httpx.AsyncClient) -> None:
    made = got(
        await agent.post("/projects", json={"data": {"name": "Settled", "workspace": WS, "team": ENGINEERING}}), 201
    )
    setting = await agent.post(
        f"/projects/{made['gid']}/addCustomFieldSetting", json={"data": {"custom_field": PRIORITY}}
    )
    assert got(setting)["resource_type"] == "custom_field_setting"
    settings = got(
        await agent.get(
            f"/projects/{made['gid']}/custom_field_settings",
            params={
                "opt_fields": "custom_field.name,custom_field.resource_subtype,custom_field.type,"
                "custom_field.enum_options.name,custom_field.enum_options.enabled"
            },
        )
    )
    assert settings == [
        {
            "gid": state.setting_gid(made["gid"], PRIORITY),
            "custom_field": {
                "gid": PRIORITY,
                "name": "Priority",
                "resource_subtype": "enum",
                "type": "enum",
                "enum_options": [
                    {"gid": option("Priority", n), "name": n, "enabled": True}
                    for n in ["Critical", "High", "Medium", "Low"]
                ],
            },
        }
    ]
    assert [s["gid"] for s in got(await agent.get(f"/projects/{made['gid']}/custom_field_settings"))] == [
        settings[0]["gid"]
    ], "a setting is compact without opt_fields: gid and resource_type"
    removed = await agent.post(
        f"/projects/{made['gid']}/removeCustomFieldSetting", json={"data": {"custom_field": PRIORITY}}
    )
    assert got(removed) == {}
    assert got(await agent.get(f"/projects/{made['gid']}/custom_field_settings")) == []


async def test_custom_fields_are_set_on_create_and_round_trip_with_their_values(agent: httpx.AsyncClient) -> None:
    made = got(
        await agent.post(
            "/tasks",
            params={"opt_fields": TASK_FIELDS},
            json={
                "data": {
                    "name": "Rotate the keys",
                    "projects": [BACKEND],
                    "assignee": "alice.chen@company.com",
                    "custom_fields": {
                        PRIORITY: option("Priority", "High"),
                        STATUS_FIELD: option("Status", "In Review"),
                        POINTS: 3,
                        state.field_gid("Platforms"): [option("Platforms", "Web")],
                        state.field_gid("Ticket"): "SEC-7",
                        state.field_gid("Launch"): {"date": "2026-09-30"},
                        state.field_gid("Reviewers"): ["bob.taylor@company.com"],
                    },
                }
            },
        ),
        201,
    )
    by_name = {str(f["name"]): f for f in made["custom_fields"]}
    assert by_name["Priority"] == {
        "gid": PRIORITY,
        "name": "Priority",
        "enum_value": {"gid": option("Priority", "High"), "name": "High"},
        "display_value": "High",
    }
    assert by_name["Story Points"] == {
        "gid": POINTS,
        "name": "Story Points",
        "number_value": 3.0,
        "display_value": "3.0",
    }
    assert [f["display_value"] for f in made["custom_fields"]] == [
        "In Review",
        "High",
        "3.0",
        "Web",
        "SEC-7",
        "2026-09-30",
        "Bob Taylor",
    ]


async def test_a_custom_field_read_in_full_carries_only_its_own_kinds_members(agent: httpx.AsyncClient) -> None:
    full = got(await agent.get(f"/tasks/{INCIDENT}"))
    fields = {str(f["name"]): f for f in full["custom_fields"]}
    assert set(fields["Priority"]) >= {"enum_value", "enum_options", "display_value", "resource_subtype"}
    assert "number_value" not in fields["Priority"] and "enum_value" not in fields["Story Points"]
    assert fields["Story Points"]["number_value"] == 5.0 and fields["Story Points"]["precision"] == 1
    assert fields["Launch"]["date_value"] == {"date": "2026-09-03", "date_time": None}
    assert [p["name"] for p in fields["Reviewers"]["people_value"]] == ["Alice Chen", "Bob Taylor"]
    assert [o["name"] for o in fields["Platforms"]["multi_enum_values"]] == ["iOS", "Web"]
    assert fields["Ticket"]["text_value"] == "INC-42"


async def test_an_update_writes_one_field_over_the_others(agent: httpx.AsyncClient) -> None:
    changed = got(
        await agent.put(
            f"/tasks/{INCIDENT}",
            params={"opt_fields": "custom_fields.display_value"},
            json={"data": {"custom_fields": {PRIORITY: option("Priority", "Low"), POINTS: None}}},
        )
    )
    assert [f["display_value"] for f in changed["custom_fields"]] == [
        "In Progress",
        "Low",
        None,
        "iOS, Web",
        "INC-42",
        "2026-09-03",
        "Alice Chen, Bob Taylor",
    ]


async def test_a_bare_field_name_answers_a_reference_but_bare_custom_fields_answer_their_values(
    agent: httpx.AsyncClient,
) -> None:
    referenced = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "assignee,custom_fields"}))
    assert set(referenced["assignee"]) == {"gid", "resource_type"}
    status = referenced["custom_fields"][0]
    assert (status["gid"], status["name"], status["resource_type"]) == (STATUS_FIELD, "Status", "custom_field")
    assert status["enum_value"]["name"] == "In Progress"
    assert status["enum_value"]["gid"] == option("Status", "In Progress")
    named = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "custom_fields.enum_value.name"}))
    assert named["custom_fields"][0] == {
        "gid": STATUS_FIELD,
        "enum_value": {"gid": option("Status", "In Progress"), "name": "In Progress"},
    }


async def test_tags_are_listed_created_added_and_removed_by_gid(agent: httpx.AsyncClient) -> None:
    listed = got(await agent.get("/tags", params={"workspace": WS, "opt_fields": "name"}))
    assert sorted(str(t["name"]) for t in listed) == ["performance", "production"]
    made = got(
        await agent.post("/tags", params={"opt_fields": "name"}, json={"data": {"name": "urgent", "workspace": WS}}),
        201,
    )
    assert made == {"gid": made["gid"], "name": "urgent"}
    assert got(await agent.post(f"/tasks/{OLD}/addTag", json={"data": {"tag": made["gid"]}})) == {}
    read = got(await agent.get(f"/tasks/{OLD}", params={"opt_fields": "tags.name"}))
    assert read["tags"] == [{"gid": made["gid"], "name": "urgent"}]
    assert [t["gid"] for t in got(await agent.get(f"/tags/{made['gid']}/tasks"))] == [OLD]
    assert got(await agent.post(f"/tasks/{OLD}/removeTag", json={"data": {"tag": made["gid"]}})) == {}
    assert got(await agent.get(f"/tasks/{OLD}", params={"opt_fields": "tags"}))["tags"] == []
    assert [t["gid"] for t in got(await agent.get(f"/tasks/{INCIDENT}/tags"))] == [
        state.tag_gid("performance"),
        state.tag_gid("production"),
    ]


async def test_add_task_moves_it_to_the_section_and_status_is_read_from_the_field(
    rich: Workspace, agent: httpx.AsyncClient
) -> None:
    assert got(await agent.post(f"/sections/{section('Done')}/addTask", json={"data": {"task": INCIDENT}})) == {}
    read = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "memberships.section.name,completed"}))
    assert read["memberships"] == [{"section": {"gid": section("Done"), "name": "Done"}}]
    assert read["completed"] is False, "a section move ticks nothing"
    task = rich.asana.task(INCIDENT)
    assert task is not None
    assert rich.asana.snapshot(task).state is TicketState.OPEN, (
        "the scenario reads state from the Status field, which still says In Progress"
    )
    listed = got(await agent.get(f"/sections/{section('Done')}/tasks"))
    assert [t["gid"] for t in listed] == [INCIDENT], "the done ticket says Done in its Status field, not its section"


async def test_a_task_in_a_section_of_another_project_joins_that_project(agent: httpx.AsyncClient) -> None:
    other = got(
        await agent.post("/projects", json={"data": {"name": "Ops", "workspace": WS, "team": ENGINEERING}}), 201
    )
    untitled = got(await agent.get(f"/projects/{other['gid']}/sections"))[0]["gid"]
    await agent.post(f"/sections/{untitled}/addTask", json={"data": {"task": INCIDENT}})
    read = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "projects.name"}))
    assert [p["name"] for p in read["projects"]] == ["Backend Services", "Ops"]


async def test_set_parent_makes_a_subtask_and_unsets_it(agent: httpx.AsyncClient) -> None:
    made = got(await agent.post("/tasks", json={"data": {"name": "Write the runbook", "projects": [BACKEND]}}), 201)
    parented = await agent.post(
        f"/tasks/{made['gid']}/setParent",
        params={"opt_fields": "parent.gid,parent.name"},
        json={"data": {"parent": INCIDENT}},
    )
    assert got(parented)["parent"] == {"gid": INCIDENT, "name": "API timeout in production"}
    subtasks = got(await agent.get(f"/tasks/{INCIDENT}/subtasks", params={"opt_fields": "name"}))
    assert [s["name"] for s in subtasks] == ["Raise the pool size", "Write the runbook"]
    read = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "subtasks.name,num_subtasks"}))
    assert read["num_subtasks"] == 2
    unset = await agent.post(f"/tasks/{made['gid']}/setParent", json={"data": {"parent": None}})
    assert got(unset)["parent"] is None


async def test_a_subtask_is_created_under_its_parent_and_deleted_with_it(
    rich: Workspace, agent: httpx.AsyncClient
) -> None:
    made = got(await agent.post(f"/tasks/{SUBTASK}/subtasks", json={"data": {"name": "Measure first"}}), 201)
    read = got(await agent.get(f"/tasks/{made['gid']}", params={"opt_fields": "parent.gid,projects,workspace"}))
    assert read == {
        "gid": made["gid"],
        "parent": {"gid": SUBTASK},
        "projects": [],
        "workspace": {"gid": WS, "resource_type": "workspace"},
    }
    await agent.delete(f"/tasks/{INCIDENT}")
    for gone in (INCIDENT, SUBTASK, made["gid"]):
        assert error(await agent.get(f"/tasks/{gone}"), 404) == f"task: Unknown object: {gone}"


async def test_search_filters_by_section_tag_and_subtask(agent: httpx.AsyncClient) -> None:
    by_section = got(await agent.get(f"/workspaces/{WS}/tasks/search", params={"sections.any": section("In Progress")}))
    assert [t["gid"] for t in by_section] == [INCIDENT]
    by_tag = got(await agent.get(f"/workspaces/{WS}/tasks/search", params={"tags.any": state.tag_gid("production")}))
    assert [t["gid"] for t in by_tag] == [INCIDENT]
    subtasks = got(await agent.get(f"/workspaces/{WS}/tasks/search", params={"is_subtask": "true"}))
    assert [t["gid"] for t in subtasks] == [SUBTASK]
    not_bob = got(
        await agent.get(
            f"/workspaces/{WS}/tasks/search", params={"assignee.not": state.user_gid("bob"), "sort_ascending": "true"}
        )
    )
    assert INCIDENT not in [t["gid"] for t in not_bob]


async def test_typeahead_finds_tags(agent: httpx.AsyncClient) -> None:
    found = got(await agent.get(f"/workspaces/{WS}/typeahead", params={"resource_type": "tag", "query": "perf"}))
    assert [t["name"] for t in found] == ["performance"]


async def test_workspace_custom_fields_list_the_definitions(agent: httpx.AsyncClient) -> None:
    fields = got(await agent.get(f"/workspaces/{WS}/custom_fields", params={"opt_fields": "name,resource_subtype"}))
    assert {str(f["name"]): f["resource_subtype"] for f in fields}["Reviewers"] == "people"
    one = got(await agent.get(f"/custom_fields/{POINTS}"))
    assert (one["name"], one["precision"], "enum_options" in one) == ("Story Points", 1, False)


async def test_html_notes_are_read_as_the_text_they_show(agent: httpx.AsyncClient) -> None:
    made = got(
        await agent.post(
            "/tasks",
            json={
                "data": {"name": "x", "projects": [BACKEND], "html_notes": "<body>Call <b>Bob</b> &amp; Alice</body>"}
            },
        ),
        201,
    )
    assert got(await agent.get(f"/tasks/{made['gid']}", params={"opt_fields": "notes"}))["notes"] == "Call Bob & Alice"
