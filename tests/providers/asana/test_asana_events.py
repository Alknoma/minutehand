"""The Events API (https://developers.asana.com/reference/getevents, https://developers.asana.com/docs/events): a
sync token, the events on a task or project since it, and the shapes `EventResponse` describes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from minutehand.adapters.providers.asana import state
from minutehand.application.people import happen
from minutehand.domain.scenario import TicketHappening
from tests.providers.asana.asana_workspace import (
    CATERING,
    SCENARIO,
    VENUE,
    Workspace,
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


async def token(client: httpx.AsyncClient, resource: str) -> str:
    """The first call, with no token, is answered 412 with the token to start from."""
    answered = await client.get("/events", params={"resource": resource})
    assert answered.status_code == 412
    found = answered.json()["sync"]
    assert isinstance(found, str)
    return found


async def poll(client: httpx.AsyncClient, resource: str, sync: str, **params: str) -> tuple[list[Any], str, bool]:
    answered = await client.get("/events", params={"resource": resource, "sync": sync, **params})
    assert answered.status_code == 200, answered.text
    found = answered.json()
    return found["data"], found["sync"], found["has_more"]


async def test_a_call_with_no_sync_token_is_answered_412_with_one(client: httpx.AsyncClient) -> None:
    answered = await client.get("/events", params={"resource": VENUE})
    assert answered.status_code == 412
    found = answered.json()
    assert len(found["sync"]) == 32 and found["errors"][0]["message"].startswith("Sync token invalid or too old")


async def test_a_token_that_is_not_one_is_answered_412_with_a_fresh_one(client: httpx.AsyncClient) -> None:
    answered = await client.get("/events", params={"resource": VENUE, "sync": "not-a-token"})
    assert answered.status_code == 412 and len(answered.json()["sync"]) == 32


async def test_a_token_over_a_day_old_is_expired_and_answered_412(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    sync = await token(client, VENUE)
    workspace.clock.jump(workspace.clock.now() + timedelta(hours=23))
    assert (await client.get("/events", params={"resource": VENUE, "sync": sync})).status_code == 200
    workspace.clock.jump(workspace.clock.now() + timedelta(hours=2))
    assert (await client.get("/events", params={"resource": VENUE, "sync": sync})).status_code == 412


async def test_the_resource_is_required_and_a_workspace_or_unknown_one_is_refused(client: httpx.AsyncClient) -> None:
    assert error(await client.get("/events"), 400) == "resource: Missing input"
    assert "a resource that is a workspace or a team" in unserved(
        await client.get("/events", params={"resource": state.WORKSPACE_GID, "sync": "x"})
    )
    assert error(await client.get("/events", params={"resource": "9999"}), 400) == "resource: Unknown object: 9999"


async def test_nothing_before_the_token_is_reported_and_each_call_resumes_where_the_last_ended(
    client: httpx.AsyncClient,
) -> None:
    await create(client, name="Before", projects=[VENUE])
    sync = await token(client, VENUE)
    assert (await poll(client, VENUE, sync))[0] == []
    made = await create(client, name="After", projects=[VENUE])
    found, sync, more = await poll(client, VENUE, sync)
    assert [(e["action"], e["resource"]["gid"]) for e in found] == [("added", made["gid"])] and not more
    again, _, _ = await poll(client, VENUE, sync)
    assert again == []


async def test_a_task_put_in_a_project_is_added_there_with_the_project_as_parent(client: httpx.AsyncClient) -> None:
    sync = await token(client, VENUE)
    made = await create(client, name="Pour", projects=[VENUE])
    found, _, _ = await poll(client, VENUE, sync)
    assert found == [
        {
            "user": {"gid": state.AGENT_GID, "resource_type": "user", "name": "Agent"},
            "created_at": found[0]["created_at"],
            "type": "task",
            "action": "added",
            "resource": {
                "gid": made["gid"],
                "resource_type": "task",
                "resource_subtype": "default_task",
                "name": "Pour",
            },
            "parent": {"gid": VENUE, "resource_type": "project"},
        }
    ]


async def test_an_assignee_set_is_changed_with_the_user_as_new_value_and_a_follower_added_is_changed_with_added_value(
    client: httpx.AsyncClient,
) -> None:
    made = await create(client, name="Pour", projects=[VENUE])
    sync = await token(client, str(made["gid"]))
    await client.put(f"/tasks/{made['gid']}", json={"data": {"assignee": TOMAS}})
    await client.post(f"/tasks/{made['gid']}/addFollowers", json={"data": {"followers": [IRIS]}})
    await client.put(f"/tasks/{made['gid']}", json={"data": {"completed": True}})
    found, _, _ = await poll(client, str(made["gid"]), sync)
    assert [(e["action"], e["parent"], e["change"]) for e in found] == [
        (
            "changed",
            None,
            {"field": "assignee", "action": "changed", "new_value": {"gid": TOMAS, "resource_type": "user"}},
        ),
        (
            "changed",
            None,
            {"field": "followers", "action": "added", "added_value": {"gid": IRIS, "resource_type": "user"}},
        ),
        ("changed", None, {"field": "completed", "action": "changed"}),
    ]


async def test_a_follower_removed_is_changed_with_removed_value(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pour", projects=[VENUE])
    await client.post(f"/tasks/{made['gid']}/addFollowers", json={"data": {"followers": [IRIS]}})
    sync = await token(client, str(made["gid"]))
    await client.post(f"/tasks/{made['gid']}/removeFollowers", json={"data": {"followers": [IRIS]}})
    found, _, _ = await poll(client, str(made["gid"]), sync)
    assert [e["change"] for e in found] == [
        {"field": "followers", "action": "removed", "removed_value": {"gid": IRIS, "resource_type": "user"}}
    ]


async def test_a_subscription_to_a_project_hears_its_tasks_and_their_subtasks_and_one_to_a_task_only_its_own(
    client: httpx.AsyncClient,
) -> None:
    mine = await create(client, name="Mine", projects=[VENUE])
    other = await create(client, name="Other", projects=[VENUE])
    elsewhere = await create(client, name="Elsewhere", projects=[CATERING])
    project, task = await token(client, VENUE), await token(client, str(mine["gid"]))
    sub = got(await client.post(f"/tasks/{mine['gid']}/subtasks", json={"data": {"name": "Part"}}), 201)
    await client.put(f"/tasks/{sub['gid']}", json={"data": {"name": "Part 2"}})
    await client.put(f"/tasks/{other['gid']}", json={"data": {"name": "Other 2"}})
    await client.put(f"/tasks/{elsewhere['gid']}", json={"data": {"name": "Elsewhere 2"}})
    heard = [(e["resource"]["gid"], e["action"]) for e in (await poll(client, VENUE, project))[0]]
    assert heard == [(sub["gid"], "added"), (sub["gid"], "changed"), (other["gid"], "changed")]
    own = [(e["resource"]["gid"], e["action"]) for e in (await poll(client, str(mine["gid"]), task))[0]]
    assert own == [(sub["gid"], "added"), (sub["gid"], "changed")]


async def test_a_comment_is_a_story_added_to_its_task_and_an_edit_and_a_delete_follow(
    client: httpx.AsyncClient,
) -> None:
    made = await create(client, name="Pour", projects=[VENUE])
    sync = await token(client, str(made["gid"]))
    story = got(await client.post(f"/tasks/{made['gid']}/stories", json={"data": {"text": "One"}}), 201)
    await client.put(f"/stories/{story['gid']}", json={"data": {"text": "Two"}})
    await client.delete(f"/stories/{story['gid']}")
    found, _, _ = await poll(client, str(made["gid"]), sync)
    assert [(e["action"], e["resource"]["resource_type"], e["resource"]["resource_subtype"]) for e in found] == [
        ("added", "story", "comment_added"),
        ("changed", "story", "comment_added"),
        ("deleted", "story", "comment_added"),
    ]
    assert found[0]["parent"] == {
        "gid": made["gid"],
        "resource_type": "task",
        "resource_subtype": "default_task",
        "name": "Pour",
    }
    assert found[1]["change"] == {"field": "text", "action": "changed"}


async def test_a_task_deleted_and_taken_out_of_a_project_are_reported(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pour", projects=[VENUE, CATERING])
    sync = await token(client, CATERING)
    await client.post(f"/tasks/{made['gid']}/removeProject", json={"data": {"project": CATERING}})
    await client.post(f"/tasks/{made['gid']}/addProject", json={"data": {"project": CATERING}})
    await client.delete(f"/tasks/{made['gid']}")
    found, _, _ = await poll(client, CATERING, sync)
    assert [(e["action"], e["parent"] and e["parent"]["gid"]) for e in found] == [
        ("removed", CATERING),
        ("added", CATERING),
        ("deleted", None),
    ]


async def test_at_most_a_hundred_events_come_at_once_and_has_more_says_there_are_others(
    client: httpx.AsyncClient,
) -> None:
    made = await create(client, name="Pour", projects=[VENUE])
    sync = await token(client, str(made["gid"]))
    for n in range(101):
        await client.put(f"/tasks/{made['gid']}", json={"data": {"name": f"Pour {n}"}})
    first, sync, more = await poll(client, str(made["gid"]), sync)
    assert (len(first), more) == (100, True)
    second, sync, more = await poll(client, str(made["gid"]), sync)
    assert (len(second), more) == (1, False)
    assert (await poll(client, str(made["gid"]), sync))[0] == []


async def test_opt_fields_narrows_each_event_to_what_it_names(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Pour", projects=[VENUE])
    sync = await token(client, str(made["gid"]))
    await client.put(f"/tasks/{made['gid']}", json={"data": {"name": "Pour 2"}})
    found, _, _ = await poll(client, str(made["gid"]), sync, opt_fields="action,resource.name,change.field,user.name")
    assert found == [
        {
            "action": "changed",
            "resource": {"gid": made["gid"], "name": "Pour 2"},
            "change": {"field": "name"},
            "user": {"gid": state.AGENT_GID, "name": "Agent"},
        }
    ]


async def test_a_person_acting_as_themselves_is_the_events_user(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    task = next(t for t in workspace.asana.tasks() if t.name == "Book the freight lift")
    sync = await token(client, task.gid)
    happening = TicketHappening.model_validate(
        {
            "person": "tomas",
            "ticket": "Book the freight lift",
            "after": timedelta(hours=5),
            "action": {"kind": "comments", "text": "On it."},
        }
    )
    await happen(SCENARIO, workspace.provider, happening, workspace.store, workspace.clock)
    found, _, _ = await poll(client, task.gid, sync)
    assert [(e["action"], e["user"]["gid"]) for e in found] == [("added", TOMAS)]


async def test_seeding_is_not_an_event(workspace: Workspace, client: httpx.AsyncClient) -> None:
    assert not list(workspace.asana.events_after("0"))
    assert items(await client.get(f"/projects/{VENUE}/tasks"))
