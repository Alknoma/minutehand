"""Google Cloud Tasks over gRPC, the transport `CloudTasksClient()` speaks when nothing else is asked for: Google's own
client in a process of its own, configured only by the environment Minutehand hands an agent, through the proxy to
the gRPC server Minutehand runs for the provider, answered from the same world the REST routes answer from."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.domain.clock import Due, DueKind
from minutehand.domain.world import Actor, CallOutcome, GrpcCode, RecordedCall, RecordSnapshot
from tests.orchestrator.world import serving as handler_at
from tests.providers.google_cloud_tasks.test_cloud_tasks import (
    GRPC,
    QUEUE,
    START,
    Handler,
    Tasks,
    opened,
    refusal,
    seeded,
)

pytestmark = pytest.mark.timeout(120)

SERVICE = "/google.cloud.tasks.v2.CloudTasks"


@pytest.fixture
async def tasks(tmp_path: Path) -> AsyncIterator[Tasks]:
    async for found in opened(tmp_path, seeded(min_backoff=timedelta(seconds=10), max_attempts=3)):
        yield found


def grpc_calls(tasks: Tasks, method: str) -> list[RecordedCall]:
    return [c for c in tasks.store.calls() if c.exchange.path == f"{SERVICE}/{method}"]


async def test_a_task_created_over_grpc_is_booked_read_back_and_recorded_as_a_rest_one_is(tasks: Tasks) -> None:
    client = await tasks.client(
        """
task = {"http_request": {"http_method": tasks_v2.HttpMethod.POST, "url": "http://127.0.0.1:9/tasks/follow-up",
                         "headers": {"Content-Type": "application/json"}, "body": b'{"ask": "rosa"}'},
        "schedule_time": at("2026-08-26T09:00:00+00:00")}
made = client.create_task(parent=QUEUE, task=task)
got = client.get_task(request={"name": made.name, "response_view": tasks_v2.Task.View.FULL})
listed = [t.name for t in client.list_tasks(parent=QUEUE)]
say(name=made.name, scheduled=got.schedule_time.isoformat(), body=got.http_request.body.decode(), listed=listed,
    transport=type(client.transport).__name__)
""",
        transport=GRPC,
    )
    heard = await client.heard()
    await client.finished()

    name = str(heard["name"])
    assert heard["transport"] == "CloudTasksGrpcTransport", "the client's default transport is gRPC"
    assert name.startswith(QUEUE + "/tasks/") and heard["listed"] == [name]
    assert heard["body"] == '{"ask": "rosa"}' and str(heard["scheduled"]).startswith("2026-08-26T09:00:00")
    assert tasks.wakes.pending == [Due(at=START + timedelta(days=2), kind=DueKind.AGENT_WAKE, ref=name)]
    [created] = grpc_calls(tasks, "CreateTask")
    exchange = created.exchange
    assert exchange.host == "cloudtasks.googleapis.com" and exchange.status == 200
    assert exchange.grpc is not None and exchange.grpc.code is GrpcCode.OK
    assert exchange.outcome is CallOutcome.ANSWERED and created.provider == "google_cloud_tasks"
    assert exchange.request_body is not None and json.loads(exchange.request_body)["parent"] == QUEUE
    assert exchange.response_body is not None and json.loads(exchange.response_body)["name"] == name
    [event] = [e for e in tasks.store.events() if created.first_seq <= e.seq <= created.last_seq]
    assert event.actor is Actor.AGENT and isinstance(event.after, RecordSnapshot)
    assert event.after.text == 'POST http://127.0.0.1:9/tasks/follow-up {"ask": "rosa"}'
    assert [c.exchange.grpc.code for c in grpc_calls(tasks, "GetTask") if c.exchange.grpc] == [GrpcCode.OK]


async def test_queues_over_grpc_are_created_read_listed_and_deleted_with_their_tasks(tasks: Tasks) -> None:
    client = await tasks.client(
        """
parent = "projects/sim-project/locations/us-central1"
made = client.create_queue(parent=parent, queue={"name": parent + "/queues/reminders",
                                                 "retry_config": {"max_attempts": 4}})
got = client.get_queue(name=made.name)
client.create_task(parent=made.name, task={"http_request": {"url": "http://127.0.0.1:9/x"},
                                           "schedule_time": at("2026-08-25T09:00:00+00:00")})
listed = sorted(q.name for q in client.list_queues(parent=parent))
client.delete_queue(name=made.name)
say(attempts=got.retry_config.max_attempts, listed=listed, gone=refused(lambda: client.get_queue(name=made.name)),
    after=sorted(q.name for q in client.list_queues(parent=parent)))
""",
        transport=GRPC,
    )
    heard = await client.heard()
    await client.finished()

    reminders = "projects/sim-project/locations/us-central1/queues/reminders"
    assert heard["attempts"] == 4 and heard["listed"] == [QUEUE, reminders]
    assert refusal(heard, "gone")[0] == "NotFound" and heard["after"] == [QUEUE]
    assert tasks.wakes.pending == [], "the queue's task was cancelled with it, as over REST"


async def test_grpc_refusals_carry_the_status_cloud_tasks_refuses_with_and_are_recorded_refused(tasks: Tasks) -> None:
    client = await tasks.client(
        """
other = "projects/sim-project/locations/us-central1/queues/nowhere"
task = {"name": QUEUE + "/tasks/once", "http_request": {"url": "http://127.0.0.1:9/x"}}
client.create_task(parent=QUEUE, task=task)
say(missing=refused(lambda: client.create_task(parent=other, task={"http_request": {"url": "http://127.0.0.1:9/x"}})),
    twice=refused(lambda: client.create_task(parent=QUEUE, task=task)),
    signed=refused(lambda: client.create_task(parent=QUEUE, task={"http_request": {
        "url": "https://agent.example.com/x", "oidc_token": {"service_account_email": "a@b.c"}}})),
    paused=refused(lambda: client.pause_queue(name=QUEUE)),
    malformed=refused(lambda: client.get_task(name="tasks/nothing")))
""",
        transport=GRPC,
    )
    heard = await client.heard()
    await client.finished()

    missing, twice = refusal(heard, "missing"), refusal(heard, "twice")
    assert missing == ["NotFound", "Queue does not exist."]
    assert twice == ["AlreadyExists", "Requested entity already exists"]
    signed = refusal(heard, "signed")
    assert signed[0] == "MethodNotImplemented" and "signing keys" in signed[1]
    assert refusal(heard, "paused")[0] == "MethodNotImplemented", "a method the fake does not serve"
    assert refusal(heard, "malformed")[0] == "InvalidArgument"
    outcomes = [(c.exchange.path.rsplit("/", 1)[1], c.exchange.outcome) for c in tasks.store.calls()]
    assert outcomes == [
        ("CreateTask", CallOutcome.ANSWERED),
        ("CreateTask", CallOutcome.REFUSED),
        ("CreateTask", CallOutcome.REFUSED),
        ("CreateTask", CallOutcome.NOT_IMPLEMENTED),
        ("PauseQueue", CallOutcome.NOT_IMPLEMENTED),
        ("GetTask", CallOutcome.REFUSED),
    ]
    [not_found] = [c for c in tasks.store.calls() if c.exchange.grpc and c.exchange.grpc.code is GrpcCode.NOT_FOUND]
    assert not_found.exchange.grpc is not None and not_found.exchange.grpc.message == "Queue does not exist."
    signed_call = tasks.store.calls()[3].exchange
    assert signed_call.failure is not None and signed_call.failure.kind is CallOutcome.NOT_IMPLEMENTED


async def test_a_task_booked_over_grpc_is_delivered_and_completed_as_a_rest_one_is(tasks: Tasks) -> None:
    handler = Handler()
    async with handler_at(handler.app()) as base:
        client = await tasks.client(
            f"""
made = client.create_task(parent=QUEUE, task={{"http_request": {{"url": "{base}/tasks/follow-up",
                                             "body": b"follow up with rosa"}},
                                             "schedule_time": at("2026-08-25T09:00:00+00:00")}})
say(name=made.name)
""",
            transport=GRPC,
        )
        name = str((await client.heard())["name"])
        await client.finished()
        [due] = tasks.wakes.pending
        tasks.clock.jump(due.at)
        tasks.wakes.pending = []
        await tasks.provider.deliver_booking(name, tasks.store, tasks.clock)
        await tasks.provider.advance_booking(name, tasks.store, tasks.clock)

    assert due == Due(at=START + timedelta(days=1), kind=DueKind.AGENT_WAKE, ref=name)
    [(method, headers, body)] = handler.heard
    assert (method, body) == ("POST", b"follow up with rosa")
    assert headers["x-cloudtasks-taskname"] == name.rsplit("/", 1)[1]
    assert tasks.provider.taken(name, tasks.store) and tasks.wakes.pending == []


async def test_grpc_to_a_claimed_host_whose_provider_serves_no_grpc_is_refused_unimplemented(tasks: Tasks) -> None:
    client = await tasks.client(
        """
import grpc
channel = grpc.secure_channel("app.asana.com:443", grpc.ssl_channel_credentials())
try:
    channel.unary_unary("/asana.Tasks/List")(b"", timeout=20)
    say(code=None)
except grpc.RpcError as error:
    say(code=error.code().name, details=error.details())
""",
        transport=GRPC,
    )
    heard = await client.heard()
    await client.finished()

    assert heard["code"] == "UNIMPLEMENTED"
    assert heard["details"] == "minutehand's asana fake does not serve gRPC: set the client to its REST transport"
    [call] = tasks.store.calls()
    assert call.exchange.host == "app.asana.com" and call.exchange.path == "/asana.Tasks/List"
    assert call.exchange.grpc is not None and call.exchange.grpc.code is GrpcCode.UNIMPLEMENTED
    assert call.exchange.outcome is CallOutcome.NOT_IMPLEMENTED
