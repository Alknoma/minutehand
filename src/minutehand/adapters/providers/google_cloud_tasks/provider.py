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
answer 501, since nothing here holds Google's signing keys; every other method of the API answers 501. A task URL
that is not this machine (127.0.0.1, ::1, localhost) is never called: the attempt is recorded as failed, saying
so, and retried as Cloud Tasks retries a handler that cannot be reached.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, Router

from minutehand.adapters.providers.google_cloud_tasks import wire
from minutehand.adapters.providers.google_cloud_tasks.manifest import MANIFEST
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.errors import Rendered
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, Wakes
from minutehand.ports.store import Store

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
DELIVERY_TIMEOUT = timedelta(seconds=60)
"""Real time a delivery waits for the agent's answer, whatever the task's dispatch deadline says."""


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
    return {"name": q.name, "retryConfig": retry, "state": "RUNNING"}


def _attempt_wire(a: Attempt) -> dict[str, object]:
    found: dict[str, object] = {
        "scheduleTime": wire.stamp(a.schedule_time),
        "dispatchTime": wire.stamp(a.dispatch_time),
    }
    if a.response_status is not None:
        found["responseTime"] = wire.stamp(a.dispatch_time)
        found["responseStatus"] = {
            "code": 0 if 200 <= a.response_status < 300 else 2,
            "message": str(a.response_status),
        }
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
        found["firstAttempt"] = _attempt_wire(t.first_attempt)
    if t.last_attempt is not None:
        found["lastAttempt"] = _attempt_wire(t.last_attempt)
    return found


def _json(payload: object, status: int = 200) -> Response:
    return Response(json.dumps(payload), status_code=status, media_type=wire.JSON)


def _parsed[T: wire.Wire](model: type[T], raw: bytes) -> T:
    try:
        return model.model_validate_json(raw or b"{}")
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise wire.TasksRefusal(400, f"Invalid JSON payload received. {problems}") from e


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


class CloudTasksProvider:
    manifest: Manifest = MANIFEST
    seed_model = CloudTasksSeed

    def __init__(self) -> None:
        self._wakes: Wakes | None = None

    def bind(self, wakes: Wakes) -> None:
        self._wakes = wakes

    def _bound(self) -> Wakes:
        if self._wakes is None:
            raise RuntimeError("the google_cloud_tasks provider books wakes, and bind() was not called before the run")
        return self._wakes

    def error(self, status: int, code: str, message: str) -> Rendered:
        return wire.error_answer(status, "UNIMPLEMENTED" if status == 501 else "INTERNAL", message)

    def seed(self, scenario: Scenario, world: Store) -> None:
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
        headers = {
            **task.headers,
            "User-Agent": "Google-Cloud-Tasks",
            "X-CloudTasks-QueueName": _short(task.queue),
            "X-CloudTasks-TaskName": _short(task.name),
            "X-CloudTasks-TaskRetryCount": str(task.dispatch_count),
            "X-CloudTasks-TaskExecutionCount": str(task.response_count),
            "X-CloudTasks-TaskETA": f"{task.schedule_time.timestamp():.6f}",
        }
        if task.body is not None and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/octet-stream"
        status: int | None = None
        failed: str | None = None
        host = urlsplit(task.url).hostname or ""
        if host not in LOOPBACK:
            failed = f"the task's URL is not this machine ({host}): point the agent's task URLs at its local address"
        else:
            body = base64.b64decode(task.body) if task.body is not None else None
            timeout = min(task.dispatch_deadline, DELIVERY_TIMEOUT).total_seconds()
            try:
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    answer = await client.request(task.method, task.url, headers=headers, content=body)
                status = answer.status_code
            except httpx.HTTPError as e:
                failed = f"{type(e).__name__}: {e}"
        attempt = Attempt(schedule_time=task.schedule_time, dispatch_time=now, response_status=status, failed=failed)
        done = task.model_copy(
            update={
                "dispatch_count": task.dispatch_count + 1,
                "response_count": task.response_count + (status is not None),
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
        out_of_attempts = retry.max_attempts != -1 and task.dispatch_count >= retry.max_attempts
        out_of_time = retry.max_retry_duration is not None and now - first >= retry.max_retry_duration
        if out_of_attempts or out_of_time:
            tasks.put_task(
                task.model_copy(update={"gone_at": now}),
                actor=Actor.SCENARIO,
                operation=Operation.UPDATE,
                said=f"given up after {task.dispatch_count} attempts",
            )
            return
        backoff = min(retry.max_backoff, retry.min_backoff * 2 ** min(task.dispatch_count - 1, retry.max_doublings))
        again = now + backoff
        tasks.put_task(
            task.model_copy(update={"schedule_time": again}),
            actor=Actor.SCENARIO,
            operation=Operation.UPDATE,
            said=f"retry {task.dispatch_count + 1} at {wire.stamp(again)}",
        )
        self._bound().book(Due(at=again, kind=DueKind.AGENT_WAKE, ref=task.name))

    # -- the API ---------------------------------------------------------------------------------------------

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        tasks = Tasks(world)
        location = "/v2/projects/{project}/locations/{location}"

        def located(request: Request) -> str:
            return f"projects/{request.path_params['project']}/locations/{request.path_params['location']}"

        def queue_name(request: Request) -> str:
            return f"{located(request)}/queues/{request.path_params['queue']}"

        def existing_queue(name: str) -> QueueRecord:
            found = tasks.queue(name)
            if found is None:
                raise wire.TasksRefusal(404, "Queue does not exist.")
            return found

        async def create_queue(request: Request) -> Response:
            given = _parsed(wire.QueueIn, await request.body())
            parent = located(request)
            if not given.name.startswith(parent + "/queues/") or "/" in given.name[len(parent) + 8 :]:
                raise wire.TasksRefusal(400, f"Queue name {given.name!r} must be in {parent}")
            if tasks.queue(given.name) is not None:
                raise wire.TasksRefusal(409, "Queue already exists")
            retry = given.retryConfig or wire.RetryConfig()
            record = QueueRecord(
                name=given.name,
                max_attempts=retry.maxAttempts if retry.maxAttempts is not None else wire.DEFAULT_MAX_ATTEMPTS,
                min_backoff=wire.duration(retry.minBackoff) if retry.minBackoff else wire.DEFAULT_MIN_BACKOFF,
                max_backoff=wire.duration(retry.maxBackoff) if retry.maxBackoff else wire.DEFAULT_MAX_BACKOFF,
                max_doublings=retry.maxDoublings if retry.maxDoublings is not None else wire.DEFAULT_MAX_DOUBLINGS,
                max_retry_duration=wire.duration(retry.maxRetryDuration) if retry.maxRetryDuration else None,
            )
            tasks.put_queue(record, actor=Actor.AGENT, operation=Operation.CREATE)
            return _json(_queue_wire(record))

        async def list_queues(request: Request) -> Response:
            return _json({"queues": [_queue_wire(q) for q in tasks.queues(located(request))]})

        async def get_queue(request: Request) -> Response:
            return _json(_queue_wire(existing_queue(queue_name(request))))

        async def delete_queue(request: Request) -> Response:
            found = existing_queue(queue_name(request))
            now = clock.now()
            for task in tasks.tasks(found.name):
                tasks.put_task(
                    task.model_copy(update={"gone_at": now}),
                    actor=Actor.AGENT,
                    operation=Operation.UPDATE,
                    said="deleted with its queue",
                )
                self._bound().cancel(task.name)
            tasks.put_queue(found.model_copy(update={"gone_at": now}), actor=Actor.AGENT, operation=Operation.UPDATE)
            return _json({})

        async def create_task(request: Request) -> Response:
            queue = existing_queue(queue_name(request))
            asked = _parsed(wire.CreateTaskIn, await request.body())
            given = asked.task
            if given.appEngineHttpRequest is not None:
                raise NotImplementedError("App Engine tasks are not served: only HTTP tasks are")
            http = given.httpRequest
            if http is None:
                raise wire.TasksRefusal(400, "Task.http_request or Task.app_engine_http_request must be set")
            if http.oidcToken is not None or http.oauthToken is not None:
                raise NotImplementedError(
                    "a task that asks Cloud Tasks to sign an OIDC or OAuth token is not served: nothing here holds "
                    "Google's signing keys, so the agent could not verify the token it would be sent"
                )
            if urlsplit(http.url).scheme not in ("http", "https"):
                raise wire.TasksRefusal(400, f"HttpRequest.url {http.url!r} must start with http:// or https://")
            if http.body is not None:
                try:
                    base64.b64decode(http.body, validate=True)
                except (binascii.Error, ValueError) as e:
                    raise wire.TasksRefusal(400, "HttpRequest.body is not valid base64") from e
            now = clock.now()
            name = given.name or f"{queue.name}/tasks/{10**18 + world.head():019d}"
            if (
                not name.startswith(queue.name + "/tasks/")
                or not _short(name).replace("-", "").replace("_", "").isalnum()
            ):
                raise wire.TasksRefusal(400, f"Task name {name!r} is not a task of {queue.name}")
            before = tasks.task(name)
            if before is not None and (before.gone_at is None or now - before.gone_at < wire.NAME_REUSE_AFTER):
                raise wire.TasksRefusal(
                    409,
                    "Requested entity already exists"
                    if before.gone_at is None
                    else "The task cannot be created because a task with this name existed too recently.",
                )
            scheduled = max(wire.moment(given.scheduleTime), now) if given.scheduleTime else now
            record = TaskRecord(
                name=name,
                queue=queue.name,
                url=http.url,
                method=_method(http.httpMethod),
                headers=http.headers,
                body=http.body,
                create_time=now,
                schedule_time=scheduled,
                dispatch_deadline=wire.duration(given.dispatchDeadline)
                if given.dispatchDeadline
                else wire.DEFAULT_DISPATCH_DEADLINE,
            )
            tasks.put_task(
                record,
                actor=Actor.AGENT,
                operation=Operation.CREATE if before is None else Operation.UPDATE,
                said=_said(record.method, record.url, record.body),
            )
            self._bound().book(Due(at=scheduled, kind=DueKind.AGENT_WAKE, ref=name))
            return _json(_task_wire(record, full=_full(asked.responseView)))

        async def list_tasks(request: Request) -> Response:
            queue = existing_queue(queue_name(request))
            full = _full(request.query_params.get("responseView"))
            return _json(
                {"tasks": [_task_wire(t, full=full) for t in sorted(tasks.tasks(queue.name), key=lambda t: t.name)]}
            )

        def existing_task(request: Request) -> TaskRecord:
            name = f"{queue_name(request)}/tasks/{request.path_params['task']}"
            found = tasks.task(name)
            if found is None or found.gone_at is not None:
                raise wire.TasksRefusal(
                    404,
                    "The task no longer exists, though a task with this name existed recently. The task either successfully completed or was deleted."
                    if found is not None
                    else "Requested entity was not found.",
                )
            return found

        async def get_task(request: Request) -> Response:
            return _json(_task_wire(existing_task(request), full=_full(request.query_params.get("responseView"))))

        async def delete_task(request: Request) -> Response:
            found = existing_task(request)
            tasks.put_task(
                found.model_copy(update={"gone_at": clock.now()}),
                actor=Actor.AGENT,
                operation=Operation.UPDATE,
                said="deleted",
            )
            self._bound().cancel(found.name)
            return _json({})

        async def unserved(request: Request) -> Response:
            raise NotImplementedError(f"Cloud Tasks {request.method} {request.url.path} is not served")

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


def build() -> CloudTasksProvider:
    return CloudTasksProvider()
