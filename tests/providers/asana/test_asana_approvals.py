"""Approval tasks (`resource_subtype: approval`, `approval_status`): kept in step with `completed`, as the reference
says (`TaskBase.approval_status`), and decided by the assigned person through the transitions port."""

from __future__ import annotations

import json

import httpx
import pytest

from minutehand.adapters.providers.asana import state
from minutehand.domain.transitions import Transition
from minutehand.domain.world import Actor, EntityKind, EntityRef
from tests.providers.asana.asana_workspace import (
    SCENARIO,
    VENUE,
    Workspace,
    client,
    create,
    items,
    unserved,
    workspace,
)
from tests.providers.asana.rich_workspace import got

__all__ = ["client", "workspace"]

TOMAS = SCENARIO.people[1]


async def approval(client: httpx.AsyncClient, **fields: object) -> dict[str, object]:
    return await create(client, name="Sign off the floor plan", projects=[VENUE], resource_subtype="approval", **fields)


def ref(gid: object) -> EntityRef:
    return EntityRef(provider="asana", kind=EntityKind.TICKET, external_id=str(gid))


async def test_an_approval_task_is_made_pending_and_not_completed(client: httpx.AsyncClient) -> None:
    made = await approval(client)
    read = got(await client.get(f"/tasks/{made['gid']}"))
    assert (read["resource_subtype"], read["approval_status"], read["completed"]) == ("approval", "pending", False)


async def test_a_task_that_is_no_approval_has_no_approval_status(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Book the lift", projects=[VENUE])
    read = got(await client.get(f"/tasks/{made['gid']}"))
    assert read["resource_subtype"] == "default_task"
    assert "approval_status" not in read


@pytest.mark.parametrize(
    ("status", "completed"),
    [("pending", False), ("approved", True), ("rejected", True), ("changes_requested", True)],
)
async def test_an_approval_status_sets_completed_with_it(
    client: httpx.AsyncClient, status: str, completed: bool
) -> None:
    made = await approval(client)
    sent = got(await client.put(f"/tasks/{made['gid']}", json={"data": {"approval_status": status}}))
    assert (sent["approval_status"], sent["completed"]) == (status, completed)
    assert (sent["completed_at"] is not None) is completed


async def test_completing_an_approval_approves_it_and_reopening_it_makes_it_pending(
    client: httpx.AsyncClient,
) -> None:
    made = await approval(client)
    approved = got(await client.put(f"/tasks/{made['gid']}", json={"data": {"completed": True}}))
    assert (approved["approval_status"], approved["completed"]) == ("approved", True)
    reopened = got(await client.put(f"/tasks/{made['gid']}", json={"data": {"completed": False}}))
    assert (reopened["approval_status"], reopened["completed"]) == ("pending", False)


async def test_an_approval_made_with_a_status_is_made_completed_by_it(client: httpx.AsyncClient) -> None:
    made = await approval(client, approval_status="rejected")
    assert (made["approval_status"], made["completed"]) == ("rejected", True)


async def test_an_approval_status_on_a_task_that_is_no_approval_is_refused_by_name(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Plain", projects=[VENUE])
    answered = await client.put(f"/tasks/{made['gid']}", json={"data": {"approval_status": "approved"}})
    assert "approval_status" in unserved(answered)


async def test_an_approval_status_that_contradicts_completed_is_refused_by_name(client: httpx.AsyncClient) -> None:
    made = await approval(client)
    answered = await client.put(
        f"/tasks/{made['gid']}", json={"data": {"approval_status": "pending", "completed": True}}
    )
    assert "contradicting" in unserved(answered)


@pytest.mark.parametrize("subtype", ["milestone", "custom"])
async def test_a_subtype_the_fake_does_not_serve_is_refused_by_name(client: httpx.AsyncClient, subtype: str) -> None:
    answered = await client.post(
        "/tasks", json={"data": {"name": "x", "projects": [VENUE], "resource_subtype": subtype}}
    )
    assert unserved(answered) == f"resource_subtype `{subtype}`"


async def test_a_subtype_cannot_be_rewritten_on_an_update_it_is_refused_by_name(client: httpx.AsyncClient) -> None:
    made = await create(client, name="Plain", projects=[VENUE])
    answered = await client.put(f"/tasks/{made['gid']}", json={"data": {"resource_subtype": "approval"}})
    assert "resource_subtype written on a task update" in unserved(answered)


async def test_an_approval_waits_on_its_assignee_who_decides_it_through_the_transitions_port(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    made = await approval(client, assignee="tomas@example.com")
    waiting = [w for w in workspace.provider.items_for(TOMAS, workspace.store) if w.item.external_id == made["gid"]]
    assert [w.state for w in waiting] == ["pending"]
    offers = workspace.provider.legal(ref(made["gid"]), Actor.PERSON, TOMAS, workspace.store)
    assert [o.name for o in offers] == ["approved", "rejected", "changes_requested", "comment", "reassign", "delete"]
    done = await workspace.provider.apply(
        ref(made["gid"]),
        "changes_requested",
        Actor.PERSON,
        TOMAS,
        json.dumps({"comment": "Move the stage left."}),
        workspace.store,
        workspace.clock,
    )
    assert isinstance(done, Transition)
    assert (done.name, done.from_state, done.to_state, done.by, done.who) == (
        "changes_requested",
        "pending",
        "changes_requested",
        Actor.PERSON,
        "tomas",
    )
    read = got(await client.get(f"/tasks/{made['gid']}"))
    assert (read["approval_status"], read["completed"]) == ("changes_requested", True)
    stories = items(await client.get(f"/tasks/{made['gid']}/stories", params={"opt_fields": "text,created_by.name"}))
    assert [(s["text"], s["created_by"]) for s in stories] == [
        ("Move the stage left.", {"gid": state.user_gid("tomas"), "name": "Tomas Brandt"})
    ]
    assert not [w for w in workspace.provider.items_for(TOMAS, workspace.store) if w.item.external_id == made["gid"]]


async def test_a_decision_the_approval_already_holds_is_refused_as_no_longer_legal(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    made = await approval(client, approval_status="approved")
    offers = workspace.provider.legal(ref(made["gid"]), Actor.PERSON, TOMAS, workspace.store)
    assert [o.name for o in offers] == ["rejected", "changes_requested", "comment", "reassign", "delete"]
    with pytest.raises(ValueError, match="offers no decision"):
        await workspace.provider.apply(
            ref(made["gid"]), "approved", Actor.PERSON, TOMAS, "{}", workspace.store, workspace.clock
        )
