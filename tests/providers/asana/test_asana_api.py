"""Tasks the agent writes read back, list, search and comment the way Asana answers them."""

from __future__ import annotations

from datetime import timedelta

import httpx

from minutehand.adapters.providers.asana import state
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation, TicketSnapshot
from tests.providers.asana.asana_workspace import (
    CATERING, START, VENUE, WS, Workspace, body, create, data, items,
)


async def test_a_created_task_reads_back_lists_under_its_project_and_is_found_by_search(
    workspace: Workspace, client: httpx.AsyncClient,
) -> None:
    made = await create(client, name="Order the signage", notes="Two banners.", projects=[VENUE],
                        assignee="tomas@example.com")
    read = data(await client.get(f"/tasks/{made['gid']}"))
    listed = items(await client.get(f"/projects/{VENUE}/tasks"))
    found = items(await client.get(f"/workspaces/{WS}/tasks/search", params={"text": "signage"}))

    assert read["name"] == "Order the signage" and read["notes"] == "Two banners."
    assert read["assignee"] == {"gid": state.user_gid("tomas"), "resource_type": "user", "name": "Tomas Brandt"}
    assert [m["gid"] for m in listed][-1] == made["gid"]
    assert [t["name"] for t in listed] == ["Book the freight lift", "Return the old keys", "Order the signage"]
    assert [t["gid"] for t in found] == [made["gid"]]


async def test_timestamps_are_the_runs_clock_not_the_machines(workspace: Workspace, client: httpx.AsyncClient) -> None:
    made = await create(client, name="Count chairs", workspace=WS)
    workspace.clock.jump(START + timedelta(days=2, hours=3))
    done = data(await client.put(f"/tasks/{made['gid']}", json={"data": {"completed": True}}))

    assert made["created_at"] == made["modified_at"] == "2026-08-24T10:50:03.250Z"
    assert done["completed_at"] == done["modified_at"] == "2026-08-26T13:50:03.250Z"
    assert done["created_at"] == "2026-08-24T10:50:03.250Z"


async def test_the_snapshot_carries_the_assignees_email_and_state(workspace: Workspace,
                                                                 client: httpx.AsyncClient) -> None:
    made = await create(client, name="Sign the lease", projects=[VENUE], assignee=state.user_gid("noor"))
    await client.put(f"/tasks/{made['gid']}", json={"data": {"completed": True, "assignee": "iris@example.com"}})

    writes = [e for e in workspace.store.events() if e.entity.external_id == made["gid"]]
    assert [(e.actor, e.operation) for e in writes] == [(Actor.AGENT, Operation.CREATE), (Actor.AGENT, Operation.UPDATE)]
    assert writes[0].entity.kind is EntityKind.TICKET
    assert writes[0].after == TicketSnapshot(title="Sign the lease", project="Venue Move",
                                             assignee_email="noor@example.com", state=TicketState.OPEN)
    second = writes[1].after
    assert isinstance(second, TicketSnapshot)
    assert (second.assignee_email, second.state) == ("iris@example.com", TicketState.DONE)


async def test_a_task_is_parented_under_its_project(workspace: Workspace, client: httpx.AsyncClient) -> None:
    made = await create(client, name="Tape the floor", projects=[CATERING])
    stored = workspace.store.get(state.task_ref(str(made["gid"])))
    assert stored is not None and stored.parent == CATERING


async def test_gids_come_from_the_event_sequence(workspace: Workspace, client: httpx.AsyncClient) -> None:
    head = workspace.store.head()
    made = await create(client, name="Label boxes", workspace=WS)
    assert made["gid"] == str(1_200_000_000_000_000 + head + 1)


async def test_a_list_is_compact_and_opt_fields_narrows_it(client: httpx.AsyncClient) -> None:
    compact = items(await client.get(f"/projects/{VENUE}/tasks"))
    asked = items(await client.get(f"/projects/{VENUE}/tasks",
                                   params={"opt_fields": "name,completed,assignee.email,memberships.section.name"}))

    assert set(compact[0]) == {"gid", "resource_type", "name", "resource_subtype"}
    assert asked[1] == {
        "gid": asked[1]["gid"], "name": "Return the old keys", "completed": True,
        "assignee": {"gid": state.user_gid("noor"), "email": "noor@example.com"},
        "memberships": [{"section": {"gid": state.section_gid(VENUE, state.wire.SectionRole.DONE), "name": "Done"}}],
    }


async def test_users_carry_no_email_until_it_is_asked_for(client: httpx.AsyncClient) -> None:
    compact = items(await client.get("/users", params={"workspace": WS}))
    asked = items(await client.get(f"/workspaces/{WS}/users", params={"opt_fields": "email"}))
    assert all("email" not in u for u in compact)
    assert sorted(str(u["email"]) for u in asked) == sorted(
        ["agent@workspace.example", "iris@example.com", "noor@example.com", "tomas@example.com"])


async def test_me_is_the_agent_and_a_user_is_found_by_email(client: httpx.AsyncClient) -> None:
    me = data(await client.get("/users/me"))
    by_email = data(await client.get("/users/tomas@example.com"))
    assert me["gid"] == state.AGENT_GID and me["email"] == "agent@workspace.example"
    assert by_email["gid"] == state.user_gid("tomas")


async def test_the_workspace_its_projects_and_their_sections(client: httpx.AsyncClient) -> None:
    workspaces = items(await client.get("/workspaces"))
    projects = items(await client.get(f"/workspaces/{WS}/projects"))
    also = items(await client.get("/projects", params={"workspace": WS}))
    one = data(await client.get(f"/projects/{VENUE}"))
    sections = items(await client.get(f"/projects/{VENUE}/sections"))

    assert [w["gid"] for w in workspaces] == [WS]
    assert sorted(str(p["name"]) for p in projects) == ["Catering", "Venue Move"] and also == projects
    assert one["name"] == "Venue Move" and one["workspace"] == {"gid": WS, "resource_type": "workspace",
                                                                "name": "Simulated Workspace"}
    assert sorted(str(s["name"]) for s in sections) == ["Cancelled", "Done", "To do"]


async def test_update_changes_only_what_was_sent(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Draft", notes="keep me", workspace=WS, due_on="2026-09-01")
    changed = data(await client.put(f"/tasks/{made['gid']}", json={"data": {"name": "Final"}}))
    timed = data(await client.put(f"/tasks/{made['gid']}", json={"data": {"due_at": "2026-09-02T15:00:00Z"}}))
    unassigned = data(await client.put(f"/tasks/{made['gid']}", json={"data": {"assignee": None}}))

    assert (changed["name"], changed["notes"], changed["due_on"]) == ("Final", "keep me", "2026-09-01")
    assert (timed["due_at"], timed["due_on"]) == ("2026-09-02T15:00:00.000Z", "2026-09-02")
    assert unassigned["assignee"] is None


async def test_a_deleted_task_is_gone_everywhere(workspace: Workspace, client: httpx.AsyncClient) -> None:
    made = await create(client, name="Mistake", projects=[VENUE])
    assert body(await client.delete(f"/tasks/{made['gid']}")) == {"data": {}}
    assert (await client.get(f"/tasks/{made['gid']}")).status_code == 404
    assert made["gid"] not in [t["gid"] for t in items(await client.get(f"/projects/{VENUE}/tasks"))]
    last = workspace.store.events()[-2]
    assert (last.operation, last.actor, last.entity.external_id) == (Operation.DELETE, Actor.AGENT, made["gid"])


async def test_a_comment_is_a_story_on_the_task(workspace: Workspace, client: httpx.AsyncClient) -> None:
    made = await create(client, name="Ask about parking", projects=[VENUE])
    story = data(await client.post(f"/tasks/{made['gid']}/stories", json={"data": {"text": "Any news?"}}), 201)
    listed = items(await client.get(f"/tasks/{made['gid']}/stories"))

    assert story["type"] == "comment" and story["text"] == "Any news?"
    assert story["created_at"] == "2026-08-24T10:50:03.250Z"
    assert [s["gid"] for s in listed] == [story["gid"]] and listed[0]["text"] == "Any news?"
    event = next(e for e in workspace.store.events() if e.entity.external_id == story["gid"])
    assert (event.entity.kind, event.actor, event.operation) == (EntityKind.COMMENT, Actor.AGENT, Operation.CREATE)
    assert event.after == MessageSnapshot(text="Any news?", channel=str(made["gid"]), thread_of=str(made["gid"]))


async def test_reads_and_searches_are_recorded_as_the_agents(workspace: Workspace, client: httpx.AsyncClient) -> None:
    before = workspace.store.head()
    task = items(await client.get(f"/projects/{VENUE}/tasks"))[0]
    await client.get(f"/tasks/{task['gid']}")
    await client.get(f"/workspaces/{WS}/tasks/search", params={"text": "x"})
    seen = workspace.store.events(since=before)
    assert [(e.operation, e.entity.external_id) for e in seen] == [
        (Operation.SEARCH, VENUE), (Operation.READ, task["gid"]), (Operation.SEARCH, WS),
    ]
    assert {e.actor for e in seen} == {Actor.AGENT} and all(e.after is None for e in seen)


async def test_tasks_list_by_assignee_and_workspace(client: httpx.AsyncClient) -> None:
    await create(client, name="Second for Tomas", workspace=WS, assignee=state.user_gid("tomas"))
    mine = items(await client.get("/tasks", params={"assignee": "tomas@example.com", "workspace": WS}))
    open_only = items(await client.get("/tasks", params={"project": VENUE, "completed_since": "now"}))
    assert [t["name"] for t in mine] == ["Book the freight lift", "Second for Tomas"]
    assert [t["name"] for t in open_only] == ["Book the freight lift"]


async def test_search_filters_by_assignee_completion_and_project(client: httpx.AsyncClient) -> None:
    search = f"/workspaces/{WS}/tasks/search"
    noor = items(await client.get(search, params={"assignee.any": state.user_gid("noor")}))
    done = items(await client.get(search, params={"completed": "true"}))
    catering = items(await client.get(search, params={"projects.any": CATERING, "completed": "false"}))
    assert [t["name"] for t in noor] == ["Return the old keys"]
    assert [t["name"] for t in done] == ["Return the old keys"]
    assert [t["name"] for t in catering] == ["Confirm the menu"]
    assert "next_page" not in body(await client.get(search))


async def test_typeahead_finds_tasks_users_and_projects(client: httpx.AsyncClient) -> None:
    at = f"/workspaces/{WS}/typeahead"
    tasks = items(await client.get(at, params={"resource_type": "task", "query": "menu"}))
    users = items(await client.get(at, params={"resource_type": "user", "query": "noor"}))
    projects = items(await client.get(at, params={"resource_type": "project", "query": "venue"}))
    assert [t["name"] for t in tasks] == ["Confirm the menu"]
    assert [u["gid"] for u in users] == [state.user_gid("noor")]
    assert [p["gid"] for p in projects] == [VENUE]


async def test_pages_follow_next_page_to_the_end(client: httpx.AsyncClient) -> None:
    for n in range(4):
        await create(client, name=f"extra {n}", projects=[VENUE])
    names: list[object] = []
    params = {"limit": "2"}
    pages = 0
    while pages < 10:
        answer = body(await client.get(f"/projects/{VENUE}/tasks", params=params))
        page = answer["data"]
        assert isinstance(page, list) and len(page) <= 2
        names += [t["name"] for t in page]
        pages += 1
        following = answer["next_page"]
        if following is None:
            break
        assert isinstance(following, dict) and following["path"].startswith(f"/projects/{VENUE}/tasks?")
        assert following["uri"].startswith("https://app.asana.com/api/1.0/")
        params = {"limit": "2", "offset": following["offset"]}
    assert pages == 3
    assert names == ["Book the freight lift", "Return the old keys", "extra 0", "extra 1", "extra 2", "extra 3"]


async def test_an_unpaginated_list_has_a_null_next_page(client: httpx.AsyncClient) -> None:
    assert body(await client.get(f"/projects/{VENUE}/tasks"))["next_page"] is None
