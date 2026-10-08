"""The workspace lives in the store and nowhere else; people and the scenario move tickets through the ports."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.provider import build
from minutehand.adapters.providers.asana.seed import seeded_gid
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.refusals import RunRefused
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import AgentUnderTest, GoalByWake, Reported
from minutehand.domain.provider import Tier
from minutehand.domain.scenario import SeededComment, SeededTicket, TicketState
from minutehand.domain.world import Actor, EntityKind, Operation, TicketSnapshot
from minutehand.ports.provider import ActsOnTickets, EditsTickets, HoldsTickets, Provider
from minutehand.session import _services  # pyright: ignore[reportPrivateUsage]
from tests.providers.asana.asana_workspace import (
    CATERING,
    SCENARIO,
    START,
    VENUE,
    WS,
    Workspace,
    client_for,
    create,
    data,
    items,
)


def _names(listed: list[dict[str, object]]) -> list[object]:
    return [t["name"] for t in listed]


async def test_the_workspace_survives_a_new_app_over_a_new_connection(
    workspace: Workspace, client: httpx.AsyncClient
) -> None:
    made = await create(client, name="Written by the first app", projects=[CATERING], assignee="noor@example.com")
    await client.post(f"/tasks/{made['gid']}/stories", json={"data": {"text": "noted"}})

    reopened = SqliteStore(workspace.path, "root", RunClock(START))
    async with client_for(build(), reopened, RunClock(START)) as second:
        read = data(await second.get(f"/tasks/{made['gid']}", params={"opt_fields": "name,assignee.email"}))
        stories = items(await second.get(f"/tasks/{made['gid']}/stories"))

    assert read == {
        "gid": made["gid"],
        "name": "Written by the first app",
        "assignee": {"gid": state.user_gid("noor"), "email": "noor@example.com"},
    }
    assert [s["text"] for s in stories] == ["noted"]


_READ_IN_A_NEW_PROCESS = """
import asyncio, json, sys
from pathlib import Path
import httpx
from minutehand.adapters.providers.asana.provider import build
from minutehand.adapters.providers.asana.seed import seeded_gid
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from tests.providers.asana.asana_workspace import AUTH, START

async def main() -> None:
    store = SqliteStore(Path(sys.argv[1]), "root", RunClock(START))
    app = build().app(store, RunClock(START))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.asana.com",
                                 headers=AUTH) as client:
        read = await client.get(f"/tasks/{sys.argv[2]}", params={"opt_fields": "name"})
    print(json.dumps(read.json()))

asyncio.run(main())
"""


async def test_the_workspace_survives_a_new_process(workspace: Workspace, client: httpx.AsyncClient) -> None:
    """Nothing the first app holds in memory, at any scope, reaches the second."""
    made = await create(client, name="Written before the restart", workspace=WS)
    read = subprocess.run(
        [sys.executable, "-c", _READ_IN_A_NEW_PROCESS, str(workspace.path), str(made["gid"])],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(read.stdout) == {"data": {"gid": made["gid"], "name": "Written before the restart"}}


async def test_a_fork_sees_tasks_only_up_to_the_fork(workspace: Workspace, client: httpx.AsyncClient) -> None:
    await create(client, name="before the fork", projects=[CATERING])
    at = workspace.store.head()
    await create(client, name="after, in the parent", projects=[CATERING])

    fork = workspace.store.fork("what-if", at_seq=at, clock=RunClock(START))
    async with client_for(workspace.provider, fork, workspace.clock) as forked:
        assert _names(items(await forked.get(f"/projects/{CATERING}/tasks"))) == ["Confirm the menu", "before the fork"]
        await create(forked, name="only in the fork", projects=[CATERING])
        assert _names(items(await forked.get(f"/projects/{CATERING}/tasks"))) == [
            "Confirm the menu",
            "before the fork",
            "only in the fork",
        ]

    assert _names(items(await client.get(f"/projects/{CATERING}/tasks"))) == [
        "Confirm the menu",
        "before the fork",
        "after, in the parent",
    ]


def test_seeding_writes_the_workspace_people_projects_and_asana_tickets_as_the_scenario(workspace: Workspace) -> None:
    events = workspace.store.events()
    assert events and {e.actor for e in events} == {Actor.SCENARIO}

    asana = workspace.asana
    assert [w.gid for w in asana.workspaces()] == [WS]
    assert sorted(u.gid for u in asana.users()) == sorted(
        [state.AGENT_GID, *(state.user_gid(p.key) for p in SCENARIO.people)]
    )
    assert sorted(p.name for p in asana.projects()) == ["Catering", "Venue Move"]
    snapshots = [e.after for e in events if e.entity.kind is EntityKind.TICKET]
    assert snapshots == [
        TicketSnapshot(title="Book the freight lift", project="Venue Move", assignee_email="tomas@example.com"),
        TicketSnapshot(
            title="Return the old keys", project="Venue Move", assignee_email="noor@example.com", state=TicketState.DONE
        ),
        TicketSnapshot(title="Confirm the menu", body="Vegetarian count first.", project="Catering"),
    ]
    stored = [workspace.store.get(e.entity) for e in events if e.entity.kind is EntityKind.TICKET]
    assert [s.parent for s in stored if s is not None] == [VENUE, VENUE, CATERING]


def test_seeded_gids_are_derived_from_the_scenarios_keys() -> None:
    assert state.user_gid("iris") == state.user_gid("iris") != state.user_gid("tomas")
    assert state.project_gid("Catering") != state.project_gid("Venue Move")
    assert all(len(g) == 16 and g.isdigit() for g in (state.user_gid("iris"), VENUE, WS, state.AGENT_GID))


async def test_a_person_completes_a_task_the_agent_handed_them(workspace: Workspace, client: httpx.AsyncClient) -> None:
    made = await create(client, name="Measure the hall", projects=[VENUE], assignee="tomas@example.com")
    ticket = state.task_ref(str(made["gid"]))
    workspace.clock.jump(START + timedelta(hours=5))
    workspace.provider.transition(ticket, TicketState.DONE, workspace.store, workspace.clock)

    last = [e for e in workspace.store.events() if e.entity.kind is EntityKind.TICKET][-1]
    assert (last.actor, last.operation, last.entity) == (Actor.PERSON, Operation.UPDATE, ticket)
    assert isinstance(last.after, TicketSnapshot) and last.after.state is TicketState.DONE
    read = data(await client.get(f"/tasks/{made['gid']}"))
    assert read["completed"] is True and read["completed_at"] == "2026-08-24T15:50:03.250Z"
    memberships = read["memberships"]
    assert isinstance(memberships, list) and memberships[0]["section"]["name"] == "Done"


async def test_a_person_cancels_a_task(workspace: Workspace, client: httpx.AsyncClient) -> None:
    made = await create(client, name="Hire a piano", projects=[VENUE], assignee="noor@example.com")
    workspace.provider.transition(
        state.task_ref(str(made["gid"])), TicketState.CANCELLED, workspace.store, workspace.clock
    )
    last = [e for e in workspace.store.events() if e.entity.kind is EntityKind.TICKET][-1]
    assert isinstance(last.after, TicketSnapshot) and last.after.state is TicketState.CANCELLED
    read = data(await client.get(f"/tasks/{made['gid']}", params={"opt_fields": "completed,memberships.section.name"}))
    assert read["completed"] is True
    assert read["memberships"] == [{"section": {"gid": state.section_gid(VENUE, 2), "name": "Cancelled"}}]


async def test_the_scenario_edits_state_and_assignee(workspace: Workspace, client: httpx.AsyncClient) -> None:
    seeded = items(await client.get(f"/projects/{VENUE}/tasks"))[1]  # Return the old keys, done, with noor
    ticket = state.task_ref(str(seeded["gid"]))
    workspace.provider.edit(
        ticket, state=TicketState.OPEN, assignee_email="iris@example.com", world=workspace.store, clock=workspace.clock
    )

    last = [e for e in workspace.store.events() if e.entity.kind is EntityKind.TICKET][-1]
    assert (last.actor, last.operation) == (Actor.SCENARIO, Operation.UPDATE)
    assert last.after == TicketSnapshot(
        title="Return the old keys", project="Venue Move", assignee_email="iris@example.com", state=TicketState.OPEN
    )
    read = data(await client.get(f"/tasks/{seeded['gid']}", params={"opt_fields": "completed,completed_at,assignee"}))
    assert read == {
        "gid": seeded["gid"],
        "completed": False,
        "completed_at": None,
        "assignee": {"gid": state.user_gid("iris"), "resource_type": "user"},
    }


def test_editing_to_an_email_nobody_has_is_refused(workspace: Workspace) -> None:
    ticket = state.task_ref(workspace.asana.tasks()[0].gid)
    with pytest.raises(LookupError):
        workspace.provider.edit(
            ticket, state=None, assignee_email="stranger@example.com", world=workspace.store, clock=workspace.clock
        )


def test_moving_a_task_that_is_not_there_is_refused(workspace: Workspace) -> None:
    with pytest.raises(LookupError):
        workspace.provider.transition(
            state.task_ref("1999999999999999"), TicketState.DONE, workspace.store, workspace.clock
        )


def test_the_provider_holds_every_port_it_claims() -> None:
    provider = build()
    held: tuple[Provider, HoldsTickets, EditsTickets, ActsOnTickets] = (provider, provider, provider, provider)
    assert all(p is provider for p in held)
    assert isinstance(provider, ActsOnTickets), "the run finds the port by isinstance, so it must be checkable"


def test_the_manifest_claims_asana_and_imports_nothing_else_of_the_provider() -> None:
    assert (MANIFEST.key, MANIFEST.tier, MANIFEST.hosts, MANIFEST.path_prefix) == (
        "asana",
        Tier.FINISHED,
        ["app.asana.com"],
        "/api/1.0",
    )
    assert MANIFEST.kinds == [EntityKind.TICKET, EntityKind.COMMENT] and not MANIFEST.pushes_events
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, minutehand.adapters.providers.asana.manifest;"
            "print(sorted(m for m in sys.modules if m.startswith('minutehand.adapters.providers.asana.')))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "['minutehand.adapters.providers.asana.manifest']"


def test_the_registry_finds_the_provider_by_its_host() -> None:
    registry = Registry.installed()
    claimed = registry.claimant("app.asana.com")
    assert claimed is not None and claimed == MANIFEST
    assert registry.provider(claimed).manifest == MANIFEST


async def test_a_seeded_tickets_labels_are_its_tags_and_its_comments_are_stories_by_their_people(
    tmp_path: Path,
) -> None:
    labelled = SeededTicket(
        provider="asana",
        project="Catering",
        title="Order the cake",
        labels=["urgent", "sweet"],
        comments=[SeededComment(by="noor", text="Lemon, not chocolate.")],
    )
    scenario = SCENARIO.model_copy(update={"tickets": [*SCENARIO.tickets, labelled]})
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "root", clock)
    build().seed(scenario, store)
    gid = seeded_gid(scenario, labelled)
    async with client_for(build(), store, clock) as client:
        task = data(await client.get(f"/tasks/{gid}", params={"opt_fields": "tags.name"}))
        stories = items(await client.get(f"/tasks/{gid}/stories", params={"opt_fields": "text,created_by.email"}))
        tags = items(await client.get("/tags", params={"workspace": WS, "opt_fields": "name"}))

    assert [t["name"] for t in task["tags"]] == ["urgent", "sweet"]  # type: ignore[index,union-attr]
    assert [(s["text"], s["created_by"]["email"]) for s in stories] == [  # type: ignore[index]
        ("Lemon, not chocolate.", "noor@example.com")
    ]
    assert {"urgent", "sweet"} <= {t["name"] for t in tags}


def test_a_ticket_key_on_an_asana_ticket_is_refused_at_load_naming_the_ticket() -> None:
    keyed = SeededTicket(key="cake", provider="asana", project="Catering", title="Order the cake")
    scenario = SCENARIO.model_copy(update={"tickets": [keyed]})
    agent = AgentUnderTest(
        name="a", goal=GoalByWake(), wakes=[Reported(wake_url="http://w.test/w", report_url="http://w.test/r")]
    )
    with pytest.raises(
        RunRefused, match="the seeded ticket 'Order the cake' sets key, which asana tickets cannot hold"
    ):
        _services(scenario, agent, Registry.installed())
    labelled = keyed.model_copy(update={"key": None, "labels": ["urgent"]})
    on_youtrack = keyed.model_copy(update={"provider": "youtrack"})
    _services(SCENARIO.model_copy(update={"tickets": [labelled, on_youtrack]}), agent, Registry.installed())
