"""Facts about how YouTrack and its Hub answer calls about projects, their fields and their teams, each carried over
from an older stand-in that had learned it, driven through the run's proxy with `httpx`. Each docstring says whether
the fact is in the vendor's documentation (with the page) or was observed and is undocumented. `CLAIMS.md` beside
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


async def team_group(yt: httpx.AsyncClient, key: str) -> str:
    page = entity(await yt.get("/hub/api/rest/projects", params={"query": f"key: {key}", "fields": "team(id)"}))
    found = page["projects"][0]["team"]["id"]  # type: ignore[index]
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


async def test_a_login_as_leader_and_a_short_name_in_use_are_refused_400(yt: httpx.AsyncClient) -> None:
    """Documented: the leader is named by database id
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html). Observed: a login there is
    refused by its shape, and a short name already taken is refused as existing."""
    by_login = refusal(
        await yt.post("/api/admin/projects", json={"name": "Summit", "shortName": "SUMMIT", "leader": {"id": "iris"}}),
        400,
    )
    taken = refusal(
        await yt.post("/api/admin/projects", json={"name": "Again", "shortName": "OPS", "leader": {"id": IRIS}}), 400
    )

    assert by_login["error_description"] == "Invalid structure of entity id: iris"
    assert "already exists" in str(taken["error_description"])


@pytest.mark.parametrize("template", ["scrum", "kanban"])
async def test_a_stock_template_is_accepted_and_carries_no_due_date(yt: httpx.AsyncClient, template: str) -> None:
    """Documented: `template` takes one of YouTrack's own templates, scrum or kanban
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html). Observed: none of them gives
    the project a Due Date; that is attached afterwards or not had."""
    made = entity(await create_project(yt, template=template))

    assert made["shortName"] == "SUMMIT"
    assert "Due Date" not in await project_field_names(yt, CREATED)


@pytest.mark.parametrize("template", ["0-1001", "OPS", "0-99", "agile"])
async def test_a_template_that_is_not_a_stock_one_is_refused_400_and_creates_nothing(
    yt: httpx.AsyncClient, team: Instance, template: str
) -> None:
    """Documented: the only templates are scrum and kanban
    (https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html), so another project's id or
    short name in that slot names nothing. Observed, undocumented: it is refused, not read as "shape it like that
    project" nor quietly dropped."""
    head = team.store.head()

    refused = refusal(await create_project(yt, template=template), 400)

    assert "Unknown project template" in str(refused["error_description"])
    assert team.store.head() == head


async def test_an_issue_in_a_created_project_carries_only_its_projects_fields(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: a project made through the API carries its template's fields and no Due Date, an issue
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


async def test_a_bundled_field_attached_without_its_bundle_is_refused_400(yt: httpx.AsyncClient) -> None:
    """Documented: a bundled field is attached as a bundle field that names its bundle
    (https://www.jetbrains.com/help/youtrack/devportal/api-entity-BundleProjectCustomField.html); observed: one
    attached bare is refused."""
    sprint = await register_id(yt, "Sprint")

    refusal(
        await yt.post(
            f"/api/admin/projects/{OPS}/customFields",
            json={"field": {"id": sprint}, "$type": "VersionProjectCustomField"},
        ),
        400,
    )

    assert "Sprint" not in await project_field_names(yt, OPS)


async def test_a_field_named_by_its_name_is_refused_by_shape(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: the field to attach is named by database id; its name there is refused by shape."""
    refused = refusal(
        await yt.post(
            f"/api/admin/projects/{OPS}/customFields",
            json={"field": {"id": "Due Date"}, "$type": "SimpleProjectCustomField"},
        ),
        400,
    )

    assert refused["error_description"] == "Invalid structure of entity id: Due Date"


# --------------------------------------------------------------------------- a project's team


async def test_a_created_projects_team_is_its_leader_alone(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: a project made through the API is teamed by its leader alone, not its creator, so the
    leader can be assigned an issue there and another user of the instance cannot."""
    made = entity(await create_project(yt, leader=IRIS))
    filed = entity(
        await yt.post(
            "/api/issues", params={"fields": "idReadable"}, json={"project": {"id": made["id"]}, "summary": "x"}
        )
    )
    key = filed["idReadable"]
    fields = entity(await yt.get(f"/api/issues/{key}", params={"fields": "customFields(id,name)"}))
    assignee = named(fields["customFields"], "Assignee")["id"]

    assert await team_ids(yt, CREATED) == [IRIS]
    assert entity(await yt.post(f"/api/issues/{key}/customFields/{assignee}", json={"value": {"id": IRIS}}))
    refused = refusal(await yt.post(f"/api/issues/{key}/customFields/{assignee}", json={"value": {"id": TOMAS}}), 400)
    assert refused["error_description"] == "Value is not allowed"


async def test_a_team_is_a_group_of_its_own_and_youtrack_withholds_its_hub_id(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: the team answers as its own entity, named after the project, counting its users; its
    `ringId` is null while each member's is a Hub id."""
    made = entity(await create_project(yt))

    group = entity(
        await yt.get(
            f"/api/admin/projects/{made['id']}/team", params={"fields": "id,name,ringId,usersCount,users(id,ringId)"}
        )
    )

    assert (group["name"], group["usersCount"], group["ringId"]) == ("Partner Summit Team", 1, None)
    assert group["id"] != made["id"]
    assert group["users"][0]["ringId"]  # type: ignore[index]


async def test_an_unknown_projects_team_is_refused_404(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: the team of a project id naming nothing is a 404 in YouTrack's not-found words."""
    refused = refusal(await yt.get("/api/admin/projects/0-99/team", params={"fields": "users(id)"}), 404)

    assert refused["error_description"] == "Entity with id 0-99 not found"


async def test_youtracks_team_route_refuses_405_whatever_is_held_and_changes_nothing(tmp_path: Path) -> None:
    """Observed on a live instance, undocumented: YouTrack serves no write to a project's team; membership is Hub's.
    Holding Update Project everywhere does not open the route."""
    async with granted(tmp_path, [{"login": "agent-bot", "permission": UPDATE_PROJECT, "held": True}]) as yt:
        before = await team_ids(yt, LAUNCH)
        refusal(await yt.post(f"/api/admin/projects/{LAUNCH}/team/users", json={"id": VENDOR}), 405)

        assert await team_ids(yt, LAUNCH) == before


async def test_a_team_read_without_read_project_is_refused_403(tmp_path: Path) -> None:
    """Observed, undocumented: reading a team needs only Read Project Basic, and without it the read is a 403 naming
    the permission."""
    async with granted(
        tmp_path, [{"login": "agent-bot", "permission": READ_PROJECT, "project": "LAUNCH", "held": False}]
    ) as yt:
        refused = refusal(await yt.get(f"/api/admin/projects/{LAUNCH}/team", params={"fields": "users(id)"}), 403)
        assert await team_ids(yt, OPS)

    assert refused["error_description"] == "Insufficient permissions: Read Project Basic is required"


# --------------------------------------------------------------------------- Hub


async def test_hubs_team_id_is_not_youtracks_team_id(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: Hub and YouTrack each mint their own id for a team; neither answers the other's."""
    youtrack_side = entity(await yt.get(f"/api/admin/projects/{LAUNCH}/team", params={"fields": "id"}))["id"]

    assert await team_group(yt, "LAUNCH") != youtrack_side


async def test_hub_answers_no_project_the_account_may_not_read(tmp_path: Path) -> None:
    """Observed on a live instance, undocumented: a project the token may not read is not in Hub's answer, `total`
    0 and an empty page, the same as no project by that key."""
    async with granted(
        tmp_path, [{"login": "agent-bot", "permission": READ_PROJECT, "project": "LAUNCH", "held": False}]
    ) as yt:
        page = entity(await yt.get("/hub/api/rest/projects", params={"query": "key: LAUNCH", "fields": "id,key"}))

    assert (page["total"], page["projects"]) == (0, [])


async def test_hub_refuses_a_project_query_it_cannot_read_400(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: a query Hub does not understand is refused, never ignored to answer every project."""
    refusal(await yt.get("/hub/api/rest/projects", params={"query": "colour: teal", "fields": "id,key"}), 400)


async def test_the_group_listing_shows_the_instance_groups_and_only_the_teams_the_caller_manages(
    tmp_path: Path,
) -> None:
    """Observed on a live instance, undocumented: Hub lists All Users and Registered Users first, then the team
    group of each project the caller may update; not one it may not, nor one made through the API."""
    async with granted(
        tmp_path, [{"login": "agent-bot", "permission": UPDATE_PROJECT, "project": "OPS", "held": False}]
    ) as yt:
        await create_project(yt, leader=AGENT)
        page = entity(await yt.get("/hub/api/rest/usergroups", params={"fields": "id,name,project(key)"}))

    groups = page["usergroups"]
    assert isinstance(groups, list)
    assert [g["name"] for g in groups] == ["All Users", "Registered Users", "Launch Team"]
    assert groups[2]["project"]["key"] == "LAUNCH"


async def test_adding_a_member_twice_leaves_one_membership(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: a second add of the same user to a team group answers 200 and changes nothing."""
    group = await team_group(yt, "LAUNCH")
    ring = await ring_id(yt, VENDOR)

    entity(await yt.post(f"/hub/api/rest/usergroups/{group}/users", json={"id": ring}))
    entity(await yt.post(f"/hub/api/rest/usergroups/{group}/users", json={"id": ring}))

    assert (await team_ids(yt, LAUNCH)).count(VENDOR) == 1


async def test_the_assignee_bundle_and_the_team_agree_after_a_hub_add(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: the Assignee field draws on the team, so after a Hub add the field's users and the
    team name the same people in the same order."""
    group = await team_group(yt, "LAUNCH")
    entity(await yt.post(f"/hub/api/rest/usergroups/{group}/users", json={"id": await ring_id(yt, VENDOR)}))

    fields = entities(
        await yt.get(
            f"/api/admin/projects/{LAUNCH}/customFields", params={"fields": "field(name),bundle(aggregatedUsers(id))"}
        )
    )
    bundled = [u["id"] for u in named(fields, "Assignee")["bundle"]["aggregatedUsers"]]  # type: ignore[index]

    assert bundled == await team_ids(yt, LAUNCH)
    assert bundled[-1] == VENDOR


async def test_a_hub_add_with_no_user_id_is_refused_400(yt: httpx.AsyncClient, team: Instance) -> None:
    """Observed, undocumented: a membership add names the user; a body without one is refused."""
    group = await team_group(yt, "LAUNCH")
    head = team.store.head()

    refused = refusal(await yt.post(f"/hub/api/rest/usergroups/{group}/users", json={}), 400)

    assert refused["error_description"] == "user id is required"
    assert team.store.head() == head


async def test_a_hub_group_that_is_no_teams_is_refused_404(yt: httpx.AsyncClient) -> None:
    """Observed, undocumented: an add to a group id naming nothing is a 404."""
    refusal(await yt.post("/hub/api/rest/usergroups/no-such-group/users", json={"id": await ring_id(yt, VENDOR)}), 404)


async def test_an_instance_wide_group_is_refused_403(yt: httpx.AsyncClient, team: Instance) -> None:
    """Observed, undocumented: All Users is the instance's group, not a project's team, and the token may not add to
    it."""
    ring = await ring_id(yt, VENDOR)
    head = team.store.head()

    refusal(await yt.post("/hub/api/rest/usergroups/all-users/users", json={"id": ring}), 403)

    assert team.store.head() == head


async def test_withholding_update_project_in_one_project_leaves_another_writable(tmp_path: Path) -> None:
    """Observed, undocumented: Update Project is held per project, so taking it in one leaves a team add in
    another."""
    async with granted(
        tmp_path, [{"login": "agent-bot", "permission": UPDATE_PROJECT, "project": "LAUNCH", "held": False}]
    ) as yt:
        ring = await ring_id(yt, VENDOR)
        ops = entity(await yt.post(f"/hub/api/rest/usergroups/{await team_group(yt, 'OPS')}/users", json={"id": ring}))
        launch = refusal(
            await yt.post(f"/hub/api/rest/usergroups/{await team_group(yt, 'LAUNCH')}/users", json={"id": ring}), 403
        )

    assert ops["login"] == "vendor"
    assert launch["error_description"] == "Insufficient permissions: Update Project is required"


async def test_a_permission_taken_from_another_user_leaves_the_caller_alone(tmp_path: Path) -> None:
    """Observed, undocumented: permissions belong to a user, so one taken from Tomas leaves the agent's add."""
    async with granted(
        tmp_path, [{"login": "tomas", "permission": UPDATE_PROJECT, "project": "LAUNCH", "held": False}]
    ) as yt:
        group = await team_group(yt, "LAUNCH")
        entity(await yt.post(f"/hub/api/rest/usergroups/{group}/users", json={"id": await ring_id(yt, VENDOR)}))

        assert VENDOR in await team_ids(yt, LAUNCH)


async def test_the_permissions_cache_leaves_out_a_withheld_project_and_one_made_through_the_api(
    tmp_path: Path,
) -> None:
    """Observed on a live instance, undocumented: Hub's permissions cache lists a per-project permission as not
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
