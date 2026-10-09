"""Followers, projects, dependencies, sections, tags, stories and project updates: each write comes back on the next
read exactly as the reference describes it."""

from __future__ import annotations

import httpx

from minutehand.adapters.providers.asana import state, wire
from minutehand.domain.world import Actor
from tests.providers.asana.asana_workspace import (
    CATERING,
    VENUE,
    Workspace,
    body,
    client,
    create,
    error,
    items,
    unserved,
    workspace,
)
from tests.providers.asana.rich_workspace import got

__all__ = ["client", "workspace"]

IRIS = state.user_gid("iris")
TOMAS = state.user_gid("tomas")
EMPTY = {"data": {}}


async def sections(client: httpx.AsyncClient, project: str) -> list[dict[str, object]]:
    return items(await client.get(f"/projects/{project}/sections"))


# ---------------------------------------------------------------------- followers


async def test_followers_are_added_by_gid_email_or_me_and_the_whole_task_comes_back(
    client: httpx.AsyncClient,
) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    added = got(
        await client.post(
            f"/tasks/{made['gid']}/addFollowers",
            json={"data": {"followers": [TOMAS, "iris@example.com", "me"]}},
        )
    )
    assert added["name"] == "Pack"
    followers = got(await client.get(f"/tasks/{made['gid']}", params={"opt_fields": "followers.name"}))["followers"]
    assert [f["gid"] for f in followers] == [TOMAS, IRIS, state.AGENT_GID]
    assert [f["gid"] for f in added["followers"]] == [TOMAS, IRIS, state.AGENT_GID]


async def test_a_follower_added_twice_is_held_once_and_removing_one_leaves_the_rest(
    client: httpx.AsyncClient,
) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    for _ in range(2):
        await client.post(f"/tasks/{made['gid']}/addFollowers", json={"data": {"followers": [TOMAS, IRIS]}})
    removed = got(await client.post(f"/tasks/{made['gid']}/removeFollowers", json={"data": {"followers": [TOMAS]}}))
    assert [f["gid"] for f in removed["followers"]] == [IRIS]
    again = got(await client.post(f"/tasks/{made['gid']}/removeFollowers", json={"data": {"followers": [TOMAS]}}))
    assert [f["gid"] for f in again["followers"]] == [IRIS]


async def test_a_follower_naming_nobody_is_refused(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    answered = await client.post(f"/tasks/{made['gid']}/addFollowers", json={"data": {"followers": ["9999"]}})
    assert error(answered, 400) == "followers: Unknown object: 9999"


async def test_followers_missing_are_refused_missing_input(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    answered = await client.post(f"/tasks/{made['gid']}/addFollowers", json={"data": {}})
    assert error(answered, 400) == "followers: Missing input"


# ---------------------------------------------------------------------- addProject, removeProject


async def test_add_project_puts_the_task_at_the_end_of_the_project_and_remove_project_takes_it_out(
    client: httpx.AsyncClient,
) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    last = (await sections(client, CATERING))[-1]
    assert body(await client.post(f"/tasks/{made['gid']}/addProject", json={"data": {"project": CATERING}})) == EMPTY
    read = got(
        await client.get(f"/tasks/{made['gid']}", params={"opt_fields": "memberships.project,memberships.section"})
    )
    assert [(m["project"]["gid"], m["section"]["gid"]) for m in read["memberships"]] == [
        (VENUE, (await sections(client, VENUE))[0]["gid"]),
        (CATERING, last["gid"]),
    ]
    assert made["gid"] in [t["gid"] for t in items(await client.get(f"/projects/{CATERING}/tasks"))]
    assert body(await client.post(f"/tasks/{made['gid']}/removeProject", json={"data": {"project": VENUE}})) == EMPTY
    after = got(await client.get(f"/tasks/{made['gid']}", params={"opt_fields": "projects"}))
    assert [p["gid"] for p in after["projects"]] == [CATERING]
    assert made["gid"] not in [t["gid"] for t in items(await client.get(f"/projects/{VENUE}/tasks"))]


async def test_add_project_names_the_section_it_goes_into(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    first = (await sections(client, CATERING))[0]
    await client.post(f"/tasks/{made['gid']}/addProject", json={"data": {"project": CATERING, "section": first["gid"]}})
    read = got(await client.get(f"/tasks/{made['gid']}", params={"opt_fields": "memberships.section"}))
    assert read["memberships"][-1]["section"]["gid"] == first["gid"]


async def test_add_project_for_a_task_already_in_it_moves_it_to_the_section_named(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    second = (await sections(client, VENUE))[1]
    await client.post(f"/tasks/{made['gid']}/addProject", json={"data": {"project": VENUE, "section": second["gid"]}})
    read = got(await client.get(f"/tasks/{made['gid']}", params={"opt_fields": "memberships.section"}))
    assert [m["section"]["gid"] for m in read["memberships"]] == [second["gid"]]


async def test_add_project_with_a_position_is_refused_by_name(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    answered = await client.post(
        f"/tasks/{made['gid']}/addProject", json={"data": {"project": CATERING, "insert_after": None}}
    )
    assert unserved(answered) == "insert_after"


async def test_remove_project_for_a_project_the_task_is_not_in_is_refused_by_name(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    answered = await client.post(f"/tasks/{made['gid']}/removeProject", json={"data": {"project": CATERING}})
    assert "removing a task from a project it is not in" in unserved(answered)


# ---------------------------------------------------------------------- dependencies and dependents


async def test_dependencies_are_linked_listed_and_seen_from_the_other_end(client: httpx.AsyncClient) -> None:
    first = await create(client, name="Pour", projects=[VENUE])
    second = await create(client, name="Paint", projects=[VENUE])
    sent = {"data": {"dependencies": [first["gid"]]}}
    assert body(await client.post(f"/tasks/{second['gid']}/addDependencies", json=sent)) == EMPTY
    assert [t["gid"] for t in items(await client.get(f"/tasks/{second['gid']}/dependencies"))] == [first["gid"]]
    assert [t["gid"] for t in items(await client.get(f"/tasks/{first['gid']}/dependents"))] == [second["gid"]]
    fields = got(await client.get(f"/tasks/{second['gid']}", params={"opt_fields": "dependencies,dependents"}))
    assert fields["dependencies"] == [{"gid": first["gid"], "resource_type": "task"}] and fields["dependents"] == []
    other = got(await client.get(f"/tasks/{first['gid']}", params={"opt_fields": "dependents"}))
    assert other["dependents"] == [{"gid": second["gid"], "resource_type": "task"}]
    await client.post(f"/tasks/{second['gid']}/removeDependencies", json=sent)
    assert items(await client.get(f"/tasks/{second['gid']}/dependencies")) == []


async def test_dependents_are_linked_from_the_task_they_wait_on(client: httpx.AsyncClient) -> None:
    first = await create(client, name="Pour", projects=[VENUE])
    second = await create(client, name="Paint", projects=[VENUE])
    sent = {"data": {"dependents": [second["gid"]]}}
    assert body(await client.post(f"/tasks/{first['gid']}/addDependents", json=sent)) == EMPTY
    assert [t["gid"] for t in items(await client.get(f"/tasks/{second['gid']}/dependencies"))] == [first["gid"]]
    await client.post(f"/tasks/{first['gid']}/removeDependents", json=sent)
    assert items(await client.get(f"/tasks/{first['gid']}/dependents")) == []


async def test_a_dependency_naming_no_task_is_refused_and_one_on_itself_is_refused_by_name(
    client: httpx.AsyncClient,
) -> None:
    made = await create(client, name="Pour", projects=[VENUE])
    missing = await client.post(f"/tasks/{made['gid']}/addDependencies", json={"data": {"dependencies": ["9999"]}})
    assert error(missing, 400) == "dependencies: Unknown object: 9999"
    itself = await client.post(f"/tasks/{made['gid']}/addDependencies", json={"data": {"dependencies": [made["gid"]]}})
    assert "a task that depends on itself" in unserved(itself)


# ---------------------------------------------------------------------- sections


async def test_a_section_is_renamed_and_the_whole_section_comes_back(client: httpx.AsyncClient) -> None:
    section = (await sections(client, VENUE))[0]
    renamed = got(await client.put(f"/sections/{section['gid']}", json={"data": {"name": "Backlog"}}))
    assert (renamed["gid"], renamed["name"]) == (section["gid"], "Backlog")
    assert next(s["name"] for s in await sections(client, VENUE)) == "Backlog"


async def test_an_empty_section_is_deleted_and_one_holding_a_task_is_refused_by_name(
    client: httpx.AsyncClient,
) -> None:
    held = (await sections(client, VENUE))[0]
    made = await create(client, name="Pack", projects=[VENUE])
    refused = await client.delete(f"/sections/{held['gid']}")
    assert "deleting a section that holds tasks" in unserved(refused)
    spare = got(await client.post(f"/projects/{VENUE}/sections", json={"data": {"name": "Spare"}}), 201)
    assert body(await client.delete(f"/sections/{spare['gid']}")) == EMPTY
    assert (await client.get(f"/sections/{spare['gid']}")).status_code == 404
    assert made["gid"] in [t["gid"] for t in items(await client.get(f"/projects/{VENUE}/tasks"))]


async def test_the_last_section_of_a_project_is_not_deleted(client: httpx.AsyncClient) -> None:
    team = items(await client.get(f"/workspaces/{state.WORKSPACE_GID}/teams"))[0]
    project = got(await client.post("/projects", json={"data": {"name": "Fresh", "team": team["gid"]}}), 201)
    only = (await sections(client, str(project["gid"])))[0]
    answered = await client.delete(f"/sections/{only['gid']}")
    assert "deleting the last section of a project" in unserved(answered)


# ---------------------------------------------------------------------- tags


async def test_a_tag_is_updated_as_far_as_it_was_sent_and_deleted(client: httpx.AsyncClient) -> None:
    tag = got(await client.post("/tags", json={"data": {"name": "urgent", "workspace": state.WORKSPACE_GID}}), 201)
    changed = got(await client.put(f"/tags/{tag['gid']}", json={"data": {"name": "later", "notes": "Not this week."}}))
    assert (changed["name"], changed["notes"], changed["color"]) == ("later", "Not this week.", None)
    colored = got(await client.put(f"/tags/{tag['gid']}", json={"data": {"color": "dark-red"}}))
    assert (colored["name"], colored["color"]) == ("later", "dark-red")
    assert body(await client.delete(f"/tags/{tag['gid']}")) == EMPTY
    assert (await client.get(f"/tags/{tag['gid']}")).status_code == 404


async def test_a_tag_a_task_carries_is_not_deleted_it_is_refused_by_name(client: httpx.AsyncClient) -> None:
    tag = got(await client.post("/tags", json={"data": {"name": "urgent", "workspace": state.WORKSPACE_GID}}), 201)
    made = await create(client, name="Pack", projects=[VENUE])
    await client.post(f"/tasks/{made['gid']}/addTag", json={"data": {"tag": tag["gid"]}})
    answered = await client.delete(f"/tags/{tag['gid']}")
    assert "deleting a tag that tasks carry" in unserved(answered)


# ---------------------------------------------------------------------- stories


async def test_a_story_is_read_edited_and_deleted_by_its_writer(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    story = got(await client.post(f"/tasks/{made['gid']}/stories", json={"data": {"text": "First thought"}}), 201)
    read = got(await client.get(f"/stories/{story['gid']}"))
    assert (read["text"], read["is_edited"]) == ("First thought", False)
    edited = got(await client.put(f"/stories/{story['gid']}", json={"data": {"text": "Second thought"}}))
    assert (edited["text"], edited["is_edited"]) == ("Second thought", True)
    assert [s["text"] for s in items(await client.get(f"/tasks/{made['gid']}/stories"))] == ["Second thought"]
    assert body(await client.delete(f"/stories/{story['gid']}")) == EMPTY
    assert items(await client.get(f"/tasks/{made['gid']}/stories")) == []
    assert (await client.get(f"/stories/{story['gid']}")).status_code == 404


async def test_a_story_edited_with_html_or_pinned_is_refused_by_name(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    story = got(await client.post(f"/tasks/{made['gid']}/stories", json={"data": {"text": "First"}}), 201)
    html = await client.put(f"/stories/{story['gid']}", json={"data": {"html_text": "<body>x</body>"}})
    pinned = await client.put(f"/stories/{story['gid']}", json={"data": {"text": "x", "is_pinned": True}})
    assert (unserved(html), unserved(pinned)) == ("html_text", "is_pinned")


async def test_deleting_a_story_another_user_wrote_is_refused_by_name(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    made = await create(client, name="Pack", projects=[VENUE])
    theirs = wire.AsanaStory(
        gid=workspace.asana.next_gid(),
        text="Tomas's",
        task=str(made["gid"]),
        created_by=TOMAS,
        created_at=wire.stamp(workspace.clock.now()),
    )
    workspace.asana.put_story(theirs, actor=Actor.PERSON, by=TOMAS)
    answered = await client.delete(f"/stories/{theirs.gid}")
    assert "deleting a story another user wrote" in unserved(answered)
    assert [s["text"] for s in items(await client.get(f"/tasks/{made['gid']}/stories"))] == ["Tomas's"]


# ---------------------------------------------------------------------- project update


async def test_a_project_is_renamed_described_and_archived(client: httpx.AsyncClient) -> None:
    updated = got(
        await client.put(
            f"/projects/{CATERING}", json={"data": {"name": "Catering 2", "notes": "Menus", "archived": True}}
        )
    )
    assert (updated["name"], updated["notes"], updated["archived"]) == ("Catering 2", "Menus", True)
    again = got(await client.put(f"/projects/{CATERING}", json={"data": {"archived": False}}))
    assert (again["name"], again["archived"]) == ("Catering 2", False)


async def test_a_project_property_the_fake_does_not_serve_is_refused_by_name(client: httpx.AsyncClient) -> None:
    answered = await client.put(f"/projects/{CATERING}", json={"data": {"color": "light-green"}})
    assert unserved(answered) == "color"
