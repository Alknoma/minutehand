"""The instance lives in the store and nowhere else; the provider meets its ports and claims its hosts."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.youtrack import state
from minutehand.adapters.providers.youtrack.manifest import MANIFEST
from minutehand.adapters.providers.youtrack.provider import build
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.provider import Tier
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor, EntityKind, Operation, TicketSnapshot
from minutehand.ports.provider import EditsTickets, HoldsTickets, Provider, TicketsHappen
from tests.providers.youtrack.youtrack_instance import (
    FIELD_OPS,
    IRIS,
    LAUNCH,
    NOOR,
    SCENARIO,
    START,
    TOMAS,
    Instance,
    client_for,
    create,
    entities,
    entity,
    millis_now,
    named,
    readable_ids,
)


async def test_the_instance_survives_a_new_app_over_a_new_connection(instance: Instance) -> None:
    async with client_for(instance.provider, instance.store, instance.clock) as first:
        made = await create(first, "Written by the first app")

    reopened = SqliteStore(instance.path, "root", RunClock(START))
    async with client_for(build(), reopened, RunClock(START)) as second:
        read = entity(await second.get(f"/api/issues/{made['idReadable']}", params={"fields": "id,summary"}))
        following = await create(second, "Written by the second app")

    assert read == {"id": made["id"], "summary": "Written by the first app", "$type": "Issue"}
    assert following["idReadable"] == "LAUNCH-4"


async def test_a_fork_sees_issues_only_up_to_the_fork(instance: Instance, client: httpx.AsyncClient) -> None:
    await create(client, "Before the fork")
    at = instance.store.head()
    after = await create(client, "After, in the parent")

    fork = instance.store.fork("what-if", at_seq=at, clock=RunClock(START))
    async with client_for(instance.provider, fork, RunClock(START)) as forked:
        query = {"query": "project: LAUNCH", "fields": "idReadable,summary"}
        seen = entities(await forked.get("/api/issues", params=query))
        assert (await forked.get(f"/api/issues/{after['id']}")).status_code == 404
        mine = await create(forked, "Only in the fork")

    assert [i["summary"] for i in seen] == ["Write the release notes", "Book the venue", "Before the fork"]
    assert mine["idReadable"] == "LAUNCH-4"
    parent = entities(await client.get("/api/issues", params={"query": "project: LAUNCH", "fields": "summary"}))
    assert [i["summary"] for i in parent] == [
        "Write the release notes",
        "Book the venue",
        "Before the fork",
        "After, in the parent",
    ]


def test_seeding_writes_people_the_agent_projects_and_issues_as_the_scenario(instance: Instance) -> None:
    events = instance.store.events()
    assert events and {e.actor for e in events} == {Actor.SCENARIO}

    youtrack = instance.youtrack
    assert [(u.id, u.login, u.email) for u in youtrack.users()] == [
        ("1-0", "agent-bot", "agent-bot@youtrack.invalid"),
        (IRIS, "iris", "iris@example.com"),
        (TOMAS, "tomas", "tomas@example.com"),
        (NOOR, "noor", "noor@example.com"),
    ]
    projects = youtrack.projects()
    assert [(p.id, p.shortName, p.name) for p in projects] == [
        (LAUNCH, "LAUNCH", "Launch"),
        (FIELD_OPS, "FIELDOPS", "Field Ops"),
    ]
    for project in projects:
        states = youtrack.state_field(project)
        assert states is not None and [s.name for s in states.values] == ["Open", "In Progress", "Fixed", "Won't fix"]
    assert all(p.team == ["1-0", IRIS, TOMAS, NOOR] for p in projects)

    tickets = [e for e in events if e.entity.kind is EntityKind.TICKET]
    assert [e.after for e in tickets] == [
        TicketSnapshot(title="Write the release notes", project="LAUNCH", assignee_email="tomas@example.com"),
        TicketSnapshot(
            title="Book the venue",
            body="Forty seats",
            project="LAUNCH",
            assignee_email="noor@example.com",
            state=TicketState.DONE,
        ),
        TicketSnapshot(title="Ship the demo kits", project="FIELDOPS"),
    ]
    issue = youtrack.find_issue("LAUNCH-1")
    assert issue is not None and issue.reporter == IRIS


def test_seeding_twice_gives_the_same_ids(tmp_path: Path) -> None:
    def ids(run: str) -> list[str]:
        clock = RunClock(START)
        store = SqliteStore(tmp_path / f"{run}.db", run, clock)
        build().seed(SCENARIO, store)
        return [e.entity.external_id for e in store.events()]

    assert ids("one") == ids("two")


def _assigned(instance: Instance, readable: str) -> str:
    issue = instance.youtrack.find_issue(readable)
    assert issue is not None
    return issue.id


@pytest.mark.parametrize(
    ("to", "value", "resolved"),
    [
        (TicketState.DONE, "Fixed", True),
        (TicketState.CANCELLED, "Won't fix", True),
    ],
)
async def test_transition_moves_the_issue_as_its_assignee(
    instance: Instance, client: httpx.AsyncClient, to: TicketState, value: str, resolved: bool
) -> None:
    issue_id = _assigned(instance, "LAUNCH-1")
    instance.clock.jump(START + timedelta(days=2))

    instance.provider.transition(state.issue_ref(issue_id), to, instance.store, instance.clock)

    last = instance.store.events()[-1]
    assert (last.actor, last.operation, last.entity) == (Actor.PERSON, Operation.UPDATE, state.issue_ref(issue_id))
    assert last.after == TicketSnapshot(
        title="Write the release notes",
        project="LAUNCH",
        assignee_email="tomas@example.com",
        state=to,
    )
    read = entity(
        await client.get(
            "/api/issues/LAUNCH-1",
            params={
                "fields": "resolved,updated,updater(login),customFields(name,value(name))",
            },
        )
    )
    assert read["resolved"] == (millis_now(instance.clock) if resolved else None)
    assert read["updated"] == millis_now(instance.clock)
    assert read["updater"] == {"login": "tomas", "$type": "User"}
    found = entities(await client.get("/api/issues", params={"query": "#Resolved", "fields": "idReadable"}))
    assert "LAUNCH-1" in readable_ids(found)
    assert named(read["customFields"], "State") == {
        "name": "State",
        "value": {"name": value, "$type": "StateBundleElement"},
        "$type": "StateIssueCustomField",
    }


def test_transition_back_to_open_clears_resolved(instance: Instance) -> None:
    issue_id = _assigned(instance, "LAUNCH-2")

    instance.provider.transition(state.issue_ref(issue_id), TicketState.OPEN, instance.store, instance.clock)

    moved = instance.youtrack.issue(issue_id)
    assert moved is not None and moved.resolved is None
    last = instance.store.events()[-1]
    assert isinstance(last.after, TicketSnapshot) and last.after.state is TicketState.OPEN


def test_transition_of_an_unassigned_issue_is_refused(instance: Instance) -> None:
    with pytest.raises(ValueError, match="no assignee"):
        instance.provider.transition(
            state.issue_ref(_assigned(instance, "FIELDOPS-1")), TicketState.DONE, instance.store, instance.clock
        )


def test_edit_rewrites_state_and_assignee_as_the_scenario(instance: Instance) -> None:
    issue_id = _assigned(instance, "LAUNCH-1")

    instance.provider.edit(
        state.issue_ref(issue_id),
        state=TicketState.CANCELLED,
        assignee_email="iris@example.com",
        world=instance.store,
        clock=instance.clock,
    )

    last = instance.store.events()[-1]
    assert (last.actor, last.operation) == (Actor.SCENARIO, Operation.UPDATE)
    assert last.after == TicketSnapshot(
        title="Write the release notes",
        project="LAUNCH",
        assignee_email="iris@example.com",
        state=TicketState.CANCELLED,
    )


def test_edit_with_nothing_named_leaves_both_fields(instance: Instance) -> None:
    issue_id = _assigned(instance, "LAUNCH-2")

    instance.provider.edit(
        state.issue_ref(issue_id), state=None, assignee_email=None, world=instance.store, clock=instance.clock
    )

    last = instance.store.events()[-1]
    assert last.actor is Actor.SCENARIO
    assert last.after == TicketSnapshot(
        title="Book the venue",
        body="Forty seats",
        project="LAUNCH",
        assignee_email="noor@example.com",
        state=TicketState.DONE,
    )


def test_edit_to_an_email_nobody_has_is_refused(instance: Instance) -> None:
    with pytest.raises(LookupError, match=r"nobody@example\.com"):
        instance.provider.edit(
            state.issue_ref(_assigned(instance, "LAUNCH-1")),
            state=None,
            assignee_email="nobody@example.com",
            world=instance.store,
            clock=instance.clock,
        )


def test_the_provider_meets_its_four_ports() -> None:
    provider = build()
    held: Provider = provider
    holds: HoldsTickets = provider
    edits: EditsTickets = provider
    happens: TicketsHappen = provider
    assert held.manifest is MANIFEST and holds is edits is happens
    assert isinstance(provider, TicketsHappen)


def test_the_manifest_claims_youtrack_and_imports_nothing_else_of_the_provider() -> None:
    assert (MANIFEST.key, MANIFEST.tier, MANIFEST.hosts, MANIFEST.path_prefix) == (
        "youtrack",
        Tier.FINISHED,
        ["*.youtrack.cloud", "*.myjetbrains.com"],
        "",
    )
    assert MANIFEST.kinds == [EntityKind.TICKET, EntityKind.COMMENT] and not MANIFEST.pushes_events
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, minutehand.adapters.providers.youtrack.manifest;"
            "print(sorted(m for m in sys.modules if m.startswith('minutehand.adapters.providers.youtrack.')))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert loaded.stdout.strip() == "['minutehand.adapters.providers.youtrack.manifest']"


@pytest.mark.parametrize(
    "host", ["lanternworks.youtrack.cloud", "Lanternworks.YouTrack.Cloud", "lanternworks.myjetbrains.com"]
)
def test_the_installed_registry_routes_both_host_families_here(host: str) -> None:
    claimed = Registry.installed().claimant(host)
    assert claimed is not None and claimed.key == "youtrack"


@pytest.mark.parametrize("host", ["youtrack.cloud", "youtrack.example.com", "myjetbrains.com.evil.test"])
def test_hosts_outside_both_families_are_not_claimed(host: str) -> None:
    claimed = Registry.installed().claimant(host)
    assert claimed is None or claimed.key != "youtrack"


@pytest.mark.parametrize("base", ["https://lanternworks.youtrack.cloud", "https://lanternworks.myjetbrains.com"])
@pytest.mark.parametrize("prefix", ["/api", "/youtrack/api"])
async def test_both_path_shapes_answer_on_both_host_families(instance: Instance, base: str, prefix: str) -> None:
    async with client_for(instance.provider, instance.store, instance.clock, base_url=base) as c:
        made = entity(
            await c.post(
                f"{prefix}/issues",
                params={"fields": "idReadable"},
                json={"project": {"id": LAUNCH}, "summary": f"Filed at {prefix}"},
            )
        )
        read = entity(await c.get(f"{prefix}/issues/{made['idReadable']}", params={"fields": "summary"}))
        me = entity(await c.get(f"{prefix}/users/me", params={"fields": "login"}))

    assert read["summary"] == f"Filed at {prefix}" and me["login"] == "agent-bot"
