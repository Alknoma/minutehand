"""Facts about how YouTrack and its Hub answer calls about projects, their fields and their teams, each carried over
from an older stand-in that had learned it, driven through the run's proxy with `httpx`. Each docstring says whether
the fact is in the vendor's documentation (with the page), was recorded from a live instance, or is the older
stand-in's own and unverified. `CLAIMS.md` beside
the provider lists them all."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from tests.providers.youtrack.caller_world import scenario_with, seeded
from tests.providers.youtrack.youtrack_instance import Instance, entities, entity, named, proxied, refusal

LAUNCH, OPS, CREATED = "0-1000", "0-1001", "0-1002"
AGENT, IRIS, TOMAS, VENDOR = "1-0", "1-1", "1-2", "1-10000"
UPDATE_PROJECT = "jetbrains.jetpass.project-update"
READ_PROJECT = "jetbrains.jetpass.project-read-basic"
TEMPLATE_FIELDS = {"Priority", "Type", "State", "Assignee"}
PROJECT_STAGE = "customFields(field(name),bundle(values(name,isResolved)))"


async def create_project(yt: httpx.AsyncClient, *, leader: str = IRIS, template: str | None = None) -> httpx.Response:
    params = {"fields": "id,shortName,name,leader(id,login)"} | ({"template": template} if template else {})
    return await yt.post(
        "/api/admin/projects",
        params=params,
        json={"name": "Partner Summit", "shortName": "SUMMIT", "leader": {"id": leader}},
    )


async def project_field_names(yt: httpx.AsyncClient, project: str) -> list[object]:
    read = entity(await yt.get(f"/api/admin/projects/{project}", params={"fields": "customFields(field(name))"}))
    return [f["field"]["name"] for f in read["customFields"]]  # type: ignore[index,union-attr]


async def team_ids(yt: httpx.AsyncClient, project: str) -> list[object]:
    read = entity(await yt.get(f"/api/admin/projects/{project}/team", params={"fields": "users(id)"}))
    return [u["id"] for u in read["users"]]  # type: ignore[index,union-attr]


async def ring_id(yt: httpx.AsyncClient, user: str) -> str:
    found = entity(await yt.get(f"/api/users/{user}", params={"fields": "ringId"}))["ringId"]
    assert isinstance(found, str)
    return found


@asynccontextmanager
async def granted(tmp_path: Path, grants: list[dict[str, object]]) -> AsyncIterator[httpx.AsyncClient]:
    """The agent's client on an instance whose seed gives or takes these permissions."""
    async with proxied(seeded(tmp_path, scenario_with(grants=grants)), tmp_path / "ca") as http:
        yield http


# --------------------------------------------------------------------------- the field register


async def test_the_field_register_answers_only_the_attributes_asked_for(yt: httpx.AsyncClient) -> None:
    """Documented: an attribute comes back only when `fields` names it
    (https://www.jetbrains.com/help/youtrack/devportal/api-fields-syntax.html), the instance's field register too."""
    register = entities(await yt.get("/api/admin/customFieldSettings/customFields", params={"fields": "name"}))

    assert register and all("fieldType" not in f for f in register)
    assert "Due Date" in [f["name"] for f in register]


# --------------------------------------------------------------------------- creating a project


async def test_a_created_project_lists_takes_an_issue_and_answers_its_leader(yt: httpx.AsyncClient) -> None:
    """Documented: a project is created from a name, a short name and a leader named by database id, and the short
    name prefixes its issues (https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html)."""
    made = entity(await create_project(yt))
    listed = entities(await yt.get("/api/admin/projects", params={"fields": "id,shortName"}))
    filed = entity(
        await yt.post(
            "/api/issues", params={"fields": "idReadable"}, json={"project": {"id": made["id"]}, "summary": "x"}
        )
    )

    assert made["leader"] == {"id": IRIS, "login": "iris", "$type": "User"}
    assert {"id": CREATED, "shortName": "SUMMIT", "$type": "Project"} in listed
    assert filed["idReadable"] == "SUMMIT-1"


async def test_a_project_with_no_leader_is_refused_400_naming_the_leader(yt: httpx.AsyncClient, team: Instance) -> None:
    """Documented: `leader` is required on a project create
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html)."""
    head = team.store.head()

    refused = refusal(await yt.post("/api/admin/projects", json={"name": "Partner Summit", "shortName": "SUMMIT"}), 400)

    assert "leader" in str(refused["error_description"])
    assert team.store.head() == head


async def test_a_login_as_leader_is_refused_400_and_a_short_name_in_use_501(yt: httpx.AsyncClient) -> None:
    """Documented: the leader is named by database id
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html), so a login there is
    refused by its shape (https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-ring-id.html). What a
    short name already taken answers is neither documented nor recorded: refused by name."""
    by_login = refusal(
        await yt.post("/api/admin/projects", json={"name": "Summit", "shortName": "SUMMIT", "leader": {"id": "iris"}}),
        400,
    )
    taken = refusal(
        await yt.post("/api/admin/projects", json={"name": "Again", "shortName": "OPS", "leader": {"id": IRIS}}), 501
    )

    assert by_login["error_description"] == "Invalid structure of entity id: iris"
    assert "shortName OPS another project holds" in str(taken["error_description"])


@pytest.mark.parametrize(
    ("template", "carried"),
    [
        (None, ["Priority", "Type", "State", "Assignee"]),
        ("scrum", ["Type", "Assignee", "State", "Ideal days", "Story Points", "Original estimation"]),
        ("kanban", ["Stage", "Assignee", "Priority"]),
    ],
)
async def test_a_template_gives_a_project_the_fields_its_page_lists(
    yt: httpx.AsyncClient, template: str | None, carried: list[str]
) -> None:
    """Documented: `template` takes scrum or kanban, none the Default template
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html), and each template's page
    lists the fields it attaches (https://www.jetbrains.com/help/youtrack/cloud/default-project-template.html,
    .../scrum-project-template.html, .../kanban-project-template.html), less those of types this fake does not hold.
    None gives a Due Date."""
    made = entity(await create_project(yt, template=template))

    assert made["shortName"] == "SUMMIT"
    assert await project_field_names(yt, CREATED) == carried


async def test_the_kanban_template_starts_an_issue_in_backlog(yt: httpx.AsyncClient) -> None:
    """Documented: the Kanban template's Stage runs Backlog to Done, Done resolved, Backlog the default
    (https://www.jetbrains.com/help/youtrack/cloud/kanban-project-template.html)."""
    made = entity(await create_project(yt, template="kanban"))
    filed = entity(
        await yt.post(
            "/api/issues",
            params={"fields": "customFields(name,value(name))"},
            json={"project": {"id": made["id"]}, "summary": "Card"},
        )
    )
    stage = entity(await yt.get(f"/api/admin/projects/{CREATED}", params={"fields": PROJECT_STAGE}))

    assert named(filed["customFields"], "Stage")["value"] == {"name": "Backlog", "$type": "StateBundleElement"}
    values = named(stage["customFields"], "Stage")["bundle"]["values"]  # type: ignore[index]
    assert [(v["name"], v["isResolved"]) for v in values] == [  # type: ignore[index]
        ("Backlog", False), ("Develop", False), ("Review", False), ("Test", False), ("Staging", False), ("Done", True),
    ]  # fmt: skip


@pytest.mark.parametrize("template", ["0-1001", "OPS", "agile"])
async def test_a_custom_template_is_refused_by_name_and_creates_nothing(
    yt: httpx.AsyncClient, team: Instance, template: str
) -> None:
    """Documented: the reference lists scrum and kanban as `template`'s values
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html); YouTrack also has custom
    project templates, which this fake does not serve, so any other value is a 501 naming it."""
    head = team.store.head()

    refused = refusal(await create_project(yt, template=template), 501)

    assert f"template={template}" in str(refused["error_description"])
    assert team.store.head() == head


async def test_a_numeric_short_name_is_refused_in_youtracks_words(yt: httpx.AsyncClient, team: Instance) -> None:
    """Documented with its body: https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-numeric-project-id.html"""
    head = team.store.head()
    refused = refusal(
        await yt.post("/api/admin/projects", json={"name": "369", "shortName": "369", "leader": {"id": IRIS}}), 400
    )

    assert refused == {
        "error": "invalid_properties",
        "error_description": "Project ID cannot be numeric",
        "error_children": [
            {
                "error": "no-type-is-invalid",
                "error_description": "Project ID cannot be numeric",
                "error_developer_message": "Project ID cannot be numeric",
            }
        ],
    }
    assert team.store.head() == head


async def test_a_project_name_and_short_name_are_kept_as_sent(yt: httpx.AsyncClient) -> None:
    """The fake stores what it is sent: no trimming of a name or a short name."""
    made = entity(
        await yt.post(
            "/api/admin/projects",
            params={"fields": "name,shortName"},
            json={"name": " Partner Summit ", "shortName": "SUMMIT_2", "leader": {"id": IRIS}},
        )
    )

    assert made == {"name": " Partner Summit ", "shortName": "SUMMIT_2", "$type": "Project"}


async def test_an_issue_in_a_created_project_carries_only_its_projects_fields(yt: httpx.AsyncClient) -> None:
    """Unverified, the older stand-in's own: a project made through the API carries its template's fields and no Due Date, an issue
    in it carries those fields and no others, and a write to another project's Due Date there is a 404."""
    made = entity(await create_project(yt, leader=AGENT))
    filed = entity(
        await yt.post(
            "/api/issues", params={"fields": "idReadable"}, json={"project": {"id": made["id"]}, "summary": "x"}
        )
    )
    read = entity(await yt.get(f"/api/issues/{filed['idReadable']}", params={"fields": "customFields(id,name)"}))
    launch = entity(await yt.get("/api/issues/LAUNCH-1", params={"fields": "customFields(id,name)"}))
    due = named(launch["customFields"], "Due Date")["id"]

    assert {f["name"] for f in read["customFields"]} == TEMPLATE_FIELDS  # type: ignore[union-attr,index]
    assert set(await project_field_names(yt, CREATED)) == TEMPLATE_FIELDS
    refusal(await yt.post(f"/api/issues/{filed['idReadable']}/customFields/{due}", json={"value": 1788004800000}), 404)


# --------------------------------------------------------------------------- attaching a field


async def register_id(yt: httpx.AsyncClient, name: str) -> object:
    register = entities(await yt.get("/api/admin/customFieldSettings/customFields", params={"fields": "id,name"}))
    return named(register, name)["id"]


async def test_a_bundled_field_attached_without_its_bundle_is_refused_501_naming_it(yt: httpx.AsyncClient) -> None:
    """Documented: a bundled field is attached as a bundle field that names its bundle
    (https://www.jetbrains.com/help/youtrack/devportal/api-entity-BundleProjectCustomField.html). What one attached
    bare answers is neither documented nor recorded: refused by name, attaching nothing."""
    sprint = await register_id(yt, "Sprint")

    refusal(
        await yt.post(
            f"/api/admin/projects/{OPS}/customFields",
            json={"field": {"id": sprint}, "$type": "VersionProjectCustomField"},
        ),
        501,
    )

    assert "Sprint" not in await project_field_names(yt, OPS)


async def test_a_field_named_by_its_name_is_refused_by_shape(yt: httpx.AsyncClient) -> None:
    """Unverified, the older stand-in's own: the field to attach is named by database id; its name there is refused by shape."""
    refused = refusal(
        await yt.post(
            f"/api/admin/projects/{OPS}/customFields",
            json={"field": {"id": "Due Date"}, "$type": "SimpleProjectCustomField"},
        ),
        400,
    )

    assert refused["error_description"] == "Invalid structure of entity id: Due Date"


# --------------------------------------------------------------------------- a project's team


async def test_a_created_projects_team_is_its_leader_and_its_maker(yt: httpx.AsyncClient) -> None:
    """Documented: the user who creates a project is added to its team, and the Assignee field takes the team
    (https://www.jetbrains.com/help/youtrack/cloud/create-new-project.html). Observed on a live instance: a project
    made through the API by the account that also led it was teamed by that account alone, so nobody else could be
    assigned there."""
    made = entity(await create_project(yt, leader=IRIS))
    filed = entity(
        await yt.post(
            "/api/issues", params={"fields": "idReadable"}, json={"project": {"id": made["id"]}, "summary": "x"}
        )
    )
    key = filed["idReadable"]
    fields = entity(await yt.get(f"/api/issues/{key}", params={"fields": "customFields(id,name)"}))
    assignee = named(fields["customFields"], "Assignee")["id"]

    assert await team_ids(yt, CREATED) == [IRIS, AGENT]
    assert entity(await yt.post(f"/api/issues/{key}/customFields/{assignee}", json={"value": {"id": IRIS}}))
    refused = refusal(await yt.post(f"/api/issues/{key}/customFields/{assignee}", json={"value": {"id": TOMAS}}), 400)
    assert refused["error_description"] == "Value is not allowed"


async def test_a_team_is_a_group_of_its_own_and_youtrack_withholds_its_hub_id(yt: httpx.AsyncClient) -> None:
    """Documented: a project's `team` is a ProjectTeam, a user group with `name`, `ringId`, `usersCount` and `users`
    (https://www.jetbrains.com/help/youtrack/devportal/api-entity-ProjectTeam.html). Observed on a live instance:
    its `ringId` is null while each member's is a Hub id, and it is named after the project."""
    made = entity(await create_project(yt))

    group = entity(
        await yt.get(
            f"/api/admin/projects/{made['id']}/team", params={"fields": "id,name,ringId,usersCount,users(id,ringId)"}
        )
    )

    assert (group["name"], group["usersCount"], group["ringId"]) == ("Partner Summit Team", 2, None)
    assert group["id"] != made["id"]
    assert group["users"][0]["ringId"]  # type: ignore[index]


async def test_an_unknown_projects_team_is_refused_404(yt: httpx.AsyncClient) -> None:
    """Recorded from JetBrains' public instance (`data/observed/unknown_project_team.http`): the team of a project id
    naming nothing is a 404 "Entity with id … not found"."""
    refused = refusal(await yt.get("/api/admin/projects/0-99/team", params={"fields": "users(id)"}), 404)

    assert refused == {"error": "Not Found", "error_description": "Entity with id 0-99 not found"}


async def test_youtracks_team_users_route_refuses_a_post_405_and_changes_nothing(yt: httpx.AsyncClient) -> None:
    """Documented: `/admin/projects/{id}/team/users` takes GET alone in YouTrack's API description; a live instance
    answered a POST there 405. A user is added at `team/ownUsers`."""
    before = await team_ids(yt, LAUNCH)
    refusal(await yt.post(f"/api/admin/projects/{LAUNCH}/team/users", json={"id": VENDOR}), 405)

    assert await team_ids(yt, LAUNCH) == before


async def test_adding_a_member_twice_is_refused_501_and_leaves_one_membership(yt: httpx.AsyncClient) -> None:
    """What a second add of the same user answers is neither documented nor recorded: refused by name."""
    entity(await yt.post(f"/api/admin/projects/{LAUNCH}/team/ownUsers", json={"id": VENDOR}))
    again = refusal(await yt.post(f"/api/admin/projects/{LAUNCH}/team/ownUsers", json={"id": VENDOR}), 501)

    assert "which already holds them" in str(again["error_description"])

    assert (await team_ids(yt, LAUNCH)).count(VENDOR) == 1


async def test_the_assignee_bundle_and_the_team_agree_after_an_add(yt: httpx.AsyncClient) -> None:
    """Documented: the Assignee field's values are the members of the project team
    (https://www.jetbrains.com/help/youtrack/cloud/default-project-template.html)."""
    entity(await yt.post(f"/api/admin/projects/{LAUNCH}/team/ownUsers", json={"id": VENDOR}))

    fields = entities(
        await yt.get(
            f"/api/admin/projects/{LAUNCH}/customFields", params={"fields": "field(name),bundle(aggregatedUsers(id))"}
        )
    )
    bundled = [u["id"] for u in named(fields, "Assignee")["bundle"]["aggregatedUsers"]]  # type: ignore[index]

    assert bundled == await team_ids(yt, LAUNCH)
    assert bundled[-1] == VENDOR


async def test_a_team_add_naming_a_hub_id_is_refused_by_shape(yt: httpx.AsyncClient, team: Instance) -> None:
    """Documented: the body names the user by database id; a Ring id there is refused as
    https://www.jetbrains.com/help/youtrack/devportal/api-troubleshoot-ring-id.html shows."""
    ring = await ring_id(yt, VENDOR)
    head = team.store.head()

    refused = refusal(await yt.post(f"/api/admin/projects/{LAUNCH}/team/ownUsers", json={"id": ring}), 400)

    assert refused == {"error": "bad_request", "error_description": f"Invalid structure of entity id: {ring}"}
    assert team.store.head() == head


# --------------------------------------------------------------------------- Hub


async def test_hub_answers_no_youtrack_project(yt: httpx.AsyncClient) -> None:
    """Documented: since YouTrack 2026.1 Hub's `/projects` returns Hub data and no YouTrack project
    (https://www.jetbrains.com/help/youtrack/devportal/hub-rest-api-deprecated-endpoints-2026-1.html). Observed on a
    live instance: `total: 0` for a project the account had made."""
    await create_project(yt, leader=AGENT)
    for query in ("key: LAUNCH", "key: SUMMIT", "colour: teal"):
        page = entity(await yt.get("/hub/api/rest/projects", params={"query": query, "fields": "id,key"}))
        assert (page["total"], page["projects"]) == (0, [])
    refusal(await yt.get("/hub/api/rest/projects/anything", params={"fields": "id"}), 404)


async def test_the_group_listing_shows_the_instance_groups_and_no_team(yt: httpx.AsyncClient) -> None:
    """Documented: Hub's project team endpoints no longer serve YouTrack teams since 2026.1 (same page). Observed on
    a live instance: the account saw All Users and Registered Users and no team group."""
    page = entity(await yt.get("/hub/api/rest/usergroups", params={"fields": "id,name,project(key)"}))

    groups = page["usergroups"]
    assert isinstance(groups, list)
    assert [g["name"] for g in groups] == ["All Users", "Registered Users"]


async def test_a_hub_group_that_is_no_instance_group_is_refused_404(yt: httpx.AsyncClient) -> None:
    """No Hub group is a YouTrack team since 2026.1, so a group id other than the instance's two names nothing."""
    refusal(await yt.post("/hub/api/rest/usergroups/no-such-group/users", json={"id": await ring_id(yt, VENDOR)}), 404)


async def test_adding_to_an_instance_wide_group_is_refused_by_name(yt: httpx.AsyncClient, team: Instance) -> None:
    """Membership of Hub's instance-wide groups is not served: a 501 naming it, and nothing written."""
    ring = await ring_id(yt, VENDOR)
    head = team.store.head()

    refused = refusal(await yt.post("/hub/api/rest/usergroups/all-users/users", json={"id": ring}), 501)

    assert "instance-wide groups" in str(refused["error_description"])
    assert team.store.head() == head


async def test_the_permissions_cache_leaves_out_a_withheld_project_and_one_made_through_the_api(
    tmp_path: Path,
) -> None:
    """Recorded from a live instance (the keys, global or per project, Hub ids beside keys), undocumented: Hub's
    permissions cache lists a per-project permission as not
    global, by the projects it is held in with Hub ids beside their keys; a project it was taken from is not listed,
    nor one made through the API."""
    async with granted(
        tmp_path, [{"login": "agent-bot", "permission": UPDATE_PROJECT, "project": "LAUNCH", "held": False}]
    ) as yt:
        await create_project(yt, leader=AGENT)
        cache = entities(
            await yt.get(
                "/hub/api/rest/permissions/cache", params={"fields": "permission/key,global,projects/id,projects/key"}
            )
        )

    by_key = {e["permission"]["key"]: e for e in cache}  # type: ignore[index]
    update = by_key[UPDATE_PROJECT]
    assert update["global"] is False and by_key[READ_PROJECT]["global"] is False
    assert [p["key"] for p in update["projects"]] == ["OPS"]  # type: ignore[union-attr,index]
    assert all(p["id"] for p in update["projects"])  # type: ignore[union-attr,index]
