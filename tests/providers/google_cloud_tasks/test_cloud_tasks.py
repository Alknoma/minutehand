"""Google Cloud Tasks, reached the way an agent reaches it: Google's own `google-cloud-tasks` client on its REST
transport, in a process of its own configured only by the environment Minutehand hands an agent, through the proxy;
and deliveries made to a handler on this machine as Cloud Tasks makes them."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.adapters.providers.google_cloud_tasks.provider import (
    CloudTasksProvider,
    CloudTasksSeed,
    SeededQueue,
    build,
)
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.scenario import Person, ProviderSeed, Scenario, Scripted, SignIn
from minutehand.domain.world import Actor, RecordSnapshot
from minutehand.ports.provider import BooksWakes, ConfirmsDelivery
from tests.orchestrator.world import serving as handler_at
from tests.providers.google_workspace.proxied import Client, client_environment

pytestmark = pytest.mark.timeout(120)

START = datetime(2026, 8, 24, 9, tzinfo=UTC)
QUEUE = "projects/sim-project/locations/us-central1/queues/follow-ups"

PRELUDE = f"""
import json, sys, datetime
from google.auth.credentials import AnonymousCredentials
from google.api_core import exceptions
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

client = tasks_v2.CloudTasksClient(transport="rest", credentials=AnonymousCredentials())
QUEUE = {QUEUE!r}

def say(**found):
    print(json.dumps(found), flush=True)

def wait():
    return sys.stdin.readline().strip()

def at(iso):
    ts = timestamp_pb2.Timestamp()
    ts.FromDatetime(datetime.datetime.fromisoformat(iso))
    return ts

def refused(call):
    try:
        call()
    except exceptions.GoogleAPICallError as error:
        return [type(error).__name__, error.message]
    return None
"""


class ListWakes:
    def __init__(self) -> None:
        self.pending: list[Due] = []

    def book(self, due: Due) -> None:
        self.pending = [d for d in self.pending if d.ref != due.ref] + [due]

    def cancel(self, ref: str) -> None:
        self.pending = [d for d in self.pending if d.ref != ref]


@dataclass
class Tasks:
    proxy: Proxy
    store: SqliteStore
    clock: RunClock
    provider: CloudTasksProvider
    wakes: ListWakes
    environment: dict[str, str]

    async def client(self, program: str) -> Client:
        import asyncio
        import sys

        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            PRELUDE + program,
            env=self.environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return Client(child)


def seeded(**retry: object) -> Scenario:
    queue = SeededQueue.model_validate({"name": QUEUE, **retry})
    return Scenario(
        name="deferred",
        goal="Follow up tomorrow.",
        owner="owen",
        starts_at=START,
        people=[Person(key="owen", name="Owen", email="owen@example.com", reply=Scripted(replies=[]))],
        provider_seeds=[
            ProviderSeed(provider="google_cloud_tasks", body=CloudTasksSeed(queues=[queue]).model_dump_json())
        ],
    )


async def opened(tmp_path: Path, scenario: Scenario) -> AsyncIterator[Tasks]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    provider = build()
    wakes = ListWakes()
    provider.bind(wakes)
    provider.seed(scenario, store)
    registry = Registry()
    registry.discover("minutehand.adapters.providers")
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(store, clock, {"google_cloud_tasks": provider.app(store, clock)}, scenario=scenario)
        yield Tasks(proxy, store, clock, provider, wakes, client_environment(proxy))


@pytest.fixture
async def tasks(tmp_path: Path) -> AsyncIterator[Tasks]:
    async for found in opened(tmp_path, seeded(min_backoff=timedelta(seconds=10), max_attempts=3)):
        yield found


@dataclass
class Handler:
    """The agent's task handler: every request it was sent, answered with `status`."""

    status: int = 200
    heard: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)

    def app(self) -> Starlette:
        async def handle(request: Request) -> Response:
            self.heard.append((request.method, dict(request.headers), await request.body()))
            return JSONResponse({}, status_code=self.status)

        return Starlette(routes=[Route("/tasks/follow-up", handle, methods=["POST", "PUT"])])


def refusal(heard: dict[str, object], key: str) -> list[str]:
    """What the client's call was refused with: the exception's type and its message."""
    found = heard[key]
    assert isinstance(found, list) and all(isinstance(part, str) for part in found), f"{key} was not refused"
    return [str(part) for part in found]


def test_it_satisfies_the_ports() -> None:
    provider = build()
    assert isinstance(provider, BooksWakes) and isinstance(provider, ConfirmsDelivery)
    assert provider.manifest.books_wakes and provider.manifest.hosts == ["cloudtasks.googleapis.com"]


def test_a_seeded_sign_in_on_cloud_tasks_is_refused_at_seeding(tmp_path: Path) -> None:
    """Cloud Tasks has no sign-in of its own, so a `SignIn` for it would be dropped; it is refused, naming it."""
    signed = seeded().model_copy(
        update={"sign_ins": [SignIn(provider="google_cloud_tasks", credential="ya29.owen", person="owen")]}
    )
    with pytest.raises(ValueError, match=r"a seeded sign_in is on google_cloud_tasks, which has no sign-in"):
        build().seed(signed, SqliteStore(tmp_path / "world.db", "run", RunClock(START)))


async def test_a_task_created_by_googles_client_is_booked_at_its_schedule_time_and_read_back(tasks: Tasks) -> None:
    client = await tasks.client(
        """
task = {"http_request": {"http_method": tasks_v2.HttpMethod.POST, "url": "http://127.0.0.1:9/tasks/follow-up",
                         "headers": {"Content-Type": "application/json"}, "body": b'{"ask": "rosa"}'},
        "schedule_time": at("2026-08-26T09:00:00+00:00")}
made = client.create_task(parent=QUEUE, task=task)
got = client.get_task(request={"name": made.name, "response_view": tasks_v2.Task.View.FULL})
listed = [t.name for t in client.list_tasks(parent=QUEUE)]
say(name=made.name, scheduled=got.schedule_time.isoformat(), body=got.http_request.body.decode(), listed=listed,
    dispatches=got.dispatch_count)
"""
    )
    heard = await client.heard()
    await client.finished()

    name = str(heard["name"])
    assert name.startswith(QUEUE + "/tasks/") and heard["listed"] == [name]
    assert heard["body"] == '{"ask": "rosa"}' and heard["dispatches"] == 0
    assert tasks.wakes.pending == [Due(at=START + timedelta(days=2), kind=DueKind.AGENT_WAKE, ref=name)]
    made = [e for e in tasks.store.events() if e.actor is Actor.AGENT]
    assert isinstance(made[-1].after, RecordSnapshot)
    assert made[-1].after.text == 'POST http://127.0.0.1:9/tasks/follow-up {"ask": "rosa"}'


async def test_a_deleted_task_is_cancelled_and_its_name_stays_taken_for_an_hour_is_refused(tasks: Tasks) -> None:
    client = await tasks.client(
        """
name = QUEUE + "/tasks/remind-rosa-1"
task = {"name": name, "http_request": {"url": "http://127.0.0.1:9/tasks/follow-up"}}
client.create_task(parent=QUEUE, task=task)
twice = refused(lambda: client.create_task(parent=QUEUE, task=task))
client.delete_task(name=name)
gone = refused(lambda: client.get_task(name=name))
again = refused(lambda: client.create_task(parent=QUEUE, task=task))
say(twice=twice, gone=gone, again=again)
wait()
say(later=refused(lambda: client.create_task(parent=QUEUE, task=task)))
"""
    )
    heard = await client.heard()
    twice, gone, again = refusal(heard, "twice"), refusal(heard, "gone"), refusal(heard, "again")
    assert twice[0] == "Conflict" and twice[1].endswith(": Requested entity already exists")
    assert gone[0] == "NotFound"
    assert again[0] == "Conflict"
    assert again[1].endswith(": The task cannot be created because a task with this name existed too recently.")
    assert tasks.wakes.pending == []
    tasks.clock.jump(START + timedelta(hours=1, minutes=1))
    await client.go()
    assert (await client.heard())["later"] is None
    await client.finished()


async def test_a_task_in_a_queue_that_does_not_exist_is_refused_404(tasks: Tasks) -> None:
    client = await tasks.client(
        """
other = "projects/sim-project/locations/us-central1/queues/nowhere"
say(refused=refused(lambda: client.create_task(parent=other, task={"http_request": {"url": "http://127.0.0.1:9/x"}})))
"""
    )
    refused = refusal(await client.heard(), "refused")
    assert refused[0] == "NotFound" and refused[1].endswith(": Queue does not exist.")
    await client.finished()


async def test_a_task_asking_for_a_signed_oidc_token_is_answered_not_implemented(tasks: Tasks) -> None:
    client = await tasks.client(
        """
task = {"http_request": {"url": "https://agent.example.com/tasks", "oidc_token": {"service_account_email": "a@b.c"}}}
say(refused=refused(lambda: client.create_task(parent=QUEUE, task=task)))
"""
    )
    refused = refusal(await client.heard(), "refused")
    assert refused[0] == "MethodNotImplemented" and "signing keys" in refused[1]
    assert tasks.wakes.pending == []
    await client.finished()


def _reached(tasks: Tasks, when: datetime) -> None:
    """The run loop's part: the clock reaches the booking, which leaves the pending set before it is delivered."""
    tasks.clock.jump(when)
    tasks.wakes.pending = [d for d in tasks.wakes.pending if d.at > when]


async def _created(tasks: Tasks, url: str) -> str:
    client = await tasks.client(
        f"""
made = client.create_task(parent=QUEUE, task={{"http_request": {{"url": {url!r}, "body": b"follow up with rosa"}},
                                             "schedule_time": at("2026-08-25T09:00:00+00:00")}})
say(name=made.name)
"""
    )
    name = str((await client.heard())["name"])
    await client.finished()
    return name


async def test_a_task_is_delivered_to_its_handler_as_cloud_tasks_delivers_it_and_a_2xx_completes_it(
    tasks: Tasks,
) -> None:
    handler = Handler()
    async with handler_at(handler.app()) as base:
        name = await _created(tasks, f"{base}/tasks/follow-up")
        _reached(tasks, START + timedelta(days=1))
        await tasks.provider.deliver_booking(name, tasks.store, tasks.clock)
        await tasks.provider.advance_booking(name, tasks.store, tasks.clock)

    [(method, headers, body)] = handler.heard
    assert (method, body) == ("POST", b"follow up with rosa")
    assert headers["x-cloudtasks-queuename"] == "follow-ups"
    assert headers["x-cloudtasks-taskname"] == name.rsplit("/", 1)[1]
    assert (headers["x-cloudtasks-taskretrycount"], headers["x-cloudtasks-taskexecutioncount"]) == ("0", "0")
    assert headers["user-agent"] == "Google-Cloud-Tasks"
    assert tasks.provider.taken(name, tasks.store)
    assert tasks.wakes.pending == [], "a completed task books nothing more"


async def test_a_handler_that_fails_is_retried_with_the_queues_backoff_until_its_attempts_run_out(tasks: Tasks) -> None:
    handler = Handler(status=503)
    async with handler_at(handler.app()) as base:
        name = await _created(tasks, f"{base}/tasks/follow-up")
        _reached(tasks, START + timedelta(days=1))
        retries: list[datetime] = []
        for _ in range(3):
            await tasks.provider.deliver_booking(name, tasks.store, tasks.clock)
            await tasks.provider.advance_booking(name, tasks.store, tasks.clock)
            if tasks.wakes.pending:
                retries.append(tasks.wakes.pending[-1].at)
                _reached(tasks, tasks.wakes.pending[-1].at)

    day = START + timedelta(days=1)
    assert retries == [day + timedelta(seconds=10), day + timedelta(seconds=30)], "10 s, then doubled to 20 s"
    assert [h["x-cloudtasks-taskretrycount"] for _, h, _ in handler.heard] == ["0", "1", "2"]
    assert tasks.wakes.pending == [], "after its third failed attempt the task is given up"


async def test_a_delivery_answered_503_is_taken_as_soon_as_it_is_answered(tasks: Tasks) -> None:
    handler = Handler(status=503)
    async with handler_at(handler.app()) as base:
        name = await _created(tasks, f"{base}/tasks/follow-up")
        _reached(tasks, START + timedelta(days=1))
        assert not tasks.provider.taken(name, tasks.store), "nothing delivered yet"
        await tasks.provider.deliver_booking(name, tasks.store, tasks.clock)
        await tasks.provider.advance_booking(name, tasks.store, tasks.clock)

    assert tasks.provider.taken(name, tasks.store), "the agent answered: the run need not wait for anything more"


async def test_a_task_whose_url_is_not_this_machine_is_never_called_and_is_retried(tasks: Tasks) -> None:
    name = await _created(tasks, "https://agent.example.com/tasks/follow-up")
    _reached(tasks, START + timedelta(days=1))
    await tasks.provider.deliver_booking(name, tasks.store, tasks.clock)
    await tasks.provider.advance_booking(name, tasks.store, tasks.clock)

    attempt = json.loads(tasks.store.versions(tasks.store.events()[-1].entity)[-1].body)
    assert "not this machine (agent.example.com)" in attempt["last_attempt"]["failed"]
    assert [d.at for d in tasks.wakes.pending] == [START + timedelta(days=1, seconds=10)]


async def test_a_task_delivered_twice_reaches_its_handler_twice_and_completes_once(tasks: Tasks) -> None:
    handler = Handler()
    async with handler_at(handler.app()) as base:
        name = await _created(tasks, f"{base}/tasks/follow-up")
        _reached(tasks, START + timedelta(days=1))
        await tasks.provider.deliver_booking(name, tasks.store, tasks.clock)
        _reached(tasks, START + timedelta(days=1, minutes=1))
        await tasks.provider.deliver_booking(name, tasks.store, tasks.clock)
        await tasks.provider.advance_booking(name, tasks.store, tasks.clock)

    assert [h["x-cloudtasks-taskretrycount"] for _, h, _ in handler.heard] == ["0", "1"]
    assert tasks.wakes.pending == []
