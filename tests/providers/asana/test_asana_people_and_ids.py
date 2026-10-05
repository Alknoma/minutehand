"""People with no email or a hidden one, a person's own Asana account, and gids a seed declares, read with the real
`asana` SDK as a client reads them."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

import asana  # pyright: ignore[reportMissingTypeStubs]
import pytest
import uvicorn

from minutehand.adapters.providers.asana import state
from minutehand.adapters.providers.asana.manifest import MANIFEST
from minutehand.adapters.providers.asana.provider import AsanaProvider, build
from minutehand.adapters.providers.asana.state import AsanaWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.refusals import RunRefused, refuse_unheld
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Account, Scenario, TicketHappening
from minutehand.domain.world import TicketSnapshot
from tests.providers.asana.asana_workspace import START, TOKEN
from tests.providers.asana.test_asana_sdk_client import behind_the_proxy

T = TypeVar("T")

SERVICE_GID = "1206000000000042"
DECLARED_TASK = "1206000000000100"

SCENARIO = Scenario.model_validate(
    {
        "name": "deploys",
        "goal": "Every deploy task has an owner.",
        "owner": "iris",
        "starts_at": START,
        "people": [
            {"key": "iris", "name": "Iris Calder", "email": "iris@example.com"},
            {
                "key": "deploy_bot",
                "name": "Deploy bot",
                "accounts": [{"provider": "asana", "id": SERVICE_GID, "name": "Deploy Service"}],
            },
            {
                "key": "pat",
                "name": "Pat Quinn",
                "email": "pat@example.com",
                "accounts": [{"provider": "asana", "email_visible": False}],
            },
            {"key": "dana", "name": "Dana Moss", "email": "dana@example.com", "account": "deactivated"},
        ],
        "tickets": [
            {
                "provider": "asana",
                "project": "Deploys",
                "title": "Roll out 4.2",
                "assignee": "deploy_bot",
                "id": DECLARED_TASK,
            },
            {"provider": "asana", "project": "Deploys", "title": "Write the notes", "assignee": "pat"},
        ],
    }
)


def with_(**changed: object) -> Scenario:
    return Scenario.model_validate({**SCENARIO.model_dump(), **changed})


@dataclass
class Seeded:
    provider: AsanaProvider
    store: SqliteStore
    clock: RunClock

    @property
    def asana(self) -> AsanaWorld:
        return AsanaWorld(self.store)


def seeded(tmp_path: Path, scenario: Scenario = SCENARIO) -> Seeded:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / f"world-{len(list(tmp_path.iterdir()))}.db", "root", clock)
    provider = build()
    provider.seed(scenario, store)
    return Seeded(provider=provider, store=store, clock=clock)


@dataclass
class Sdk:
    tasks: Any
    users: Any


@pytest.fixture
def world(tmp_path: Path) -> Seeded:
    return seeded(tmp_path)


@pytest.fixture
async def sdk(world: Seeded) -> AsyncIterator[Sdk]:
    server = uvicorn.Server(
        uvicorn.Config(
            behind_the_proxy(world.provider.app(world.store, world.clock)),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    configuration = asana.Configuration()
    configuration.access_token = TOKEN
    configuration.host = f"http://127.0.0.1:{port}{urlparse(configuration.host).path}"
    client = asana.ApiClient(configuration)
    yield Sdk(tasks=asana.TasksApi(client), users=asana.UsersApi(client))
    server.should_exit = True
    await serving


async def off_loop(call: Callable[[], T]) -> T:
    return await asyncio.to_thread(call)


async def test_a_person_without_an_email_is_a_user_whose_email_is_null(sdk: Sdk) -> None:
    read = await off_loop(lambda: sdk.users.get_user(SERVICE_GID, {"opt_fields": "name,email"}))
    assert read == {"gid": SERVICE_GID, "name": "Deploy Service", "email": None}


async def test_a_hidden_email_is_not_shown_though_the_person_has_one(sdk: Sdk) -> None:
    pat = state.user_gid("pat")
    read = await off_loop(lambda: sdk.users.get_user(pat, {"opt_fields": "name,email"}))
    assert read == {"gid": pat, "name": "Pat Quinn", "email": None}


async def test_a_seeded_task_takes_its_declared_gid_and_the_rest_keep_theirs(sdk: Sdk) -> None:
    read = await off_loop(lambda: sdk.tasks.get_task(DECLARED_TASK, {"opt_fields": "name,assignee"}))
    assert read["name"] == "Roll out 4.2"
    assert read["assignee"]["gid"] == SERVICE_GID
    other = await off_loop(lambda: sdk.tasks.get_task(state.task_gid(1), {"opt_fields": "name"}))
    assert other["name"] == "Write the notes"


async def test_a_deactivated_person_is_seeded_removed_from_the_workspace(sdk: Sdk) -> None:
    listed = await off_loop(lambda: list(sdk.users.get_users({"workspace": state.WORKSPACE_GID, "opt_fields": "name"})))
    names = {u["name"] for u in listed}
    assert "Dana Moss" not in names
    assert {"Iris Calder", "Deploy Service", "Pat Quinn"} <= names


def test_the_snapshot_names_the_assignee_by_key_when_they_have_no_email(world: Seeded) -> None:
    task = world.asana.task(DECLARED_TASK)
    assert task is not None
    assert world.asana.snapshot(task) == TicketSnapshot(title="Roll out 4.2", project="Deploys", assignee="deploy_bot")
    hidden = world.asana.task(state.task_gid(1))
    assert hidden is not None
    snapshot = world.asana.snapshot(hidden)
    assert (snapshot.assignee, snapshot.assignee_email) == ("pat", "pat@example.com")


def test_a_reassignment_and_an_edit_find_the_person_by_key_not_email(world: Seeded) -> None:
    happening = TicketHappening.model_validate(
        {
            "person": "iris",
            "ticket": "Write the notes",
            "after": "PT1H",
            "action": {"kind": "reassigns", "to": "deploy_bot"},
        }
    )
    world.provider.act(happening, SCENARIO, world.store, world.clock)
    task = world.asana.task(state.task_gid(1))
    assert task is not None and task.assignee == SERVICE_GID
    service = next(p for p in SCENARIO.people if p.key == "deploy_bot")
    world.provider.edit(
        state.task_ref(DECLARED_TASK), state=None, assignee=service, world=world.store, clock=world.clock
    )
    assert world.store.events()[-1].after == TicketSnapshot(
        title="Roll out 4.2", project="Deploys", assignee="deploy_bot"
    )


def test_removing_a_person_with_a_declared_gid_removes_that_user(world: Seeded) -> None:
    from minutehand.domain.provider import PersonChange

    service = next(p for p in SCENARIO.people if p.key == "deploy_bot")
    world.provider.change_person(PersonChange.REMOVED, service, world.store, world.clock)
    user = world.asana.user(SERVICE_GID)
    assert user is not None and user.removed


def test_a_task_the_agent_makes_never_takes_a_declared_gid(tmp_path: Path) -> None:
    head = seeded(tmp_path).store.head()
    minted_next = str(state._MINTED + head + 1)  # pyright: ignore[reportPrivateUsage]
    tickets = [t.model_dump() for t in SCENARIO.tickets]
    tickets[1]["id"] = minted_next
    world = seeded(tmp_path, with_(tickets=tickets))
    assert world.asana.next_gid() != minted_next


def test_a_gid_that_is_not_digits_is_refused(tmp_path: Path) -> None:
    tickets = [t.model_dump() for t in SCENARIO.tickets]
    tickets[0]["id"] = "task-1"
    with pytest.raises(ValueError, match="string of digits"):
        seeded(tmp_path, with_(tickets=tickets))


def test_a_declared_gid_another_seeded_thing_has_is_refused(tmp_path: Path) -> None:
    tickets = [t.model_dump() for t in SCENARIO.tickets]
    tickets[0]["id"] = state.user_gid("iris")
    with pytest.raises(ValueError, match="would take the gid"):
        seeded(tmp_path, with_(tickets=tickets))


def test_a_login_on_an_asana_account_is_refused() -> None:
    people = [p.model_dump() for p in SCENARIO.people]
    people[1]["accounts"] = [{"provider": "asana", "login": "deploy.service"}]
    with pytest.raises(RunRefused, match="login"):
        refuse_unheld(with_(people=people), {MANIFEST.key: MANIFEST})


def test_a_deactivated_person_is_in_no_team(world: Seeded) -> None:
    dana = world.asana.user(state.user_gid("dana"))
    assert dana is not None and dana.removed
    assert all(state.user_gid("dana") not in team.members for team in world.asana.teams())
    assert next(p for p in SCENARIO.people if p.key == "dana").account is Account.DEACTIVATED
