"""What the fake answers is what Asana answers: data as it was sent, `opt_fields` answering exactly what it names, a
full record without what Asana's OpenAPI document marks [Opt In], and a field Asana has and this fake does not serve
refused by name. Each case pins one behaviour fixed against the reference (`CLAIMS.md` beside the provider)."""

from __future__ import annotations

import httpx
import pytest

from minutehand.adapters.providers.asana import state
from tests.providers.asana.asana_workspace import Workspace, unserved
from tests.providers.asana.rich_workspace import BACKEND, INCIDENT, POINTS, agent, got, option, rich

__all__ = ["agent", "rich"]

LAUNCH = state.field_gid("Launch")


async def test_a_task_comes_back_with_its_words_as_they_were_sent(agent: httpx.AsyncClient) -> None:
    sent = {
        "name": "  Fix  the\tlogin  ",
        "notes": "Line one\n\n  <b>not markup</b> & more\n",
        "projects": [BACKEND],
        "due_at": "2026-09-02T15:00:00+02:00",
        "custom_fields": {POINTS: 5, LAUNCH: {"date_time": "2026-09-03T09:30:00+02:00"}},
    }
    made = got(await agent.post("/tasks", json={"data": sent}), 201)
    read = got(
        await agent.get(
            f"/tasks/{made['gid']}",
            params={"opt_fields": "name,notes,due_at,custom_fields.number_value,custom_fields.date_value"},
        )
    )
    values = {f["gid"]: f for f in read["custom_fields"]}
    assert (read["name"], read["notes"], read["due_at"]) == (sent["name"], sent["notes"], sent["due_at"])
    assert values[POINTS]["number_value"] == 5 and isinstance(values[POINTS]["number_value"], int)
    assert values[LAUNCH]["date_value"] == {"date": "2026-09-03", "date_time": "2026-09-03T09:30:00+02:00"}


async def test_opt_fields_answers_exactly_what_it_names_and_the_gid(agent: httpx.AsyncClient) -> None:
    """https://developers.asana.com/docs/inputoutput-options: "The `gid` of included objects will always be
    returned, regardless of the field options"; beyond it, exactly the fields named."""
    read = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "name,assignee.name"}))
    assert read == {
        "gid": INCIDENT,
        "name": "API timeout in production",
        "assignee": {"gid": state.user_gid("bob"), "name": "Bob Taylor"},
    }


async def test_opt_fields_paths_may_start_with_this_and_group_terms(agent: httpx.AsyncClient) -> None:
    """https://developers.asana.com/docs/inputoutput-options writes paths as `this.followers.email` and groups
    terms as `(followers|assignee)`."""
    plain = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "name,assignee.name,created_by.name"}))
    written = got(
        await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "this.name,this.(assignee|created_by).name"})
    )
    assert written == plain


@pytest.mark.parametrize(
    ("field", "named"),
    [
        ("followers", "followers"),
        ("subtasks.name", "subtasks"),
        ("html_notes", "html_notes"),
        ("assignee.photo", "assignee.photo"),
        ("name.first", "name.first"),
        ("nonsense", "nonsense"),
    ],
)
async def test_a_field_this_fake_never_answers_is_refused_501_naming_it(
    agent: httpx.AsyncClient, field: str, named: str
) -> None:
    """A field Asana has that is not served here (`followers`), one Asana's document does not give a task
    (`subtasks`), or a path into a string: answering without it would say it is absent or empty."""
    answered = await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": f"name,{field}"})
    assert unserved(answered) == f"opt_fields={named}"


async def test_a_full_task_leaves_out_what_is_opt_in_until_it_is_asked_for(agent: httpx.AsyncClient) -> None:
    """Asana's OpenAPI document marks `num_subtasks`, `dependencies` and `dependents` [Opt In] (TaskBase), and gives
    no task a `subtasks` field. Nothing in the world links tasks (`addDependencies` is refused by name), so a task's
    dependencies are none."""
    full = got(await agent.get(f"/tasks/{INCIDENT}"))
    assert not {"num_subtasks", "dependencies", "dependents", "subtasks", "html_notes"} & set(full)
    assert got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "num_subtasks"}))["num_subtasks"] == 1
    linked = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "dependencies,dependents"}))
    assert linked == {"gid": INCIDENT, "dependencies": [], "dependents": []}


async def test_a_full_task_holds_its_custom_fields_full_and_a_listed_one_compact(agent: httpx.AsyncClient) -> None:
    """`TaskResponse.custom_fields` is `[CustomFieldResponse]`; a listed custom field is `CustomFieldCompact`, which
    has no `resource_subtype` or `precision`."""
    full = got(await agent.get(f"/tasks/{INCIDENT}"))
    points = next(f for f in full["custom_fields"] if f["gid"] == POINTS)
    assert (points["resource_subtype"], points["precision"], points["number_value"]) == ("number", 1, 5)
    listed = got(await agent.get(f"/workspaces/{state.WORKSPACE_GID}/custom_fields"))
    assert all("resource_subtype" not in f and "precision" not in f for f in listed)


async def test_a_project_membership_answers_neither_an_invented_access_level_nor_opt_in_fields(
    rich: Workspace, agent: httpx.AsyncClient
) -> None:
    """`ProjectMembershipCompact.parent` and `ProjectMembershipNormalResponse.project` are [Opt In]; the world holds
    no access level, so `access_level` and `write_access` are refused by name, not answered "editor"."""
    del rich
    listed = got(await agent.get(f"/projects/{BACKEND}/project_memberships"))
    assert all(set(m) == {"gid", "resource_type", "member"} for m in listed), listed
    asked = got(await agent.get(f"/projects/{BACKEND}/project_memberships", params={"opt_fields": "parent.name"}))
    assert asked[0]["parent"] == {"gid": BACKEND, "name": "Backend Services"}
    refused = await agent.get(f"/projects/{BACKEND}/project_memberships", params={"opt_fields": "access_level"})
    assert unserved(refused) == "opt_fields=access_level"


async def test_an_enum_value_is_named_by_its_option(agent: httpx.AsyncClient) -> None:
    read = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "custom_fields.enum_value.name"}))
    chosen = [f["enum_value"] for f in read["custom_fields"] if "enum_value" in f and f["enum_value"] is not None]
    assert {"gid": option("Priority", "Critical"), "name": "Critical"} in chosen
