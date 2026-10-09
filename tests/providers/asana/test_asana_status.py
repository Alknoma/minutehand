"""A task's state for the scenario's checks comes from whichever fact the scenario names: the completed box,
the section, or a status custom field. People move tasks by that source, and act on seeded tasks by
themselves at moments the scenario sets."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.provider import build
from minutehand.adapters.providers.asana.seed import AsanaSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.people import move
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import ProviderSeed, Scenario, TicketHappening, TicketState
from minutehand.domain.world import Actor, EntityKind, Operation, TicketSnapshot
from tests.providers.asana.asana_workspace import SCENARIO as PLAIN
from tests.providers.asana.asana_workspace import VENUE, Workspace, client, create, workspace
from tests.providers.asana.rich_workspace import (
    INCIDENT,
    OLD,
    SCENARIO,
    STATUS_FIELD,
    agent,
    got,
    option,
    rich,
    section,
)
from tests.support.tickets import acted, assignee_moves, edited

__all__ = ["agent", "client", "rich", "workspace"]


def state_of(ws: Workspace, gid: str) -> TicketState:
    task = ws.asana.task(gid)
    assert task is not None
    return ws.asana.snapshot(task).state


def last_change(ws: Workspace, gid: str) -> tuple[Actor, TicketSnapshot]:
    found = [e for e in ws.store.events() if e.entity.external_id == gid and isinstance(e.after, TicketSnapshot)]
    after = found[-1].after
    assert isinstance(after, TicketSnapshot)
    return found[-1].actor, after


# ---------------------------------------------------------------------- the section (the default)


async def test_by_default_state_is_what_the_section_means_and_the_box_is_its_floor(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    made = await create(client, name="Order chairs", projects=[VENUE])
    gid = str(made["gid"])
    assert state_of(workspace, gid) is TicketState.OPEN
    await client.post(f"/sections/{state.section_gid(VENUE, 2)}/addTask", json={"data": {"task": gid}})
    assert state_of(workspace, gid) is TicketState.CANCELLED, "the Cancelled section says cancelled, ticked or not"
    await client.post(f"/sections/{state.section_gid(VENUE, 0)}/addTask", json={"data": {"task": gid}})
    await client.put(f"/tasks/{gid}", json={"data": {"completed": True}})
    assert state_of(workspace, gid) is TicketState.DONE, "a ticked task in To do is done: the box is the floor"


# ---------------------------------------------------------------------- the completed box


def scenario_with(seed: dict[str, object], base: Scenario = PLAIN) -> Scenario:
    body = AsanaSeed.model_validate(seed).model_dump_json()
    return base.model_copy(update={"provider_seeds": [ProviderSeed(provider="asana", body=body)]})


def seeded(tmp_path: Path, scenario: Scenario) -> Workspace:
    clock = RunClock(scenario.starts_at)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(scenario, store)
    return Workspace(provider=provider, store=store, clock=clock, path=tmp_path / "world.db")


def test_with_the_completed_box_as_the_source_a_section_says_nothing(tmp_path: Path) -> None:
    ws = seeded(tmp_path, scenario_with({"status": {"kind": "completed"}}))
    done = state.task_gid(1)
    assert state_of(ws, done) is TicketState.DONE
    task = ws.asana.task(done)
    assert task is not None
    moved = task.model_copy(
        update={"memberships": [state.wire.AsanaMembership(project=VENUE, section=state.section_gid(VENUE, 2))]}
    )
    assert ws.asana.snapshot(moved).state is TicketState.DONE
    reopened = task.model_copy(update={"completed": False})
    assert ws.asana.snapshot(reopened.model_copy(update={"memberships": moved.memberships})).state is TicketState.OPEN


async def test_with_the_completed_box_as_the_source_a_person_cannot_cancel(tmp_path: Path) -> None:
    ws = seeded(tmp_path, scenario_with({"status": {"kind": "completed"}}))
    ticket = state.task_ref(state.task_gid(0))
    # The completed box alone cannot say cancelled, so nothing that means it is offered.
    with pytest.raises(ValueError, match="offers no 'cancelled' now"):
        await assignee_moves(ws.provider, ticket, TicketState.CANCELLED, PLAIN, ws.store, ws.clock)
    await assignee_moves(ws.provider, ticket, TicketState.DONE, PLAIN, ws.store, ws.clock)
    assert last_change(ws, ticket.external_id) == (
        Actor.PERSON,
        TicketSnapshot(
            title="Book the freight lift",
            project="Venue Move",
            assignee_email="tomas@example.com",
            state=TicketState.DONE,
        ),
    )


# ---------------------------------------------------------------------- a status custom field


async def test_with_a_status_field_as_the_source_the_field_says_and_the_box_is_the_floor(
    rich: Workspace, agent: httpx.AsyncClient
) -> None:
    assert state_of(rich, INCIDENT) is TicketState.OPEN
    assert state_of(rich, OLD) is TicketState.DONE
    old = got(await agent.get(f"/tasks/{OLD}", params={"opt_fields": "completed,custom_fields.enum_value.name"}))
    assert old["completed"] is True and old["custom_fields"][0]["enum_value"]["name"] == "Done"
    await agent.put(
        f"/tasks/{INCIDENT}", json={"data": {"custom_fields": {STATUS_FIELD: option("Status", "Cancelled")}}}
    )
    assert state_of(rich, INCIDENT) is TicketState.CANCELLED
    await agent.put(
        f"/tasks/{INCIDENT}", json={"data": {"custom_fields": {STATUS_FIELD: option("Status", "In Review")}}}
    )
    assert state_of(rich, INCIDENT) is TicketState.OPEN, "an option the scenario gives no meaning reads as open"
    await agent.put(f"/tasks/{INCIDENT}", json={"data": {"completed": True}})
    read = got(await agent.get(f"/tasks/{INCIDENT}", params={"opt_fields": "completed,custom_fields.display_value"}))
    assert read["completed"] is True and read["custom_fields"][0]["display_value"] == "In Review", (
        "completing a task moves nothing else, as in Asana"
    )
    assert state_of(rich, INCIDENT) is TicketState.DONE


async def test_a_person_moves_a_task_by_the_status_field_and_ticks_it(rich: Workspace) -> None:
    ticket = state.task_ref(INCIDENT)
    await assignee_moves(rich.provider, ticket, TicketState.CANCELLED, SCENARIO, rich.store, rich.clock)
    task = rich.asana.task(INCIDENT)
    assert task is not None and task.completed
    assert [v.option for v in task.custom_fields if v.field == STATUS_FIELD] == [option("Status", "Cancelled")]
    assert [m.section for m in task.memberships] == [section("In Progress")], "the section is not the source"
    assert (
        last_change(rich, INCIDENT)[0] is Actor.PERSON and last_change(rich, INCIDENT)[1].state is TicketState.CANCELLED
    )
    await edited(
        rich.provider,
        ticket,
        state=TicketState.OPEN,
        assignee_email="alice.chen@company.com",
        world=rich.store,
        clock=rich.clock,
    )
    actor, after = last_change(rich, INCIDENT)
    assert (actor, after.state, after.assignee_email) == (Actor.SCENARIO, TicketState.OPEN, "alice.chen@company.com")


async def test_a_task_in_no_project_with_the_status_field_cannot_be_cancelled(rich: Workspace) -> None:
    bob = next(p for p in SCENARIO.people if p.key == "bob")
    with pytest.raises(ValueError, match="offers no 'cancelled' now"):
        await move(
            rich.provider, state.task_ref(state.task_gid(1)), "cancelled", Actor.PERSON, bob, {}, rich.store, rich.clock
        )


# ---------------------------------------------------------------------- people acting by themselves


def happening(person: str, ticket: str, action: Mapping[str, object]) -> TicketHappening:
    return TicketHappening.model_validate(
        {"person": person, "ticket": ticket, "after": timedelta(hours=5), "action": action}
    )


async def test_people_complete_reassign_comment_and_delete_seeded_tasks_as_themselves(
    rich: Workspace, agent: httpx.AsyncClient
) -> None:
    incident = "API timeout in production"
    rich.clock.jump(rich.clock.now() + timedelta(hours=5))
    for action in (
        {"kind": "moves", "to": "done"},
        {"kind": "reassigns", "to": "alice"},
        {"kind": "comments", "text": "Fixed by the pool change."},
    ):
        await acted(rich.provider, happening("bob", incident, action), SCENARIO, rich.store, rich.clock)
    read = got(
        await agent.get(
            f"/tasks/{INCIDENT}", params={"opt_fields": "completed,assignee.name,custom_fields.display_value"}
        )
    )
    assert (read["completed"], read["assignee"]["name"], read["custom_fields"][0]["display_value"]) == (
        True,
        "Alice Chen",
        "Done",
    )
    stories = got(
        await agent.get(f"/tasks/{INCIDENT}/stories", params={"opt_fields": "text,created_by.name,created_at"})
    )
    assert [(s["text"], s["created_by"]["name"]) for s in stories] == [
        ("Seen again at 9am.", "Alice Chen"),
        ("Fixed by the pool change.", "Bob Taylor"),
    ]
    assert stories[0]["created_at"] == "2026-08-24T08:50:03.250Z", "a seeded comment is stamped before the start"
    assert stories[1]["created_at"] == "2026-08-24T15:50:03.250Z"
    people = [e for e in rich.store.events() if e.actor is Actor.PERSON]
    assert [(e.entity.kind, e.operation) for e in people] == [
        (EntityKind.TICKET, Operation.UPDATE),
        (EntityKind.TRANSITION, Operation.CREATE),
        (EntityKind.TICKET, Operation.UPDATE),
        (EntityKind.TRANSITION, Operation.CREATE),
        (EntityKind.COMMENT, Operation.CREATE),
        (EntityKind.TRANSITION, Operation.CREATE),
    ], "each act is recorded once as a transition beside what it changed: the move, the reassignment, the comment"
    assert all(e.sim_time == rich.clock.now() for e in people)
    await acted(rich.provider, happening("bob", incident, {"kind": "deletes"}), SCENARIO, rich.store, rich.clock)
    assert (await agent.get(f"/tasks/{INCIDENT}")).status_code == 404
    deleted = [e for e in rich.store.events() if e.actor is Actor.PERSON and e.operation is Operation.DELETE]
    assert [e.entity.external_id for e in deleted] == [state.task_gid(1), INCIDENT], "its subtask goes with it"
    head = rich.store.head()
    await acted(
        rich.provider,
        happening("bob", incident, {"kind": "comments", "text": "too late"}),
        SCENARIO,
        rich.store,
        rich.clock,
    )
    assert rich.store.head() == head, "a task the agent or someone deleted is left alone"


# ---------------------------------------------------------------------- the seed refuses names that name nothing


@pytest.mark.parametrize(
    "seed, message",
    [
        ({"projects": [{"name": "Venue Move", "team": "Nobody"}]}, "the team 'Nobody', which it does not define"),
        ({"projects": [{"name": "Venue Move", "custom_fields": ["Nope"]}]}, "the custom field 'Nope'"),
        ({"tokens": [{"token": "t", "person": "ghost"}]}, "the person 'ghost', who is not in the scenario"),
        (
            {
                "custom_fields": [{"name": "Size", "kind": "text"}],
                "status": {"kind": "custom_field", "field": "Size", "means": {}},
            },
            "the status field 'Size' is text, not enum",
        ),
        ({"tasks": [{"ticket": "Book the freight lift", "tags": ["nope"]}]}, "with 'nope', which it does not define"),
        ({"tasks": [{"ticket": "Book the freight lift", "section": "Limbo"}]}, "has no section 'Limbo'"),
        ({"tasks": [{"ticket": "No such ticket"}]}, "0 asana tickets have that title"),
        (
            {"workspace": {"organization": False}, "teams": [{"name": "A"}]},
            "teams in a workspace that is not an organization",
        ),
    ],
)
def test_a_seed_that_names_nothing_is_refused_before_it_writes(
    tmp_path: Path, seed: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        seeded(tmp_path, scenario_with(seed))
