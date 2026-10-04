"""The AWS provider, reached the way an agent reaches AWS: stock boto3, no endpoint_url, a real proxy.

Each test is one run: a real `SqliteStore`, a real `RunClock` starting on Monday
24 August 2026 (weeks before the machine's date, so anything read from the machine
clock shows), and a list standing in for the orchestrator's pending wakes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from minutehand.adapters.providers.aws.provider import AwsProvider, build
from minutehand.adapters.providers.aws.wire import UnsupportedTarget
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.clock import Due, DueKind, next_jump
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.provider import BooksWakes, Provider, Wakes
from tests.support.aws_proxy import AwsProxy

START = datetime(2026, 8, 24, 10, 51, tzinfo=UTC)  # a Monday
ROLE = "arn:aws:iam::000000000000:role/scheduler"


class ListWakes:
    """The orchestrator's side of `Wakes`: what is pending, in a list."""

    def __init__(self) -> None:
        self.pending: list[Due] = []

    def book(self, due: Due) -> None:
        self.pending.append(due)

    def cancel(self, ref: str) -> None:
        self.pending = [d for d in self.pending if d.ref != ref]


class AwsRun:
    def __init__(self, root: Path, run_id: str, proxy: AwsProxy | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._owns_proxy = proxy is None
        self.proxy = proxy or AwsProxy(root / "ca").start()
        self.clock = RunClock(START)
        self.store: SqliteStore = self.proxy.on_loop(lambda: SqliteStore(root / "world.db", run_id, self.clock))
        self.wakes = ListWakes()
        self.provider = build()
        self.provider.bind(self.wakes)
        self.app = self.provider.app(self.store, self.clock)
        self.activate()
        self.sqs = self.proxy.client("sqs")
        self.scheduler = self.proxy.client("scheduler")

    def activate(self) -> None:
        """Answer the proxy's AWS traffic with this run's provider instance."""
        self.proxy.mount(self.app)

    def queue(self, name: str = "agent-wakes", **attributes: str) -> tuple[str, str]:
        url = self.sqs.create_queue(QueueName=name, Attributes=attributes)["QueueUrl"]
        arn = self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        return url, arn

    def schedule(self, name: str, expression: str, target: str, *, update: bool = False, **extra: object) -> str:
        call = self.scheduler.update_schedule if update else self.scheduler.create_schedule
        target_fields: dict[str, object] = {"Arn": target, "RoleArn": ROLE, "Input": f'{{"wake": "{name}"}}'}
        if "SqsParameters" in extra:
            target_fields["SqsParameters"] = extra.pop("SqsParameters")
        response = call(
            Name=name, ScheduleExpression=expression, FlexibleTimeWindow={"Mode": "OFF"}, Target=target_fields, **extra
        )
        return response["ScheduleArn"]

    def poll(self, url: str) -> list[str]:
        return [m["Body"] for m in self.sqs.receive_message(QueueUrl=url).get("Messages", [])]

    def advance(self) -> list[Due]:
        """One step of the run loop: jump to the earliest pending wake and fire everything due."""
        jump = next_jump(self.clock.now(), self.wakes.pending)
        assert jump is not None, "nothing is pending"
        self.clock.jump(jump.now)
        self.clock.begin_wake()
        for due in jump.firing:
            self.wakes.pending.remove(due)
            self.proxy.await_on_loop(self.provider.fire(due.ref, self.store, self.clock))
        return jump.firing

    def log(self) -> list[tuple[Actor, Operation, str | None]]:
        events = self.proxy.on_loop(self.store.events)
        return [
            (e.actor, e.operation, e.after.resource if isinstance(e.after, RecordSnapshot) else None) for e in events
        ]

    def stop(self) -> None:
        if self._owns_proxy:
            self.proxy.stop()


@pytest.fixture
def run(tmp_path: Path) -> Iterator[AwsRun]:
    aws = AwsRun(tmp_path / "a", "run-a")
    yield aws
    assert aws.proxy.refused == [], "a call left for a host no provider claims"
    aws.stop()


def test_it_satisfies_the_ports() -> None:
    aws: AwsProvider = build()
    provider: Provider = aws
    books: BooksWakes = aws
    wakes: Wakes = ListWakes()
    books.bind(wakes)
    assert provider.manifest.books_wakes


def test_a_one_time_schedule_books_its_instant_in_utc(run: AwsRun) -> None:
    _, queue = run.queue()
    arn = run.schedule("follow-up", "at(2026-08-27T10:51:00)", queue)
    assert run.wakes.pending == [Due(at=datetime(2026, 8, 27, 10, 51, tzinfo=UTC), kind=DueKind.AGENT_WAKE, ref=arn)]


def test_a_one_time_schedule_books_its_instant_in_its_own_timezone(run: AwsRun) -> None:
    _, queue = run.queue()
    run.schedule("follow-up", "at(2026-08-27T10:51:00)", queue, ScheduleExpressionTimezone="America/New_York")
    assert [d.at for d in run.wakes.pending] == [datetime(2026, 8, 27, 14, 51, tzinfo=UTC)]


def test_a_schedule_already_past_the_simulated_now_fires_at_once(run: AwsRun) -> None:
    url, queue = run.queue()
    run.schedule("overdue", "at(2026-08-20T09:00:00)", queue)
    run.advance()
    assert run.clock.now() == START
    assert run.poll(url) == ['{"wake": "overdue"}']


def test_fire_puts_the_input_where_receive_message_finds_it_and_nothing_is_there_before(run: AwsRun) -> None:
    url, queue = run.queue()
    run.schedule("follow-up", "at(2026-08-27T10:51:00)", queue)
    assert run.poll(url) == []
    run.clock.jump(datetime(2026, 8, 26, 9, 0, tzinfo=UTC))
    assert run.poll(url) == []
    run.advance()
    assert run.clock.now() == datetime(2026, 8, 27, 10, 51, tzinfo=UTC)
    assert run.poll(url) == ['{"wake": "follow-up"}']
    assert run.wakes.pending == []


def test_a_fifo_target_gets_the_message_group_id(run: AwsRun) -> None:
    url, queue = run.queue("agent-wakes.fifo", FifoQueue="true", ContentBasedDeduplication="true")
    run.schedule("grouped", "at(2026-08-25T00:00:00)", queue, SqsParameters={"MessageGroupId": "mission-42"})
    run.advance()
    received = run.sqs.receive_message(QueueUrl=url, MessageSystemAttributeNames=["MessageGroupId"])["Messages"]
    assert [m["Attributes"]["MessageGroupId"] for m in received] == ["mission-42"]


def test_a_rate_schedule_books_after_the_simulated_now_and_rebooks_after_firing(run: AwsRun) -> None:
    url, queue = run.queue()
    run.schedule("every-six-hours", "rate(6 hours)", queue)
    assert [d.at for d in run.wakes.pending] == [START + timedelta(hours=6)]
    run.advance()
    assert run.poll(url) == ['{"wake": "every-six-hours"}']
    assert [d.at for d in run.wakes.pending] == [START + timedelta(hours=12)]
    run.advance()
    assert [d.at for d in run.wakes.pending] == [START + timedelta(hours=18)]


def test_a_cron_schedule_books_its_next_occurrence_in_its_timezone_and_rebooks_after_firing(run: AwsRun) -> None:
    url, queue = run.queue()
    # 09:00 London on weekdays; London is UTC+1 in August. Monday 10:51 UTC is past Monday's 08:00 UTC.
    run.schedule("standup", "cron(0 9 ? * MON-FRI *)", queue, ScheduleExpressionTimezone="Europe/London")
    assert [d.at for d in run.wakes.pending] == [datetime(2026, 8, 25, 8, 0, tzinfo=UTC)]
    run.advance()
    assert run.poll(url) == ['{"wake": "standup"}']
    assert [d.at for d in run.wakes.pending] == [datetime(2026, 8, 26, 8, 0, tzinfo=UTC)]
    for _ in range(3):  # Wednesday, Thursday, Friday
        run.advance()
    assert [d.at for d in run.wakes.pending] == [datetime(2026, 8, 31, 8, 0, tzinfo=UTC)]


def test_a_recurring_schedule_stops_at_its_end_date(run: AwsRun) -> None:
    _, queue = run.queue()
    arn = run.schedule("twice", "rate(6 hours)", queue, EndDate=START + timedelta(hours=13))
    run.advance()
    run.advance()
    assert run.wakes.pending == []
    stored = run.proxy.on_loop(
        lambda: run.store.get(EntityRef(provider="aws", kind=EntityKind.RECORD, external_id=arn))
    )
    assert stored is not None and '"next_at":null' in stored.body


def test_an_update_rebooks(run: AwsRun) -> None:
    _, queue = run.queue()
    arn = run.schedule("follow-up", "at(2026-08-27T10:51:00)", queue)
    run.schedule("follow-up", "at(2026-08-29T08:00:00)", queue, update=True)
    assert run.wakes.pending == [Due(at=datetime(2026, 8, 29, 8, 0, tzinfo=UTC), kind=DueKind.AGENT_WAKE, ref=arn)]


def test_a_delete_cancels(run: AwsRun) -> None:
    url, queue = run.queue()
    arn = run.schedule("follow-up", "at(2026-08-27T10:51:00)", queue)
    run.scheduler.delete_schedule(Name="follow-up")
    assert run.wakes.pending == []
    with pytest.raises(LookupError, match=arn):
        run.proxy.await_on_loop(run.provider.fire(arn, run.store, run.clock))
    assert run.poll(url) == []


def test_a_disabled_schedule_books_nothing_until_it_is_enabled(run: AwsRun) -> None:
    _, queue = run.queue()
    run.schedule("paused", "at(2026-08-27T10:51:00)", queue, State="DISABLED")
    assert run.wakes.pending == []
    run.schedule("paused", "at(2026-08-27T10:51:00)", queue, update=True, State="ENABLED")
    assert [d.at for d in run.wakes.pending] == [datetime(2026, 8, 27, 10, 51, tzinfo=UTC)]
    run.schedule("paused", "at(2026-08-27T10:51:00)", queue, update=True, State="DISABLED")
    assert run.wakes.pending == []


def test_action_after_completion_delete_removes_a_one_time_schedule_once_it_fires(run: AwsRun) -> None:
    _, queue = run.queue()
    run.schedule("once", "at(2026-08-25T00:00:00)", queue, ActionAfterCompletion="DELETE")
    run.schedule("kept", "at(2026-08-25T00:00:00)", queue)
    run.advance()
    with pytest.raises(ClientError, match="ResourceNotFoundException"):
        run.scheduler.get_schedule(Name="once")
    assert run.scheduler.get_schedule(Name="kept")["Name"] == "kept"
    assert (Actor.SCENARIO, Operation.DELETE, None) in run.log()


def test_firing_a_lambda_target_is_refused_by_name(run: AwsRun) -> None:
    run.schedule("lambda", "at(2026-08-25T00:00:00)", "arn:aws:lambda:us-east-1:000000000000:function:wake-agent")
    before = run.log()
    with pytest.raises(UnsupportedTarget, match="lambda"):
        run.advance()
    assert run.log() == before, "nothing was delivered, so nothing is recorded as delivered"


def test_an_invalid_expression_is_refused_and_books_nothing(run: AwsRun) -> None:
    _, queue = run.queue()
    for bad in ("rate(1 hours)", "at(next tuesday)", "cron(0 9 * * MON *)"):
        with pytest.raises(ClientError, match="ValidationException"):
            run.schedule("bad", bad, queue)
    assert run.wakes.pending == []
    assert run.log() == []


def test_every_step_is_in_the_world_log_with_the_right_actor(run: AwsRun) -> None:
    _, queue = run.queue()
    run.schedule("hourly", "rate(1 hour)", queue)
    run.schedule("hourly", "rate(2 hours)", queue, update=True)
    run.advance()
    run.scheduler.delete_schedule(Name="hourly")
    assert run.log() == [
        (Actor.AGENT, Operation.CREATE, "schedule"),
        (Actor.AGENT, Operation.UPDATE, "schedule"),
        (Actor.SCENARIO, Operation.CREATE, "queue_message"),
        (Actor.SCENARIO, Operation.UPDATE, "schedule"),
        (Actor.AGENT, Operation.DELETE, None),
    ]


def test_two_provider_instances_do_not_see_each_others_aws(run: AwsRun, tmp_path: Path) -> None:
    """Both instances live in one process, so they share moto's global state; only the account keeps them apart."""
    url_a, queue_a = run.queue("shared-name")
    run.sqs.send_message(QueueUrl=url_a, MessageBody="only in run a")
    run.schedule("shared-name", "at(2026-08-25T00:00:00)", queue_a)
    other = AwsRun(tmp_path / "b", "run-b", proxy=run.proxy)
    assert other.sqs.list_queues().get("QueueUrls", []) == []
    assert other.scheduler.list_schedules()["Schedules"] == []
    url_b, queue_b = other.queue("shared-name")
    assert queue_a != queue_b
    assert other.poll(url_b) == []
    run.activate()
    assert run.poll(url_a) == ["only in run a"]
