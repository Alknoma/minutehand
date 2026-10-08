"""Each Cloud Tasks behaviour `CLAIMS.md` pins that this provider once answered otherwise: Google's own client
through the proxy, deliveries to a handler on this machine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.adapters.providers.google_cloud_tasks import wire
from tests.orchestrator.world import serving as handler_at
from tests.providers.google_cloud_tasks.test_cloud_tasks import START, Tasks, opened, refusal, seeded

pytestmark = pytest.mark.timeout(120)


@dataclass
class Answers:
    """A handler that answers each delivery with the next status, then 200, keeping what it was sent."""

    statuses: list[int]
    heard: list[dict[str, str]] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)

    def app(self) -> Starlette:
        async def handle(request: Request) -> Response:
            self.heard.append(dict(request.headers))
            self.bodies.append(await request.body())
            status = self.statuses.pop(0) if self.statuses else 200
            return JSONResponse({}, status_code=status)

        return Starlette(routes=[Route("/tasks/follow-up", handle, methods=["POST"])])


async def _create(tasks: Tasks, task: str) -> str:
    client = await tasks.client(f"say(name=client.create_task(parent=QUEUE, task={task}).name)")
    name = str((await client.heard())["name"])
    await client.finished()
    return name


async def _run_out(tasks: Tasks, name: str, most: int = 60) -> list[timedelta]:
    """Deliver the task, and each retry it is booked for, until none is booked: the intervals between attempts."""
    [first] = tasks.wakes.pending
    tasks.wakes.pending = []
    tasks.clock.jump(first.at)
    attempts = [tasks.clock.now()]
    for _ in range(most):
        await tasks.provider.deliver_booking(name, tasks.store, tasks.clock)
        await tasks.provider.advance_booking(name, tasks.store, tasks.clock)
        if not tasks.wakes.pending:
            break
        [due] = tasks.wakes.pending
        tasks.wakes.pending = []
        tasks.clock.jump(due.at)
        attempts.append(due.at)
    return [b - a for a, b in pairwise(attempts)]


def _task(url: str, **more: str) -> str:
    extra = "".join(f", {k!r}: {v}" for k, v in more.items())
    return f'{{"http_request": {{"url": "{url}/tasks/follow-up", "body": b"hi"}}{extra}}}'


@pytest.fixture
async def doubling(tmp_path: Path) -> AsyncIterator[Tasks]:
    """The reference's own example: minBackoff 10s, maxBackoff 300s, maxDoublings 3."""
    queue = {
        "min_backoff": timedelta(seconds=10),
        "max_backoff": timedelta(seconds=300),
        "max_doublings": 3,
        "max_attempts": 9,
    }
    async for found in opened(tmp_path, seeded(**queue)):
        yield found


async def test_backoff_doubles_then_grows_linearly_then_holds_at_its_maximum(doubling: Tasks) -> None:
    """The reference: "the requests will retry at 10s, 20s, 40s, 80s, 160s, 240s, 300s, 300s, ...." (RetryConfig.maxDoublings)."""
    handler = Answers(statuses=[503] * 20)
    async with handler_at(handler.app()) as base:
        name = await _create(doubling, _task(base))
        intervals = await _run_out(doubling, name)
    assert [int(i.total_seconds()) for i in intervals] == [10, 20, 40, 80, 160, 240, 300, 300]


@pytest.fixture
async def both_limits(tmp_path: Path) -> AsyncIterator[Tasks]:
    queue = {"min_backoff": timedelta(seconds=10), "max_attempts": 2, "max_retry_duration": timedelta(seconds=60)}
    async for found in opened(tmp_path, seeded(**queue)):
        yield found


async def test_a_task_is_given_up_only_once_both_its_attempts_and_its_retry_duration_are_spent(
    both_limits: Tasks,
) -> None:
    """The reference: "Cloud Tasks stops retrying only when maxAttempts and maxRetryDuration are both satisfied"
    (RetryConfig.maxAttempts): two attempts are spent 10 s in, the 60 s only after 70 s (10+20+40)."""
    handler = Answers(statuses=[503] * 20)
    async with handler_at(handler.app()) as base:
        name = await _create(both_limits, _task(base))
        intervals = await _run_out(both_limits, name)
    assert [int(i.total_seconds()) for i in intervals] == [10, 20, 40]


@pytest.fixture
async def unlimited(tmp_path: Path) -> AsyncIterator[Tasks]:
    queue = {"min_backoff": timedelta(days=1), "max_backoff": timedelta(days=1), "max_attempts": -1}
    async for found in opened(tmp_path, seeded(**queue)):
        yield found


async def test_unlimited_attempts_and_duration_retry_until_the_retention_limit(unlimited: Tasks) -> None:
    """The reference: "If maxAttempts is set to -1 and maxRetryDuration is set to 0, the task is retried until the maximum task
    retention limit is reached" (RetryConfig.maxAttempts): 31 days (Quotas and limits)."""
    handler = Answers(statuses=[503] * 40)
    async with handler_at(handler.app()) as base:
        name = await _create(unlimited, _task(base))
        intervals = await _run_out(unlimited, name)
    assert sum(intervals, timedelta()) == timedelta(days=31) and len(intervals) == 31


@pytest.fixture
async def tasks(tmp_path: Path) -> AsyncIterator[Tasks]:
    async for found in opened(tmp_path, seeded(min_backoff=timedelta(seconds=10), max_attempts=5)):
        yield found


async def test_delivery_headers_count_retries_and_executions_and_name_the_previous_response(tasks: Tasks) -> None:
    """X-CloudTasks-TaskExecutionCount "does not include failures due to 5XX error codes";
    X-CloudTasks-TaskPreviousResponse is "The HTTP response code from the previous retry" (Creating HTTP target
    tasks)."""
    handler = Answers(statuses=[404, 503, 200])
    async with handler_at(handler.app()) as base:
        name = await _create(tasks, _task(base))
        await _run_out(tasks, name)
    seen = [
        (
            h["x-cloudtasks-taskretrycount"],
            h["x-cloudtasks-taskexecutioncount"],
            h["x-cloudtasks-taskpreviousresponse"] if "x-cloudtasks-taskpreviousresponse" in h else None,
        )
        for h in handler.heard
    ]
    assert seen == [("0", "0", None), ("1", "1", "404"), ("2", "1", "503")]


async def test_a_task_body_is_delivered_with_no_content_type_cloud_tasks_did_not_set(tasks: Tasks) -> None:
    """The reference: "Content-Type won't be set by Cloud Tasks" (HttpRequest.headers)."""
    handler = Answers(statuses=[])
    async with handler_at(handler.app()) as base:
        name = await _create(tasks, _task(base))
        await _run_out(tasks, name)
    assert handler.bodies == [b"hi"]
    assert "content-type" not in handler.heard[0]


async def test_google_only_headers_a_task_carries_are_not_sent_and_its_own_are(tasks: Tasks) -> None:
    """The reference: "X-Google-*: Google use only. X-AppEngine-*: Google use only" (HttpRequest.headers, ignored or replaced)."""
    handler = Answers(statuses=[])
    headers = '{"X-Google-Trace": "t", "X-AppEngine-Country": "ZZ", "X-Agent": "kept", "User-Agent": "mine"}'
    async with handler_at(handler.app()) as base:
        name = await _create(
            tasks, f'{{"http_request": {{"url": "{base}/tasks/follow-up", "body": b"hi", "headers": {headers}}}}}'
        )
        await _run_out(tasks, name)
    [heard] = handler.heard
    assert heard["x-agent"] == "kept" and heard["user-agent"] == "Google-Cloud-Tasks"
    assert "x-google-trace" not in heard and "x-appengine-country" not in heard


async def test_a_body_on_a_get_task_is_refused_invalid_argument(tasks: Tasks) -> None:
    client = await tasks.client(
        """
task = {"http_request": {"url": "http://127.0.0.1:9/x", "http_method": tasks_v2.HttpMethod.GET, "body": b"x"}}
say(refused=refused(lambda: client.create_task(parent=QUEUE, task=task)))
"""
    )
    refused = refusal(await client.heard(), "refused")
    await client.finished()
    assert refused[0] == "BadRequest" and refused[1].endswith(wire.BODY_RULE)
    assert tasks.wakes.pending == []


async def test_a_dispatch_deadline_outside_fifteen_seconds_to_thirty_minutes_is_refused(tasks: Tasks) -> None:
    client = await tasks.client(
        """
from google.protobuf import duration_pb2
def task(seconds):
    return {"http_request": {"url": "http://127.0.0.1:9/x"}, "dispatch_deadline": duration_pb2.Duration(seconds=seconds)}
say(short=refused(lambda: client.create_task(parent=QUEUE, task=task(14))),
    long=refused(lambda: client.create_task(parent=QUEUE, task=task(1801))),
    edge=refused(lambda: client.create_task(parent=QUEUE, task=task(1800))))
"""
    )
    heard = await client.heard()
    await client.finished()
    for key in ("short", "long"):
        assert refusal(heard, key)[0] == "BadRequest" and refusal(heard, key)[1].endswith(wire.DEADLINE_RULE)
    assert heard["edge"] is None


async def test_create_time_is_whole_seconds_and_the_first_attempt_keeps_only_its_dispatch_time(tasks: Tasks) -> None:
    """createTime "will be truncated to the nearest second"; firstAttempt: "Only dispatchTime will be set" (Task)."""
    tasks.clock.jump(START + timedelta(seconds=5, microseconds=250000))
    handler = Answers(statuses=[503])
    async with handler_at(handler.app()) as base:
        name = await _create(tasks, _task(base))
        await tasks.provider.deliver_booking(name, tasks.store, tasks.clock)
        await tasks.provider.advance_booking(name, tasks.store, tasks.clock)
    client = await tasks.client(
        f"""
got = client.get_task(name={name!r})
say(created=got.create_time.isoformat(), first=sorted(f.name for f, _ in got.first_attempt._pb.ListFields()),
    last=sorted(f.name for f, _ in got.last_attempt._pb.ListFields()))
"""
    )
    heard = await client.heard()
    await client.finished()
    assert heard["created"] == "2026-08-24T09:00:05+00:00"
    assert heard["first"] == ["dispatch_time"]
    assert heard["last"] == ["dispatch_time", "response_time", "schedule_time"]


async def test_a_queues_rate_limits_are_read_back_as_sent_or_as_googles_defaults(tasks: Tasks) -> None:
    client = await tasks.client(
        """
location = QUEUE.rsplit("/queues/", 1)[0]
mine = client.create_queue(parent=location, queue={"name": location + "/queues/slow",
                                                  "rate_limits": {"max_dispatches_per_second": 5}})
plain = client.get_queue(name=QUEUE)
say(mine=[mine.rate_limits.max_dispatches_per_second, mine.rate_limits.max_concurrent_dispatches,
          mine.rate_limits.max_burst_size],
    plain=[plain.rate_limits.max_dispatches_per_second, plain.rate_limits.max_concurrent_dispatches,
           plain.rate_limits.max_burst_size])
"""
    )
    heard = await client.heard()
    await client.finished()
    assert heard["mine"] == [5.0, 1000, 0], "maxBurstSize is the system's, and not documented for this rate"
    assert heard["plain"] == [500.0, 1000, 100]


async def test_a_queue_recreated_within_its_tombstone_window_is_refused_by_name(tasks: Tasks) -> None:
    client = await tasks.client(
        """
location = QUEUE.rsplit("/queues/", 1)[0]
client.delete_queue(name=QUEUE)
say(again=refused(lambda: client.create_queue(parent=location, queue={"name": QUEUE})))
wait()
say(later=client.create_queue(parent=location, queue={"name": QUEUE}).name)
"""
    )
    again = refusal(await client.heard(), "again")
    assert again[0] == "MethodNotImplemented" and "tombstone window" in again[1]
    tasks.clock.jump(START + timedelta(days=3, minutes=1))
    await client.go()
    assert str((await client.heard())["later"]).endswith("/queues/follow-ups")
    await client.finished()


async def test_tasks_are_listed_a_page_at_a_time_and_a_filter_is_refused_by_name(tasks: Tasks) -> None:
    client = await tasks.client(
        """
for n in range(3):
    client.create_task(parent=QUEUE, task={"name": f"{QUEUE}/tasks/t{n}", "http_request": {"url": "http://127.0.0.1:9/x"}})
pages = [[t.name.rsplit("/", 1)[1] for t in page.tasks]
         for page in client.list_tasks(request={"parent": QUEUE, "page_size": 2}).pages]
location = QUEUE.rsplit("/queues/", 1)[0]
say(pages=pages, filtered=refused(lambda: list(client.list_queues(request={"parent": location, "filter": "state: PAUSED"}))))
"""
    )
    heard = await client.heard()
    await client.finished()
    assert heard["pages"] == [["t0", "t1"], ["t2"]]
    filtered = refusal(heard, "filtered")
    assert filtered[0] == "MethodNotImplemented" and "filter" in filtered[1]


async def test_names_outside_the_documented_id_formats_are_refused(tasks: Tasks) -> None:
    client = await tasks.client(
        """
location = QUEUE.rsplit("/queues/", 1)[0]
say(queue=refused(lambda: client.create_queue(parent=location, queue={"name": location + "/queues/no_underscores"})),
    task=refused(lambda: client.create_task(parent=QUEUE, task={"name": QUEUE + "/tasks/" + "x" * 501,
                                                               "http_request": {"url": "http://127.0.0.1:9/x"}})),
    attempts=refused(lambda: client.create_queue(parent=location, queue={"name": location + "/queues/q2",
                                                                         "retry_config": {"max_attempts": -2}})))
"""
    )
    heard = await client.heard()
    await client.finished()
    assert refusal(heard, "queue")[1].endswith(wire.QUEUE_NAME_FORMAT)
    assert refusal(heard, "task")[1].endswith(wire.TASK_NAME_FORMAT)
    assert refusal(heard, "attempts")[0] == "BadRequest"


async def test_a_task_created_without_a_name_is_given_an_id_of_digits_that_does_not_run_in_sequence(
    tasks: Tasks,
) -> None:
    """The reference: "If a name is not specified then the system will generate a random unique task id" (tasks.create)."""
    names = [await _create(tasks, '{"http_request": {"url": "http://127.0.0.1:9/x"}}') for _ in range(3)]
    ids = [n.rsplit("/", 1)[1] for n in names]
    assert len(set(ids)) == 3 and all(i.isdigit() and len(i) == 20 for i in ids)
    gaps = {int(b) - int(a) for a, b in pairwise(ids)}
    assert all(abs(g) > 1000 for g in gaps), ids
