"""Google Cloud Tasks, as a booking source that calls the agent back.

An agent that defers work with Cloud Tasks creates an HTTP task: a URL, a body and a `scheduleTime`. The task's
creation reaches this provider through the proxy, and its `scheduleTime` is booked as a wake on the run's clock
(`ports.provider.BooksWakes`). When the clock reaches it, the task is delivered as Cloud Tasks delivers one: an
HTTP request to the task's URL, carrying the task's headers and body and Cloud Tasks' own `X-CloudTasks-*`
headers. A 2xx answer completes the task; any other answer, or none, is retried with the queue's backoff until
its attempts run out.

What the world log holds: every queue and task is a RECORD entity (`RecordSnapshot`, `resource` "queue" or
"task"), keyed by its Google resource name, a task listed under its queue. What the agent did is actor AGENT;
what Cloud Tasks did (a delivery attempt, a retry booked, a task completed) is actor SCENARIO. A task deleted or
completed keeps its record with `gone_at` set, which is how its name stays taken for an hour. Nothing lives
outside the log.

What it cannot do, loudly: App Engine tasks, and tasks that ask Cloud Tasks to sign an OIDC or OAuth token,
answer 501, since nothing here holds Google's signing keys; every other method of the API (Google's discovery
document for REST, the client's `CloudTasks` service for gRPC) answers 501 (UNIMPLEMENTED), naming it. A task URL
that is not this machine (127.0.0.1, ::1, localhost) is never called: the attempt is recorded as failed, saying
so, and retried as Cloud Tasks retries a handler that cannot be reached. No credential is ever checked. Where each
behaviour comes from is in `CLAIMS.md`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from google.protobuf import json_format
from google.protobuf.message import Message as ProtoMessage
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, Router

from minutehand.adapters.providers.google_cloud_tasks import wire
from minutehand.adapters.providers.google_cloud_tasks.manifest import MANIFEST
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.errors import GrpcRefusal, NotServed, Rendered
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, GrpcMethod, Wakes
from minutehand.ports.store import Store

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
GOOGLE_ONLY = ("x-google-", "x-appengine-")
"""Headers a task may carry that Cloud Tasks does not send: "`X-Google-*`: Google use only. `X-AppEngine-*`: Google
use only" (HttpRequest.headers, "ignored or replaced"). `Host`, `Content-Length` and `User-Agent` are replaced."""
REPLACED = frozenset({"host", "content-length", "user-agent"})


class SeededQueue(Model):
    """A queue that exists when the run starts, as infrastructure creates one before an agent is deployed."""

    name: str = Field(pattern=r"^projects/[^/]+/locations/[^/]+/queues/[A-Za-z0-9-]{1,100}$")
    max_attempts: int = Field(default=wire.DEFAULT_MAX_ATTEMPTS, description="-1: unlimited")
    min_backoff: timedelta = wire.DEFAULT_MIN_BACKOFF
    max_backoff: timedelta = wire.DEFAULT_MAX_BACKOFF
    max_doublings: int = Field(default=wire.DEFAULT_MAX_DOUBLINGS, ge=0)
    max_retry_duration: timedelta | None = None


class CloudTasksSeed(Model):
    """`ProviderSeed.body` for `google_cloud_tasks`."""

    queues: list[SeededQueue] = []


class QueueRecord(Model):
    name: str
    max_attempts: int
    min_backoff: timedelta
    max_backoff: timedelta
    max_doublings: int
    max_retry_duration: timedelta | None
    max_dispatches_per_second: float | None = Field(default=None, description="As sent; None: Google's default")
    max_concurrent_dispatches: int | None = Field(default=None, description="As sent; None: Google's default")
    gone_at: datetime | None = None


class Attempt(Model):
    schedule_time: datetime
    dispatch_time: datetime
    response_status: int | None = Field(description="None: no answer came back")
    failed: str | None = Field(default=None, description="Why no answer came back, when none did")


class TaskRecord(Model):
    name: str
    queue: str
    url: str
    method: str
    headers: dict[str, str]
    body: str | None = Field(description="Base64, as the task carries it")
    create_time: datetime
    schedule_time: datetime
    dispatch_deadline: timedelta
    dispatch_count: int = 0
    response_count: int = 0
    execution_count: int = Field(default=0, description="Answers but 5XX ones: X-CloudTasks-TaskExecutionCount")
    first_attempt: Attempt | None = None
    last_attempt: Attempt | None = None
    gone_at: datetime | None = None


def _queue_ref(name: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=name)


def _task_ref(name: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=name)


def _location(queue: str) -> str:
    return queue.rsplit("/queues/", 1)[0]


def _short(name: str) -> str:
    return name.rsplit("/", 1)[1]


class Tasks:
    """Queues and tasks as the world log holds them."""

    def __init__(self, world: Store) -> None:
        self.world = world

    def queue(self, name: str) -> QueueRecord | None:
        stored = self.world.get(_queue_ref(name))
        if stored is None:
            return None
        found = QueueRecord.model_validate_json(stored.body)
        return found if found.gone_at is None else None

    def queues(self, location: str) -> list[QueueRecord]:
        held = [
            QueueRecord.model_validate_json(s.body)
            for s in self.world.children(MANIFEST.key, EntityKind.RECORD, location, limit=1000)
        ]
        return [q for q in held if q.gone_at is None]

    def task(self, name: str) -> TaskRecord | None:
        stored = self.world.get(_task_ref(name))
        return TaskRecord.model_validate_json(stored.body) if stored is not None else None

    def tasks(self, queue: str) -> list[TaskRecord]:
        held = [
            TaskRecord.model_validate_json(s.body)
            for s in self.world.children(MANIFEST.key, EntityKind.RECORD, queue, limit=1000)
        ]
        return [t for t in held if t.gone_at is None]

    def put_queue(self, queue: QueueRecord, *, actor: Actor, operation: Operation) -> None:
        self.world.apply(
            Change(
                entity=_queue_ref(queue.name),
                operation=operation,
                actor=actor,
                body=queue.model_dump_json(),
                parent=_location(queue.name),
                after=RecordSnapshot(resource="queue", text=queue.name),
            )
        )

    def put_task(self, task: TaskRecord, *, actor: Actor, operation: Operation, said: str) -> None:
        self.world.apply(
            Change(
                entity=_task_ref(task.name),
                operation=operation,
                actor=actor,
                body=task.model_dump_json(),
                parent=task.queue,
                after=RecordSnapshot(resource="task", text=said),
            )
        )


def _queue_wire(q: QueueRecord) -> dict[str, object]:
    retry: dict[str, object] = {
        "maxAttempts": q.max_attempts,
        "minBackoff": wire.seconds(q.min_backoff),
        "maxBackoff": wire.seconds(q.max_backoff),
        "maxDoublings": q.max_doublings,
    }
    if q.max_retry_duration is not None:
        retry["maxRetryDuration"] = wire.seconds(q.max_retry_duration)
    rate: dict[str, object] = {
        "maxDispatchesPerSecond": q.max_dispatches_per_second
        if q.max_dispatches_per_second is not None
        else wire.DEFAULT_MAX_DISPATCHES_PER_SECOND,
        "maxConcurrentDispatches": q.max_concurrent_dispatches
        if q.max_concurrent_dispatches is not None
        else wire.DEFAULT_MAX_CONCURRENT_DISPATCHES,
    }
    if q.max_dispatches_per_second is None:
        # maxBurstSize is output only and "calculated by the system based on the value you set for
        # max_dispatches_per_second"; only the default rate's is documented.
        rate["maxBurstSize"] = wire.DEFAULT_MAX_BURST_SIZE
    return {"name": q.name, "rateLimits": rate, "retryConfig": retry, "state": "RUNNING"}


def _attempt_wire(a: Attempt, *, first: bool) -> dict[str, object]:
    """An Attempt. The first attempt: "Only dispatchTime will be set. The other Attempt information is not retained
    by Cloud Tasks" (Task.firstAttempt). `responseStatus` is left out: how Cloud Tasks turns the handler's HTTP
    status into a google.rpc.Status is in no reference."""
    if first:
        return {"dispatchTime": wire.stamp(a.dispatch_time)}
    found: dict[str, object] = {
        "scheduleTime": wire.stamp(a.schedule_time),
        "dispatchTime": wire.stamp(a.dispatch_time),
    }
    if a.response_status is not None:
        found["responseTime"] = wire.stamp(a.dispatch_time)
    return found


def _task_wire(t: TaskRecord, *, full: bool) -> dict[str, object]:
    http: dict[str, object] = {"url": t.url, "httpMethod": t.method}
    if full:
        http["headers"] = t.headers
        if t.body is not None:
            http["body"] = t.body
    found: dict[str, object] = {
        "name": t.name,
        "httpRequest": http,
        "scheduleTime": wire.stamp(t.schedule_time),
        "createTime": wire.stamp(t.create_time),
        "dispatchDeadline": wire.seconds(t.dispatch_deadline),
        "dispatchCount": t.dispatch_count,
        "responseCount": t.response_count,
        "view": "FULL" if full else "BASIC",
    }
    if t.first_attempt is not None:
        found["firstAttempt"] = _attempt_wire(t.first_attempt, first=True)
    if t.last_attempt is not None:
        found["lastAttempt"] = _attempt_wire(t.last_attempt, first=False)
    return found


def _json(payload: object, status: int = 200) -> Response:
    return Response(json.dumps(payload), status_code=status, media_type=wire.JSON)


def _parsed[T: wire.Wire](model: type[T], raw: bytes) -> T:
    try:
        return model.model_validate_json(raw or b"{}")
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise wire.TasksRefusal(400, f"{wire.INVALID_ARGUMENT} {problems}") from e


def _said(method: str, url: str, body: str | None) -> str:
    text = ""
    if body is not None:
        try:
            text = base64.b64decode(body).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            text = ""
    return f"{method} {url} {text}".strip()


def _method(given: str | int) -> str:
    method = wire.enum_name(given, wire.HTTP_METHODS, "httpMethod")
    return "POST" if method == wire.HTTP_METHODS[0] else method


def _full(view: str | int | None) -> bool:
    """Whether a read asked for the FULL view; a query string carries a number as text."""
    if view is None:
        return False
    given: str | int = int(view) if isinstance(view, str) and view.isdigit() else view
    return wire.enum_name(given, wire.VIEWS, "responseView") == wire.VIEWS[2]


def backoff(retry: QueueRecord, failed: int) -> timedelta:
    """How long after its `failed`-th failed attempt a task is retried: "A task's retry interval starts at
    minBackoff, then doubles maxDoublings times, then increases linearly, and finally retries at intervals of
    maxBackoff ... if minBackoff is 10s, maxBackoff is 300s, and maxDoublings is 3 ... the requests will retry at
    10s, 20s, 40s, 80s, 160s, 240s, 300s, 300s, ...." (RetryConfig.maxDoublings). The linear step is the last
    doubled interval, 2^maxDoublings * minBackoff."""
    k = failed - 1
    doublings = retry.max_doublings
    step = retry.min_backoff * 2**doublings
    interval = retry.min_backoff * 2**k if k <= doublings else step * (1 + k - doublings)
    return min(retry.max_backoff, interval)


def _given_up(retry: QueueRecord, attempts: int, retrying: timedelta, age: timedelta) -> bool:
    """Whether a task that failed its `attempts`-th attempt is retried no more: "Cloud Tasks stops retrying only
    when maxAttempts and maxRetryDuration are both satisfied ... If maxAttempts is set to -1 and maxRetryDuration is
    set to 0, the task is retried until the maximum task retention limit is reached" (RetryConfig.maxAttempts);
    "MAX_RETRY_DURATION still applies even if MAX_ATTEMPTS is reached or set to -1 ... MAX_ATTEMPTS still applies
    even if MAX_RETRY_DURATION is reached or set to 0s" (Configure Cloud Tasks queues). An unlimited bound is
    satisfied, unless both are unlimited; the retention limit is 31 days (Quotas and limits)."""
    unlimited_attempts = retry.max_attempts == -1
    unlimited_duration = retry.max_retry_duration is None or retry.max_retry_duration <= timedelta(0)
    if unlimited_attempts and unlimited_duration:
        return age >= wire.TASK_RETENTION
    attempts_done = unlimited_attempts or attempts >= retry.max_attempts
    duration_done = unlimited_duration or (
        retry.max_retry_duration is not None and retrying >= retry.max_retry_duration
    )
    return attempts_done and duration_done


class CloudTasksProvider:
    manifest: Manifest = MANIFEST
    seed_model = CloudTasksSeed

    def __init__(self, deadline: Callable[[timedelta], float] = timedelta.total_seconds) -> None:
        self._wakes: Wakes | None = None
        self._deadline = deadline
        """The real seconds a delivery waits for the handler's answer, given the task's `dispatchDeadline`: the
        deadline itself, which is the handler's real time. A test hands in a shorter wait to prove the deadline
        without waiting 15 seconds or more."""

    def bind(self, wakes: Wakes) -> None:
        self._wakes = wakes

    def _bound(self) -> Wakes:
        if self._wakes is None:
            raise RuntimeError("the google_cloud_tasks provider books wakes, and bind() was not called before the run")
        return self._wakes

    def error(self, status: int, code: str, message: str) -> Rendered:
        return wire.error_answer(status, "UNIMPLEMENTED" if status == 501 else "INTERNAL", message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        """The queues `CloudTasksSeed` declares. A `SignIn` on this provider is refused: Cloud Tasks has no sign-in
        of its own (a client brings the access token Google's token endpoint mints), so it would be dropped."""
        if any(s.provider == MANIFEST.key for s in scenario.sign_ins):
            raise ValueError(
                f"a seeded sign_in is on {MANIFEST.key}, which has no sign-in of its own: a Cloud Tasks client sends "
                "the access token Google's token endpoint mints, and no credential names a person here"
            )
        given = scenario.provider_seed(MANIFEST.key)
        if given is None:
            return
        tasks = Tasks(world)
        for q in CloudTasksSeed.model_validate_json(given.body).queues:
            record = QueueRecord(
                name=q.name,
                max_attempts=q.max_attempts,
                min_backoff=q.min_backoff,
                max_backoff=q.max_backoff,
                max_doublings=q.max_doublings,
                max_retry_duration=q.max_retry_duration,
            )
            tasks.put_queue(record, actor=Actor.SCENARIO, operation=Operation.CREATE)

    def taken(self, ref: str, world: Store) -> bool:
        """`ConfirmsDelivery`: a delivery is a request the agent answers, so it is taken once the latest attempt is
        over, whatever the answer; a queue the agent polls, by contrast, waits for the agent's delete."""
        task = Tasks(world).task(ref)
        return task is None or task.gone_at is not None or task.last_attempt is not None

    # -- delivering ------------------------------------------------------------------------------------------

    async def deliver_booking(self, ref: str, world: Store, clock: Clock) -> None:
        tasks = Tasks(world)
        task = tasks.task(ref)
        if task is None or task.gone_at is not None:
            raise LookupError(f"no task {ref} in the world: it was deleted or never created")
        now = clock.now()
        kept = {
            k: v for k, v in task.headers.items() if k.lower() not in REPLACED and not k.lower().startswith(GOOGLE_ONLY)
        }
        headers = {
            **kept,
            "User-Agent": "Google-Cloud-Tasks",
            "X-CloudTasks-QueueName": _short(task.queue),
            "X-CloudTasks-TaskName": _short(task.name),
            "X-CloudTasks-TaskRetryCount": str(task.dispatch_count),
            "X-CloudTasks-TaskExecutionCount": str(task.execution_count),
            "X-CloudTasks-TaskETA": f"{task.schedule_time.timestamp():.6f}",
        }
        previous = task.last_attempt.response_status if task.last_attempt is not None else None
        if previous is not None:
            headers["X-CloudTasks-TaskPreviousResponse"] = str(previous)
        status: int | None = None
        failed: str | None = None
        host = urlsplit(task.url).hostname or ""
        if host not in LOOPBACK:
            failed = f"the task's URL is not this machine ({host}): point the agent's task URLs at its local address"
        else:
            body = base64.b64decode(task.body) if task.body is not None else None
            timeout = self._deadline(task.dispatch_deadline)
            try:
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    answer = await client.request(task.method, task.url, headers=headers, content=body)
                status = answer.status_code
            except httpx.TimeoutException:
                # "If the worker does not respond by this deadline then the request is cancelled and the attempt is
                # marked as a DEADLINE_EXCEEDED failure" (Task.dispatchDeadline).
                failed = f"DEADLINE_EXCEEDED: no answer within the task's dispatchDeadline of {task.dispatch_deadline}"
            except httpx.HTTPError as e:
                failed = f"{type(e).__name__}: {e}"
        attempt = Attempt(schedule_time=task.schedule_time, dispatch_time=now, response_status=status, failed=failed)
        done = task.model_copy(
            update={
                "dispatch_count": task.dispatch_count + 1,
                "response_count": task.response_count + (status is not None),
                "execution_count": task.execution_count + (status is not None and status < 500),
                "first_attempt": task.first_attempt or attempt,
                "last_attempt": attempt,
            }
        )
        said = f"delivered: {status}" if status is not None else f"not delivered: {failed}"
        tasks.put_task(done, actor=Actor.SCENARIO, operation=Operation.UPDATE, said=said)

    async def advance_booking(self, ref: str, world: Store, clock: Clock) -> None:
        tasks = Tasks(world)
        task = tasks.task(ref)
        if task is None or task.gone_at is not None:
            return
        now = clock.now()
        last = task.last_attempt
        this_one = last is not None and last.schedule_time == task.schedule_time
        answered = (
            this_one and last is not None and last.response_status is not None and 200 <= last.response_status < 300
        )
        if answered or not this_one:
            # handled, or dropped before any attempt: the task is over
            tasks.put_task(
                task.model_copy(update={"gone_at": now}),
                actor=Actor.SCENARIO,
                operation=Operation.UPDATE,
                said="completed" if answered else "completed undelivered",
            )
            return
        queue = tasks.queue(task.queue)
        retry = queue or QueueRecord(
            name=task.queue,
            max_attempts=wire.DEFAULT_MAX_ATTEMPTS,
            min_backoff=wire.DEFAULT_MIN_BACKOFF,
            max_backoff=wire.DEFAULT_MAX_BACKOFF,
            max_doublings=wire.DEFAULT_MAX_DOUBLINGS,
            max_retry_duration=None,
        )
        first = task.first_attempt.dispatch_time if task.first_attempt is not None else now
        if _given_up(retry, task.dispatch_count, now - first, now - task.create_time):
            tasks.put_task(
                task.model_copy(update={"gone_at": now}),
                actor=Actor.SCENARIO,
                operation=Operation.UPDATE,
                said=f"given up after {task.dispatch_count} attempts",
            )
            return
        again = now + backoff(retry, task.dispatch_count)
        tasks.put_task(
            task.model_copy(update={"schedule_time": again}),
            actor=Actor.SCENARIO,
            operation=Operation.UPDATE,
            said=f"retry {task.dispatch_count + 1} at {wire.stamp(again)}",
        )
        self._bound().book(Due(at=again, kind=DueKind.AGENT_WAKE, ref=task.name))

    # -- the API ---------------------------------------------------------------------------------------------

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        api = _Api(Tasks(world), world, clock, self._bound)
        location = "/v2/projects/{project}/locations/{location}"

        def located(request: Request) -> str:
            return f"projects/{request.path_params['project']}/locations/{request.path_params['location']}"

        def queue_name(request: Request) -> str:
            return f"{located(request)}/queues/{request.path_params['queue']}"

        def task_name(request: Request) -> str:
            return f"{queue_name(request)}/tasks/{request.path_params['task']}"

        async def create_queue(request: Request) -> Response:
            return _json(_queue_wire(api.create_queue(located(request), _parsed(wire.QueueIn, await request.body()))))

        def paging(request: Request) -> wire.Parented:
            query = request.query_params
            if "filter" in query or "readMask" in query:
                raise NotServed("queues.list with a filter or a readMask is not served")
            size = query.get("pageSize")
            return wire.Parented(
                parent=located(request),
                pageSize=int(size) if size is not None and size.lstrip("-").isdigit() else None,
                pageToken=query.get("pageToken"),
            )

        async def list_queues(request: Request) -> Response:
            asked = paging(request)
            listed, following = api.list_queues(asked.parent, asked.pageSize, asked.pageToken)
            return _json(_page("queues", [_queue_wire(q) for q in listed], following))

        async def get_queue(request: Request) -> Response:
            return _json(_queue_wire(api.get_queue(queue_name(request))))

        async def delete_queue(request: Request) -> Response:
            api.delete_queue(queue_name(request))
            return _json({})

        async def create_task(request: Request) -> Response:
            asked = _parsed(wire.CreateTaskIn, await request.body())
            return _json(_task_wire(api.create_task(queue_name(request), asked), full=_full(asked.responseView)))

        async def list_tasks(request: Request) -> Response:
            query = request.query_params
            full = _full(query.get("responseView"))
            size = query.get("pageSize")
            listed, following = api.list_tasks(
                queue_name(request),
                int(size) if size is not None and size.lstrip("-").isdigit() else None,
                query.get("pageToken"),
            )
            return _json(_page("tasks", [_task_wire(t, full=full) for t in listed], following))

        async def get_task(request: Request) -> Response:
            found = api.get_task(task_name(request))
            return _json(_task_wire(found, full=_full(request.query_params.get("responseView"))))

        async def delete_task(request: Request) -> Response:
            api.delete_task(task_name(request))
            return _json({})

        async def unserved(request: Request) -> Response:
            called = wire.rest_method(request.method, request.url.path)
            if called is None:
                raise NotServed(f"no method of Cloud Tasks v2 has the route {request.method} {request.url.path}")
            raise NotServed(f"{called}: {wire.REST_REFUSED[called]}")

        return Router(
            routes=[
                Route(f"{location}/queues", create_queue, methods=["POST"]),
                Route(f"{location}/queues", list_queues, methods=["GET"]),
                Route(f"{location}/queues/{{queue}}/tasks", create_task, methods=["POST"]),
                Route(f"{location}/queues/{{queue}}/tasks", list_tasks, methods=["GET"]),
                Route(f"{location}/queues/{{queue}}/tasks/{{task}}", get_task, methods=["GET"]),
                Route(f"{location}/queues/{{queue}}/tasks/{{task}}", delete_task, methods=["DELETE"]),
                Route(f"{location}/queues/{{queue}}", get_queue, methods=["GET"]),
                Route(f"{location}/queues/{{queue}}", delete_queue, methods=["DELETE"]),
                Route("/{rest:path}", unserved, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
            ]
        )

    def grpc(self, world: Store, clock: Clock) -> list[GrpcMethod]:
        """The same API over gRPC, the transport `CloudTasksClient()` speaks by default, answered by the same
        operations as the REST routes over the same world: each request is read as the proto3 JSON its REST call
        carries, and each answer written as the message the REST answer's JSON parses to. Google's messages come
        from `google-cloud-tasks` (`minutehand[grpc]`)."""
        from google.cloud.tasks_v2.types import cloudtasks, queue, task
        from google.protobuf import empty_pb2

        api = _Api(Tasks(world), world, clock, self._bound)

        def method[In: wire.Wire](
            name: str, request: type[ProtoMessage], read: type[In], answer: Callable[[In], ProtoMessage]
        ) -> GrpcMethod:
            async def answered(message: ProtoMessage) -> ProtoMessage:
                try:
                    return answer(_parsed(read, json_format.MessageToJson(message, indent=None).encode()))
                except wire.TasksRefusal as refusal:
                    raise GrpcRefusal(refusal.grpc_code(), refusal.message) from refusal

            return GrpcMethod(path=f"{SERVICE}/{name}", request=request, answer=answered)

        def as_queue(record: QueueRecord) -> ProtoMessage:
            return json_format.Parse(json.dumps(_queue_wire(record)), queue.Queue.pb()())

        def as_task(record: TaskRecord, view: str | int | None) -> ProtoMessage:
            return json_format.Parse(json.dumps(_task_wire(record, full=_full(view))), task.Task.pb()())

        def tasks_listed(asked: wire.TasksListed) -> ProtoMessage:
            found, following = api.list_tasks(_named(asked.parent), asked.pageSize, asked.pageToken)
            listed = [_task_wire(t, full=_full(asked.responseView)) for t in found]
            return json_format.Parse(json.dumps(_page("tasks", listed, following)), cloudtasks.ListTasksResponse.pb()())

        def queues_listed(asked: wire.Parented) -> ProtoMessage:
            if asked.filter or asked.readMask:
                raise NotServed("ListQueues with a filter or a read_mask is not served")
            found, following = api.list_queues(_located(asked.parent), asked.pageSize, asked.pageToken)
            listed = [_queue_wire(q) for q in found]
            return json_format.Parse(
                json.dumps(_page("queues", listed, following)), cloudtasks.ListQueuesResponse.pb()()
            )

        def refused(name: str, request: type[ProtoMessage]) -> GrpcMethod:
            async def answered(message: ProtoMessage) -> ProtoMessage:
                raise NotServed(f"google.cloud.tasks.v2.CloudTasks/{name}: {wire.GRPC_REFUSED[name]}")

            return GrpcMethod(path=f"{SERVICE}/{name}", request=request, answer=answered)

        from google.iam.v1 import iam_policy_pb2

        unserved: dict[str, type[ProtoMessage]] = {
            "UpdateQueue": cloudtasks.UpdateQueueRequest.pb(),
            "PurgeQueue": cloudtasks.PurgeQueueRequest.pb(),
            "PauseQueue": cloudtasks.PauseQueueRequest.pb(),
            "ResumeQueue": cloudtasks.ResumeQueueRequest.pb(),
            "GetIamPolicy": iam_policy_pb2.GetIamPolicyRequest,
            "SetIamPolicy": iam_policy_pb2.SetIamPolicyRequest,
            "TestIamPermissions": iam_policy_pb2.TestIamPermissionsRequest,
            "RunTask": cloudtasks.RunTaskRequest.pb(),
            "BatchCreateTasks": cloudtasks.BatchCreateTasksRequest.pb(),
            "BatchDeleteTasks": cloudtasks.BatchDeleteTasksRequest.pb(),
            "GetCmekConfig": cloudtasks.GetCmekConfigRequest.pb(),
            "UpdateCmekConfig": cloudtasks.UpdateCmekConfigRequest.pb(),
        }

        def queue_deleted(asked: wire.Named) -> ProtoMessage:
            api.delete_queue(_named(asked.name))
            return empty_pb2.Empty()

        def task_deleted(asked: wire.Named) -> ProtoMessage:
            api.delete_task(_task_named(asked.name))
            return empty_pb2.Empty()

        return [
            method(
                "CreateQueue",
                cloudtasks.CreateQueueRequest.pb(),
                wire.QueueCreated,
                lambda asked: as_queue(api.create_queue(_located(asked.parent), asked.queue)),
            ),
            method("ListQueues", cloudtasks.ListQueuesRequest.pb(), wire.Parented, queues_listed),
            method(
                "GetQueue",
                cloudtasks.GetQueueRequest.pb(),
                wire.Named,
                lambda asked: as_queue(api.get_queue(_named(asked.name))),
            ),
            method("DeleteQueue", cloudtasks.DeleteQueueRequest.pb(), wire.Named, queue_deleted),
            method(
                "CreateTask",
                cloudtasks.CreateTaskRequest.pb(),
                wire.TaskCreated,
                lambda asked: as_task(api.create_task(_named(asked.parent), asked.as_rest()), asked.responseView),
            ),
            method("ListTasks", cloudtasks.ListTasksRequest.pb(), wire.TasksListed, tasks_listed),
            method(
                "GetTask",
                cloudtasks.GetTaskRequest.pb(),
                wire.TaskNamed,
                lambda asked: as_task(api.get_task(_task_named(asked.name)), asked.responseView),
            ),
            method("DeleteTask", cloudtasks.DeleteTaskRequest.pb(), wire.Named, task_deleted),
            *(refused(name, request) for name, request in unserved.items()),
        ]


SERVICE = "/google.cloud.tasks.v2.CloudTasks"
_QUEUE_ID = re.compile(r"[A-Za-z0-9-]{1,100}")
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,500}")


def _generated_id(queue: str, head: int) -> str:
    """The id Cloud Tasks gives a task created without a name: "a random unique task id" (tasks.create). Drawn from
    the queue and the log's position, so it is unique in the run and the same when the run is replayed; a string
    of digits, of the documented TASK_ID characters."""
    return str(int.from_bytes(hashlib.sha256(f"{queue}@{head}".encode()).digest()[:8], "big")).zfill(20)


_LOCATION = re.compile(r"projects/[^/]+/locations/[^/]+")
_QUEUE = re.compile(r"projects/[^/]+/locations/[^/]+/queues/[^/]+")
_TASK = re.compile(r"projects/[^/]+/locations/[^/]+/queues/[^/]+/tasks/[^/]+")


def _checked(name: str, pattern: re.Pattern[str], rule: str) -> str:
    """A resource name a gRPC request carries whole, which a REST call spells out in its path: one not of its
    documented format is INVALID_ARGUMENT, which google.rpc.Code gives for "arguments that are problematic
    regardless of the state of the system (e.g., a malformed file name)"."""
    if pattern.fullmatch(name) is None:
        raise wire.TasksRefusal(400, f"{wire.INVALID_ARGUMENT} {name!r}: {rule}")
    return name


def _located(name: str) -> str:
    return _checked(name, _LOCATION, wire.LOCATION_NAME_FORMAT)


def _named(name: str) -> str:
    return _checked(name, _QUEUE, wire.QUEUE_NAME_FORMAT)


def _task_named(name: str) -> str:
    return _checked(name, _TASK, wire.TASK_NAME_FORMAT)


def _page(field: str, listed: list[dict[str, object]], following: str | None) -> dict[str, object]:
    found: dict[str, object] = {field: listed}
    if following is not None:
        found["nextPageToken"] = following
    return found


def _paged[T](
    items: list[T], key: Callable[[T], str], size: int | None, token: str | None, most: int
) -> tuple[list[T], str | None]:
    """One page of `items` (sorted by `key`): at most `size`, or the method's maximum when it is left out or asks
    for more ("If unspecified, the page size will be the maximum"; "Fewer ... than requested might be returned"),
    after the item `token` names. The token is this provider's: the key of the page's last item, base64."""
    if size is not None and size < 0:
        raise wire.TasksRefusal(400, f"{wire.INVALID_ARGUMENT} pageSize: {size}")
    limit = min(size or most, most)
    after = ""
    if token:
        try:
            after = base64.urlsafe_b64decode(token.encode()).decode()
        except (binascii.Error, UnicodeDecodeError) as e:
            raise wire.TasksRefusal(400, f"{wire.INVALID_ARGUMENT} pageToken: {token!r}") from e
    rest = [item for item in items if key(item) > after]
    page = rest[:limit]
    following = base64.urlsafe_b64encode(key(page[-1]).encode()).decode() if len(rest) > limit else None
    return page, following


class _Api:
    """Cloud Tasks' operations over the world, whatever transport asked: the REST routes and the gRPC methods both
    answer from here. Each raises `wire.TasksRefusal` where Cloud Tasks refuses and `NotServed` for what
    is not served."""

    def __init__(self, tasks: Tasks, world: Store, clock: Clock, wakes: Callable[[], Wakes]) -> None:
        self._tasks = tasks
        self._world = world
        self._clock = clock
        self._wakes = wakes

    def existing_queue(self, name: str) -> QueueRecord:
        found = self._tasks.queue(name)
        if found is None:
            raise wire.TasksRefusal(404, f"{name}: {wire.QUEUE_MUST_EXIST}")
        return found

    def create_queue(self, parent: str, given: wire.QueueIn) -> QueueRecord:
        if _QUEUE_ID.fullmatch(given.name.removeprefix(parent + "/queues/")) is None or not given.name.startswith(
            parent + "/queues/"
        ):
            raise wire.TasksRefusal(
                400, f"{wire.INVALID_ARGUMENT} {given.name!r} in {parent}: {wire.QUEUE_NAME_FORMAT}"
            )
        if self._tasks.queue(given.name) is not None:
            raise wire.TasksRefusal(409, f"{given.name}: {wire.QUEUE_EXISTS}")
        stored = self._world.get(_queue_ref(given.name))
        if stored is not None:
            deleted = QueueRecord.model_validate_json(stored.body).gone_at
            if deleted is not None and self._clock.now() - deleted < wire.QUEUE_TOMBSTONE:
                raise NotServed(
                    f"creating {given.name} within 3 days of deleting it: the reference says only that queues.create "
                    "may appear to recreate the queue during this tombstone window"
                )
        retry = given.retryConfig or wire.RetryConfig()
        rate = given.rateLimits or wire.RateLimits()
        record = QueueRecord(
            name=given.name,
            max_attempts=retry.maxAttempts if retry.maxAttempts is not None else wire.DEFAULT_MAX_ATTEMPTS,
            min_backoff=wire.duration(retry.minBackoff) if retry.minBackoff else wire.DEFAULT_MIN_BACKOFF,
            max_backoff=wire.duration(retry.maxBackoff) if retry.maxBackoff else wire.DEFAULT_MAX_BACKOFF,
            max_doublings=retry.maxDoublings if retry.maxDoublings is not None else wire.DEFAULT_MAX_DOUBLINGS,
            max_retry_duration=wire.duration(retry.maxRetryDuration) if retry.maxRetryDuration else None,
            max_dispatches_per_second=rate.maxDispatchesPerSecond,
            max_concurrent_dispatches=rate.maxConcurrentDispatches,
        )
        if record.max_attempts < -1:
            raise wire.TasksRefusal(
                400, f"{wire.INVALID_ARGUMENT} maxAttempts {record.max_attempts}: must be greater than or equal to -1"
            )
        self._tasks.put_queue(
            record, actor=Actor.AGENT, operation=Operation.CREATE if stored is None else Operation.UPDATE
        )
        return record

    def list_queues(self, parent: str, size: int | None, token: str | None) -> tuple[list[QueueRecord], str | None]:
        found = sorted(self._tasks.queues(parent), key=lambda q: q.name)
        return _paged(found, lambda q: q.name, size, token, wire.MAX_QUEUE_PAGE)

    def get_queue(self, name: str) -> QueueRecord:
        return self.existing_queue(name)

    def delete_queue(self, name: str) -> None:
        found = self.existing_queue(name)
        now = self._clock.now()
        for task in self._tasks.tasks(found.name):
            self._tasks.put_task(
                task.model_copy(update={"gone_at": now}),
                actor=Actor.AGENT,
                operation=Operation.UPDATE,
                said="deleted with its queue",
            )
            self._wakes().cancel(task.name)
        self._tasks.put_queue(found.model_copy(update={"gone_at": now}), actor=Actor.AGENT, operation=Operation.UPDATE)

    def create_task(self, queue_name: str, asked: wire.CreateTaskIn) -> TaskRecord:
        queue = self.existing_queue(queue_name)
        given = asked.task
        if given.appEngineHttpRequest is not None:
            raise NotServed("App Engine tasks are not served: only HTTP tasks are")
        http = given.httpRequest
        if http is None:
            raise wire.TasksRefusal(400, f"{wire.INVALID_ARGUMENT} task: {wire.MESSAGE_RULE}")
        if http.oidcToken is not None or http.oauthToken is not None:
            raise NotServed(
                "a task that asks Cloud Tasks to sign an OIDC or OAuth token is not served: nothing here holds "
                "Google's signing keys, so the agent could not verify the token it would be sent"
            )
        if urlsplit(http.url).scheme not in ("http", "https"):
            raise wire.TasksRefusal(400, f"{wire.INVALID_ARGUMENT} url {http.url!r}: {wire.URL_RULE}")
        method = _method(http.httpMethod)
        if http.body is not None:
            if method not in ("POST", "PUT", "PATCH"):
                raise wire.TasksRefusal(400, f"{wire.INVALID_ARGUMENT} body with {method}: {wire.BODY_RULE}")
            try:
                base64.b64decode(http.body, validate=True)
            except (binascii.Error, ValueError) as e:
                raise wire.TasksRefusal(400, f"{wire.INVALID_ARGUMENT} body: a base64-encoded string") from e
        deadline = wire.duration(given.dispatchDeadline) if given.dispatchDeadline else wire.DEFAULT_DISPATCH_DEADLINE
        low, high = wire.DISPATCH_DEADLINE_RANGE
        if not low <= deadline <= high:
            raise wire.TasksRefusal(
                400, f"{wire.INVALID_ARGUMENT} dispatchDeadline {given.dispatchDeadline}: {wire.DEADLINE_RULE}"
            )
        now = self._clock.now()
        name = given.name or f"{queue.name}/tasks/{_generated_id(queue.name, self._world.head())}"
        if not name.startswith(queue.name + "/tasks/") or _TASK_ID.fullmatch(_short(name)) is None:
            raise wire.TasksRefusal(400, f"{wire.INVALID_ARGUMENT} {name!r} in {queue.name}: {wire.TASK_NAME_FORMAT}")
        before = self._tasks.task(name)
        if before is not None and (before.gone_at is None or now - before.gone_at < wire.NAME_REUSE_AFTER):
            raise wire.TasksRefusal(409, f"{name}: {wire.TASK_EXISTS}")
        scheduled = max(wire.moment(given.scheduleTime), now) if given.scheduleTime else now
        record = TaskRecord(
            name=name,
            queue=queue.name,
            url=http.url,
            method=method,
            headers=http.headers,
            body=http.body,
            create_time=now.replace(microsecond=0),
            schedule_time=scheduled,
            dispatch_deadline=deadline,
        )
        self._tasks.put_task(
            record,
            actor=Actor.AGENT,
            operation=Operation.CREATE if before is None else Operation.UPDATE,
            said=_said(record.method, record.url, record.body),
        )
        self._wakes().book(Due(at=scheduled, kind=DueKind.AGENT_WAKE, ref=name))
        return record

    def list_tasks(self, queue_name: str, size: int | None, token: str | None) -> tuple[list[TaskRecord], str | None]:
        queue = self.existing_queue(queue_name)
        found = sorted(self._tasks.tasks(queue.name), key=lambda t: t.name)
        return _paged(found, lambda t: t.name, size, token, wire.MAX_TASK_PAGE)

    def get_task(self, name: str) -> TaskRecord:
        found = self._tasks.task(name)
        if found is None or found.gone_at is not None:
            raise wire.TasksRefusal(404, f"{name}: {wire.NOT_FOUND}")
        return found

    def delete_task(self, name: str) -> None:
        found = self.get_task(name)
        self._tasks.put_task(
            found.model_copy(update={"gone_at": self._clock.now()}),
            actor=Actor.AGENT,
            operation=Operation.UPDATE,
            said="deleted",
        )
        self._wakes().cancel(found.name)


def build() -> CloudTasksProvider:
    return CloudTasksProvider()
