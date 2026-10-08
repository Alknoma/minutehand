"""AWS, answered by moto mounted in this process, with EventBridge Scheduler as a booking source.

An agent that books its own wake-ups with EventBridge Scheduler needs no adapter:
its CreateSchedule reaches this provider through the proxy, moto stores the
schedule, and the provider books the schedule's next occurrence as a `Due` on the
run's clock. When the clock reaches it, `fire` delivers the schedule's input to its
SQS target the way AWS would, where the agent's own ReceiveMessage finds it.

What the world log holds, and what it does not:

- Every booking, update, cancellation and delivery is a `RecordSnapshot` on a
  RECORD entity in the run's `Store`: `resource="schedule"` (keyed by the schedule
  ARN, the same string as the `Due.ref`) and `resource="queue_message"` (keyed by
  the SQS message id, listed under the schedule that delivered it). What the agent
  did is `actor=AGENT`; what the scheduler did (a delivery, a re-booking, a delete
  after completion) is `actor=SCENARIO`. The schedule record carries `next_at`, so
  what is pending is in the log too.
- **A delivery is taken when the agent deletes its message** (`DeleteMessage` or
  `DeleteMessageBatch`, either SQS protocol): the delivery's record is deleted as
  the agent's act, and `taken` (`ConfirmsDelivery`) answers True once nothing the
  schedule delivered is still live. Receiving is not taking.
- **moto keeps AWS's own state in process memory, outside the `Store`.** Queues,
  their messages, and moto's copy of each schedule are not in the log, and each
  run's app takes a fresh account (below), so a fork cannot reach them at all: the
  schedule record it shares with its parent names the parent's account, whose
  queues live only in the memory of the process that played the parent. The
  manifest says so (`state_outside_log`), and a fork of a run that used this
  provider before the fork's seq is therefore refused before it starts, naming
  it (`application.rewind`); `fire` raises `LookupError` on a schedule targeting
  another account. moto's SQS backend holds a `threading.RLock` and cannot be
  pickled or deep-copied, so there is no per-run snapshot to restore instead.
- **moto reads the machine clock.** SQS DelaySeconds, VisibilityTimeout,
  MessageRetentionPeriod, long-poll WaitTimeSeconds and every Sent/Creation
  timestamp run on real time, not the run's clock (a schedule's CreationDate and
  LastModificationDate are restamped from the run's clock, `_stamp`); and moto refuses a cron
  schedule whose StartDate is more than five minutes before the machine's now,
  which is every StartDate taken from a simulated clock in the past.

Isolation: moto's state is global to the process, so each provider instance takes
a fresh AWS account id when its app is made, and every request is answered in that
account (moto's `x-moto-account-id`, replacing any the caller sent). The id is
random, so a rerun of the same run sees a different account and different queue
URLs. Setting `MOTO_ACCOUNT_ID` would pin every instance to one account, so
building refuses it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable

from asgiref.wsgi import WsgiToAsgi
from moto.moto_server.werkzeug_app import DomainDispatcherApplication, create_backend_app
from moto.scheduler.models import scheduler_backends
from moto.sqs.models import Queue, sqs_backends

from minutehand.adapters import answering
from minutehand.adapters.providers.aws.manifest import MANIFEST
from minutehand.adapters.providers.aws.schedule import ScheduleRecord
from minutehand.adapters.providers.aws.wire import (
    ActionAfterCompletion,
    CallKind,
    NotImplementedByProvider,
    Refusal,
    ScheduleCall,
    SqsDelete,
    schedule_arn,
    schedule_call,
    sqs_delete,
    sqs_target,
)
from minutehand.adapters.providers.aws.wire import error as aws_error
from minutehand.domain.clock import Due, DueKind
from minutehand.domain.errors import NotServed, Rendered
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp, Message, Scope, Wakes
from minutehand.ports.store import Store

Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class QueueMessageRecord(Model):
    """One message a fired schedule put on a queue."""

    queue_arn: str
    message_id: str
    body: str
    message_group_id: str | None
    schedule_arn: str


def fresh_account() -> str:
    """A twelve-digit AWS account id nothing else in this process has used."""
    return str(uuid.uuid4().int % 10**12).zfill(12)


class AwsProvider:
    manifest: Manifest = MANIFEST

    def __init__(self) -> None:
        self._wakes: Wakes | None = None
        self._account: str | None = None

    @property
    def account(self) -> str:
        if self._account is None:
            raise RuntimeError("the aws provider has no account until app() is called for a run")
        return self._account

    def bind(self, wakes: Wakes) -> None:
        self._wakes = wakes

    def error(self, status: int, code: str, message: str) -> Rendered:
        return aws_error(status, code, message)

    def seed(self, scenario: Scenario, world: Store) -> None:
        """A scenario declares no AWS resources: the agent creates its own queues and schedules."""

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        self._account = fresh_account()
        # moto builds a boto3 client to read each service's model. Building one resolves
        # credentials, and with none configured botocore goes looking on the network (the
        # instance-metadata address). This process must never use, or look for, real AWS
        # credentials, so it is given dummy ones before moto builds anything.
        os.environ.update(
            AWS_ACCESS_KEY_ID="minutehand",
            AWS_SECRET_ACCESS_KEY="minutehand",
            AWS_SESSION_TOKEN="minutehand",
            AWS_EC2_METADATA_DISABLED="true",
        )
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        moto: ASGIApp = WsgiToAsgi(DomainDispatcherApplication(create_backend_app))
        account = self._account.encode()

        async def serve(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                raise NotServed(f"the aws provider serves HTTP only, not {scope['type']}")
            body = await _read_body(receive)
            headers = _headers(scope)
            try:
                call = schedule_call(
                    str(scope["method"]),
                    _header(headers, b"host"),
                    str(scope["path"]),
                    _query(scope),
                    body,
                )
            except Refusal as refused:
                if isinstance(refused, NotImplementedByProvider):
                    answering.unimplemented(refused, refused.message)
                await _respond(
                    send,
                    refused.status,
                    [(b"x-amzn-errortype", refused.error_type.encode()), (b"content-type", b"application/json")],
                    refused.body(),
                )
                return
            deleting = sqs_delete(_header(headers, b"host"), _header(headers, b"x-amz-target"), body)
            deliveries = self._deliveries(deleting, world) if deleting is not None else []
            forwarded = dict(scope)
            forwarded["headers"] = [(k, v) for k, v in headers if k != b"x-moto-account-id"] + [
                (b"x-moto-account-id", account)
            ]
            status, response_headers, response_body = await _call(moto, forwarded, body)
            if call is not None and 200 <= status < 300:
                self._record(call, world, clock)
            if deleting is not None and 200 <= status < 300:
                self._taken(deleting, deliveries, world)
            await _respond(send, status, response_headers, response_body)

        return serve

    def _record(self, call: ScheduleCall, world: Store, clock: Clock) -> None:
        wakes = self._bound()
        arn = schedule_arn(call.region, self.account, call.group, call.name)
        entity = _schedule_entity(arn)
        if call.kind is CallKind.DELETE or call.request is None:
            wakes.cancel(arn)
            world.apply(Change(entity=entity, operation=Operation.DELETE, actor=Actor.AGENT, parent=call.group))
            return
        record = ScheduleRecord.of(
            call.request,
            arn=arn,
            name=call.name,
            group=call.group,
            region=call.region,
            account=self.account,
            now=clock.now(),
        )
        self._stamp(call, clock)
        if call.kind is CallKind.UPDATE:
            wakes.cancel(arn)
        if record.next_at is not None:
            wakes.book(Due(at=record.next_at, kind=DueKind.AGENT_WAKE, ref=arn))
        operation = Operation.CREATE if call.kind is CallKind.CREATE else Operation.UPDATE
        world.apply(_schedule_change(record, operation, Actor.AGENT))

    def _stamp(self, call: ScheduleCall, clock: Clock) -> None:
        """moto stamps a schedule's CreationDate and LastModificationDate from the machine clock; both are
        restamped in moto's copy from the run's clock as the create or update lands, so every later GetSchedule
        and ListSchedules answers the world's time."""
        made = scheduler_backends[self.account][call.region].get_schedule(call.group, call.name)
        at = clock.now().timestamp()
        made.last_modified_date = at
        if call.kind is CallKind.CREATE:
            made.creation_date = at

    def _deliveries(self, deleting: SqsDelete, world: Store) -> list[QueueMessageRecord]:
        """The delivered messages still live in the world that these receipt handles name. Read before moto
        answers the delete, since a receipt handle names nothing once its message is gone."""
        queue = self._queue(deleting)
        if queue is None:
            return []
        # moto keeps in-flight messages only in `_messages`; its public `messages` lists the visible ones, and a
        # message the agent has received is exactly one that is not visible.
        ids = {m.id for m in queue._messages if any(m.had_receipt_handle(h) for h in deleting.receipt_handles)}
        found: list[QueueMessageRecord] = []
        for message_id in sorted(ids):
            stored = world.get(_message_entity(message_id))
            if stored is not None:
                found.append(QueueMessageRecord.model_validate_json(stored.body))
        return found

    def _taken(self, deleting: SqsDelete, deliveries: list[QueueMessageRecord], world: Store) -> None:
        """Each delivered message the delete removed from its queue is gone from the world too, as the agent's
        act: the booking that delivered it has been taken."""
        queue = self._queue(deleting)
        remaining = {m.id for m in queue._messages} if queue is not None else set()
        for delivery in deliveries:
            if delivery.message_id in remaining:
                continue
            world.apply(
                Change(
                    entity=_message_entity(delivery.message_id),
                    operation=Operation.DELETE,
                    actor=Actor.AGENT,
                    parent=delivery.schedule_arn,
                )
            )

    def _queue(self, deleting: SqsDelete) -> Queue | None:
        queues = sqs_backends[self.account][deleting.region].queues
        return queues[deleting.queue] if deleting.queue in queues else None

    def taken(self, ref: str, world: Store) -> bool:
        """`ConfirmsDelivery`: everything schedule `ref` delivered has been deleted from its queue by the agent.
        A message merely received is not taken: it is in flight, and returns to the queue if the agent fails."""
        return not world.children(MANIFEST.key, EntityKind.RECORD, ref, limit=1)

    def _due(self, ref: str, world: Store) -> ScheduleRecord:
        stored = world.get(_schedule_entity(ref))
        if stored is None:
            raise LookupError(f"no schedule {ref} in the world: it was deleted or never created")
        record = ScheduleRecord.model_validate_json(stored.body)
        if record.next_at is None:
            raise RuntimeError(f"schedule {ref} fired with nothing booked: it is disabled or complete")
        return record

    async def deliver_booking(self, ref: str, world: Store, clock: Clock) -> None:
        record = self._due(ref, world)
        queue = sqs_target(record.target_arn)
        if queue.account != self.account:
            raise LookupError(f"schedule {ref} targets account {queue.account}; this run is account {self.account}")
        if record.target_input is None:
            raise ValueError(f"schedule {ref} has no Target.Input to put on {queue.arn}")
        message = sqs_backends[self.account][queue.region].send_message(
            queue.queue,
            record.target_input,
            group_id=record.message_group_id,
        )
        delivered = QueueMessageRecord(
            queue_arn=queue.arn,
            message_id=message.id,
            body=record.target_input,
            message_group_id=record.message_group_id,
            schedule_arn=ref,
        )
        world.apply(
            Change(
                entity=_message_entity(message.id),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                body=delivered.model_dump_json(),
                parent=ref,
                after=RecordSnapshot(resource="queue_message", text=record.target_input),
            )
        )

    async def advance_booking(self, ref: str, world: Store, clock: Clock) -> None:
        record = self._due(ref, world)
        assert record.next_at is not None
        following = record.occurrence_after(max(clock.now(), record.next_at))
        if following is not None:
            self._bound().book(Due(at=following, kind=DueKind.AGENT_WAKE, ref=ref))
            world.apply(
                _schedule_change(record.model_copy(update={"next_at": following}), Operation.UPDATE, Actor.SCENARIO)
            )
            return
        if record.action_after_completion is ActionAfterCompletion.DELETE:
            scheduler_backends[self.account][record.region].delete_schedule(record.group, record.name)
            world.apply(
                Change(
                    entity=_schedule_entity(ref), operation=Operation.DELETE, actor=Actor.SCENARIO, parent=record.group
                )
            )
            return
        world.apply(_schedule_change(record.model_copy(update={"next_at": None}), Operation.UPDATE, Actor.SCENARIO))

    def _bound(self) -> Wakes:
        if self._wakes is None:
            raise RuntimeError("the aws provider books wakes, and bind() was not called before the run")
        return self._wakes


def build() -> AwsProvider:
    if "MOTO_ACCOUNT_ID" in os.environ:
        raise RuntimeError("MOTO_ACCOUNT_ID is set: every run would share one AWS account; unset it")
    return AwsProvider()


def _schedule_entity(arn: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=arn)


def _message_entity(message_id: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=message_id)


def _schedule_change(record: ScheduleRecord, operation: Operation, actor: Actor) -> Change:
    return Change(
        entity=_schedule_entity(record.arn),
        operation=operation,
        actor=actor,
        body=record.model_dump_json(),
        parent=record.group,
        after=RecordSnapshot(resource="schedule", text=record.text()),
    )


# --- ASGI plumbing ---------------------------------------------------------------------------

Headers = list[tuple[bytes, bytes]]


def _headers(scope: Scope) -> Headers:
    raw = scope["headers"]
    if not isinstance(raw, list):
        raise TypeError("an ASGI http scope carries its headers as a list")
    pairs: Headers = []
    for item in raw:
        if not (isinstance(item, (tuple, list)) and len(item) == 2):
            raise TypeError("an ASGI header is a (name, value) pair")
        name, value = item[0], item[1]
        if not (isinstance(name, bytes) and isinstance(value, bytes)):
            raise TypeError("ASGI header names and values are bytes")
        pairs.append((name.lower(), value))
    return pairs


def _header(headers: Headers, name: bytes) -> str:
    return next((v.decode() for k, v in headers if k == name), "")


def _query(scope: Scope) -> str:
    raw = scope["query_string"]
    if not isinstance(raw, bytes):
        raise TypeError("an ASGI query string is bytes")
    return raw.decode()


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        chunk = message["body"] if "body" in message else b""
        if isinstance(chunk, bytes):
            chunks.append(chunk)
        if not (message.get("more_body")):
            return b"".join(chunks)


async def _call(app: ASGIApp, scope: Scope, body: bytes) -> tuple[int, Headers, bytes]:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    status = 500
    headers: Headers = []
    chunks: list[bytes] = []

    async def send(message: Message) -> None:
        nonlocal status, headers
        if message["type"] == "http.response.start":
            code = message["status"]
            status = code if isinstance(code, int) else 500
            headers = _headers(message)
        elif message["type"] == "http.response.body":
            chunk = message["body"] if "body" in message else b""
            if isinstance(chunk, bytes):
                chunks.append(chunk)

    await app(scope, receive, send)
    return status, headers, b"".join(chunks)


async def _respond(send: Send, status: int, headers: Headers, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})
