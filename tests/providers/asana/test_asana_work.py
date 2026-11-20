"""A task assigned to a person is work they carry (`ports.transitions.CarriesWork`, docs/design-transitions.md phase
5), read as Asana holds it: due on its `due_on`, with no priority of Asana's own, held back by each task it depends
on until that one is completed, and with no estimate this fake holds. Asana knows a task only as completed or not:
nothing of its own means started, and a section the scenario says is started never ticks the box."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx

from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.provider import build
from minutehand.adapters.providers.asana.seed import AsanaSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import ProviderSeed, TicketState
from minutehand.domain.world import Actor, EntityRef
from tests.providers.asana.asana_workspace import SCENARIO, START, VENUE, Workspace, client_for, create, data

TOMAS = SCENARIO.people[1]
LIFT, KEYS, MENU = "Book the freight lift", "Return the old keys", "Confirm the menu"


def _task(workspace: Workspace, name: str) -> EntityRef:
    return state.task_ref(next(t.gid for t in workspace.asana.tasks() if t.name == name))


async def test_a_task_assigned_and_not_completed_is_work_due_on_its_due_on_with_no_rank(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    lift = _task(workspace, LIFT)
    data(await client.put(f"/tasks/{lift.external_id}", json={"data": {"due_on": "2026-08-28"}}))
    approval = await create(
        client, name="Sign off the floor plan", projects=[VENUE], resource_subtype="approval", assignee=TOMAS.email
    )

    waiting = workspace.provider.items_for(TOMAS, workspace.store)

    assert [(w.item, w.work, w.due, w.rank) for w in waiting] == [
        (lift, True, date(2026, 8, 28), None),
        (state.task_ref(str(approval["gid"])), False, None, None),
    ], "a task is work he carries; an approval is a decision asked of him"


def test_a_task_offers_no_started_move_where_no_section_says_one(workspace: Workspace) -> None:
    offers = workspace.provider.legal(_task(workspace, LIFT), Actor.PERSON, TOMAS, workspace.store)

    assert [o.means for o in offers if o.unprompted] == [TicketState.DONE, TicketState.CANCELLED]


async def test_a_task_is_held_by_a_task_it_depends_on_until_that_one_is_completed(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    lift, keys, menu = (_task(workspace, name) for name in (LIFT, KEYS, MENU))
    sent = {"data": {"dependencies": [menu.external_id, keys.external_id]}}
    data(await client.post(f"/tasks/{lift.external_id}/addDependencies", json=sent))

    assert workspace.provider.holding(lift, workspace.store) == [menu], "the old keys are returned already"
    assert workspace.provider.holding(menu, workspace.store) == [], "the lift waits on the menu, not the other way"

    data(await client.put(f"/tasks/{menu.external_id}", json={"data": {"completed": True}}))

    assert workspace.provider.holding(lift, workspace.store) == [], "the menu is confirmed: nothing holds the lift"


def test_a_task_holds_no_estimate_here(workspace: Workspace) -> None:
    assert workspace.provider.estimate(_task(workspace, LIFT), workspace.store) is None


async def test_a_section_the_scenario_says_is_started_holds_unfinished_work_and_never_ticks_the_box(
    tmp_path: Path,
) -> None:
    sections = [
        {"name": "To do", "means": "open"},
        {"name": "Doing", "means": "started"},
        {"name": "Done", "means": "done"},
    ]
    seed = AsanaSeed.model_validate({"projects": [{"name": "Venue Move", "sections": sections}]})
    scenario = SCENARIO.model_copy(
        update={"provider_seeds": [ProviderSeed(provider="asana", body=seed.model_dump_json())]}
    )
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "w", clock)
    provider = build()
    provider.seed(scenario, store)
    workspace = Workspace(provider=provider, store=store, clock=clock, path=tmp_path / "w.db")
    lift = _task(workspace, LIFT)

    offers = provider.legal(lift, Actor.PERSON, TOMAS, store)
    assert [(o.name, o.means) for o in offers if o.unprompted] == [
        ("Doing", TicketState.STARTED),
        ("Done (completed)", TicketState.DONE),
    ]
    await provider.apply(lift, "Doing", Actor.PERSON, TOMAS, "{}", store, clock)
    task = workspace.asana.task(lift.external_id)
    assert task is not None and not task.completed, "started is not finished: the box stays clear"
    assert workspace.asana.state_of(task) is TicketState.STARTED
    [waiting] = provider.items_for(TOMAS, store)
    assert (waiting.state, waiting.work) == ("Doing", True)

    async with client_for(provider, store, clock) as client:
        data(await client.put(f"/tasks/{lift.external_id}", json={"data": {"completed": True}}))
    ticked = workspace.asana.task(lift.external_id)
    assert ticked is not None and workspace.asana.state_of(ticked) is TicketState.DONE, "ticked in Doing: finished"
    assert provider.items_for(TOMAS, store) == []
    store.close()
