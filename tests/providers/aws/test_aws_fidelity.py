"""Each behaviour `CLAIMS.md` pins where moto, left alone, would answer otherwise: stock boto3 through the proxy,
one run per test (a real `SqliteStore`, a real `RunClock` weeks before the machine's date)."""

from __future__ import annotations

import hashlib
import json
import ssl
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from botocore import UNSIGNED
from botocore.exceptions import ClientError
from moto import settings as moto_settings

from tests.providers.aws.test_aws_provider import ROLE, START, AwsRun


def _code(error: pytest.ExceptionInfo[ClientError]) -> tuple[str, int]:
    response = error.value.response
    return response["Error"]["Code"], response["ResponseMetadata"]["HTTPStatusCode"]


# --- authentication always passes ------------------------------------------------------------------------------


@pytest.fixture
def iam_checked() -> Iterator[None]:
    """moto told to check every request's signature and IAM policy, as `INITIAL_NO_AUTH_ACTION_COUNT=0`,
    `set_initial_no_auth_action_count(0)` or `enable_iam_authentication()` tell it."""
    before = moto_settings.INITIAL_NO_AUTH_ACTION_COUNT
    moto_settings.INITIAL_NO_AUTH_ACTION_COUNT = 0
    yield
    moto_settings.INITIAL_NO_AUTH_ACTION_COUNT = before


@pytest.mark.usefixtures("iam_checked")
def test_any_credential_or_none_is_answered_even_when_moto_is_told_to_check_iam(run: AwsRun) -> None:
    stranger = run.proxy.client("sqs", key="AKIANOBODYEVERISSUED", secret="not-the-secret-of-anything")
    url = stranger.create_queue(QueueName="anyone")["QueueUrl"]
    unsigned = run.proxy.client("sqs", signature_version=UNSIGNED)
    unsigned.send_message(QueueUrl=url, MessageBody="unsigned")
    scheduler = run.proxy.client("scheduler", key="AKIAANOTHERSTRANGER0", secret="x")
    assert scheduler.list_schedules()["Schedules"] == []
    assert [m["Body"] for m in stranger.receive_message(QueueUrl=url)["Messages"]] == ["unsigned"]


# --- what is not AWS, or not served, is refused by name ---------------------------------------------------------


def test_another_aws_service_is_refused_naming_its_host(run: AwsRun) -> None:
    with pytest.raises(ClientError) as refused:
        run.proxy.client("sts").get_caller_identity()
    assert _code(refused) == ("NotImplemented", 501)
    assert "sts.us-east-1.amazonaws.com: only EventBridge Scheduler and SQS are served" in str(refused.value)


def test_motos_management_api_is_refused_as_no_part_of_aws(run: AwsRun) -> None:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": f"http://127.0.0.1:{run.proxy.port}"}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(run.proxy.ca))),
    )
    request = urllib.request.Request("https://sqs.us-east-1.amazonaws.com/moto-api/reset-auth", data=b"0")
    with pytest.raises(urllib.error.HTTPError) as refused:
        opener.open(request, timeout=10)
    assert refused.value.code == 501
    assert "moto's management API is no part of AWS" in refused.value.read().decode()


# --- SQS on the run's clock -------------------------------------------------------------------------------------


def test_a_received_message_comes_back_once_the_runs_clock_passes_the_default_visibility_timeout(run: AwsRun) -> None:
    """The reference: "If you don't include the parameter, the overall visibility timeout for the queue is used ... The default
    visibility timeout for a queue is 30 seconds" (ReceiveMessage). The machine's clock does not move here."""
    url, _ = run.queue()
    run.sqs.send_message(QueueUrl=url, MessageBody="wake")
    assert run.poll(url) == ["wake"]
    run.clock.jump(START + timedelta(seconds=29))
    assert run.poll(url) == [], "in flight for 30 s of the run's time"
    run.clock.jump(START + timedelta(seconds=31))
    assert run.poll(url) == ["wake"]


def test_a_delayed_message_appears_when_the_runs_clock_reaches_its_delay(run: AwsRun) -> None:
    url, _ = run.queue()
    run.sqs.send_message(QueueUrl=url, MessageBody="later", DelaySeconds=600)
    assert run.poll(url) == []
    run.clock.jump(START + timedelta(seconds=601))
    assert run.poll(url) == ["later"]


def test_message_and_schedule_timestamps_are_the_runs_time(run: AwsRun) -> None:
    url, queue = run.queue()
    run.sqs.send_message(QueueUrl=url, MessageBody="stamped")
    [got] = run.sqs.receive_message(QueueUrl=url, MessageSystemAttributeNames=["SentTimestamp"])["Messages"]
    assert int(got["Attributes"]["SentTimestamp"]) == int(START.timestamp() * 1000)
    run.schedule("stamped", "at(2026-08-30T00:00:00)", queue)
    made = run.scheduler.get_schedule(Name="stamped")
    assert made["CreationDate"] == made["LastModificationDate"] == START


@pytest.mark.timeout(30)
def test_a_long_poll_that_finds_a_message_answers_at_once(run: AwsRun) -> None:
    """The reference: "If a message is available, the call returns sooner than WaitTimeSeconds" (ReceiveMessage)."""
    url, _ = run.queue()
    run.sqs.send_message(QueueUrl=url, MessageBody="there")
    got = run.proxy.client("sqs", read_timeout=10).receive_message(QueueUrl=url, WaitTimeSeconds=20)
    assert [m["Body"] for m in got["Messages"]] == ["there"]


@pytest.mark.timeout(30)
def test_a_long_poll_that_would_wait_is_refused_by_name_and_never_waits(run: AwsRun) -> None:
    """Here nothing holds a call while the run's clock moves (as in a standing world, whose clock only its control
    API moves; a run's proxy holds it, `tests/e2e/test_long_poll_run.py`): refused, whether the wait is the call's
    or the queue's ReceiveMessageWaitTimeSeconds; an explicit WaitTimeSeconds of 0 is a short poll."""
    sqs = run.proxy.client("sqs", read_timeout=10)
    url, _ = run.queue()
    waiting, _ = run.queue("waiting", ReceiveMessageWaitTimeSeconds="20")
    began = time.monotonic()
    for asked in ({"QueueUrl": url, "WaitTimeSeconds": 20}, {"QueueUrl": waiting}):
        with pytest.raises(ClientError) as refused:
            sqs.receive_message(**asked)
        assert _code(refused) == ("NotImplemented", 501)
        assert "a long poll" in str(refused.value)
    assert "Messages" not in sqs.receive_message(QueueUrl=waiting, WaitTimeSeconds=0)
    assert time.monotonic() - began < 5
    assert run.clock.now() == START


def test_receive_takes_one_to_ten_messages_and_refuses_more(run: AwsRun) -> None:
    """The reference: "Valid values: 1 to 10. Default: 1." (ReceiveMessage, MaxNumberOfMessages): moto's own check, kept."""
    url, _ = run.queue()
    for n in range(3):
        run.sqs.send_message(QueueUrl=url, MessageBody=f"m{n}")
    assert len(run.sqs.receive_message(QueueUrl=url)["Messages"]) == 1
    with pytest.raises(ClientError) as refused:
        run.sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=11)
    assert _code(refused)[0] == "InvalidParameterValue"


def test_a_visibility_change_runs_on_the_runs_clock(run: AwsRun) -> None:
    url, _ = run.queue()
    run.sqs.send_message(QueueUrl=url, MessageBody="held")
    [got] = run.sqs.receive_message(QueueUrl=url)["Messages"]
    run.sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=got["ReceiptHandle"], VisibilityTimeout=600)
    run.clock.jump(START + timedelta(seconds=300))
    assert run.poll(url) == []
    run.clock.jump(START + timedelta(seconds=601))
    assert run.poll(url) == ["held"]


# --- SQS data as sent --------------------------------------------------------------------------------------------


def test_queue_attributes_are_answered_as_they_were_sent(run: AwsRun) -> None:
    dlq_url, dlq = run.queue("wakes-dlq")
    redrive = '{"deadLetterTargetArn":"' + dlq + '","maxReceiveCount":"3"}'
    policy = '{ "Version": "2012-10-17" }'
    url, _ = run.queue("wakes", RedrivePolicy=redrive, Policy=policy, VisibilityTimeout="45")
    every = run.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    assert (every["RedrivePolicy"], every["Policy"], every["VisibilityTimeout"]) == (redrive, policy, "45")
    named = run.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["RedrivePolicy"])["Attributes"]
    assert named == {"RedrivePolicy": redrive}
    assert dlq_url != url


def test_a_received_message_carries_no_made_up_sender_id(run: AwsRun) -> None:
    url, _ = run.queue()
    run.sqs.send_message(QueueUrl=url, MessageBody="from nobody in particular")
    [got] = run.sqs.receive_message(QueueUrl=url, MessageSystemAttributeNames=["All"])["Messages"]
    assert "SenderId" not in got["Attributes"]
    assert "SentTimestamp" in got["Attributes"]


# --- EventBridge Scheduler: what was sent, and what UpdateSchedule resets -----------------------------------------


def test_a_schedule_is_read_back_with_the_target_and_timezone_it_was_sent_with_and_nothing_added(
    run: AwsRun,
) -> None:
    _, queue = run.queue()
    run.schedule("plain", "at(2026-08-30T00:00:00)", queue)
    made = run.scheduler.get_schedule(Name="plain")
    assert made["Target"] == {"Arn": queue, "RoleArn": ROLE, "Input": '{"wake": "plain"}'}
    assert "ScheduleExpressionTimezone" not in made


def test_an_update_that_leaves_fields_out_sets_them_to_their_defaults(run: AwsRun) -> None:
    """The reference: "if you do not set an optional field in your request, that field will be set to its system-default value
    after the update" (UpdateSchedule)."""
    _, queue = run.queue()
    run.schedule("once", "at(2026-08-25T00:00:00)", queue, ActionAfterCompletion="DELETE", State="DISABLED")
    run.schedule("once", "at(2026-08-25T00:00:00)", queue, update=True)
    made = run.scheduler.get_schedule(Name="once")
    assert made["State"] == "ENABLED"
    assert "ActionAfterCompletion" not in made
    run.advance()
    assert run.scheduler.get_schedule(Name="once")["Name"] == "once", "no longer deleted after completion"


def test_a_create_repeated_with_its_client_token_answers_the_same_schedule(run: AwsRun) -> None:
    _, queue = run.queue()
    first = run.schedule("idempotent", "at(2026-08-30T00:00:00)", queue, ClientToken="token-1")
    again = run.schedule("idempotent", "at(2026-08-30T00:00:00)", queue, ClientToken="token-1")
    assert first == again
    assert len(run.wakes.pending) == 1
    with pytest.raises(ClientError) as conflict:
        run.schedule("idempotent", "at(2026-08-30T00:00:00)", queue, ClientToken="token-2")
    assert _code(conflict)[0] == "ConflictException"


def test_a_schedule_in_a_group_other_than_default_is_refused_not_found(run: AwsRun) -> None:
    _, queue = run.queue()
    with pytest.raises(ClientError) as refused:
        run.schedule("grouped", "at(2026-08-30T00:00:00)", queue, GroupName="nightly")
    assert _code(refused) == ("ResourceNotFoundException", 404)
    with pytest.raises(ClientError) as read:
        run.scheduler.get_schedule(Name="grouped", GroupName="nightly")
    assert _code(read) == ("ResourceNotFoundException", 404)


def test_a_flexible_window_without_its_maximum_is_refused(run: AwsRun) -> None:
    _, queue = run.queue()
    with pytest.raises(ClientError) as refused:
        run.scheduler.create_schedule(
            Name="flexible",
            ScheduleExpression="rate(1 hour)",
            FlexibleTimeWindow={"Mode": "FLEXIBLE"},
            Target={"Arn": queue, "RoleArn": ROLE, "Input": "x"},
        )
    assert _code(refused) == ("ValidationException", 400)
    assert run.wakes.pending == []


def test_a_schedule_with_no_input_is_refused_by_name(run: AwsRun) -> None:
    _, queue = run.queue()
    with pytest.raises(ClientError) as refused:
        run.scheduler.create_schedule(
            Name="silent",
            ScheduleExpression="rate(1 hour)",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={"Arn": queue, "RoleArn": ROLE},
        )
    assert _code(refused) == ("NotImplemented", 501)
    assert "default notification" in str(refused.value)


# --- schedule expressions as the reference gives them -----------------------------------------------------------


def test_a_rate_takes_any_of_its_six_units_with_any_positive_value(run: AwsRun) -> None:
    """The reference: "a value as a positive integer, and a unit with the following options: minute | minutes | hour | hours |
    day | days" (CreateSchedule, ScheduleExpression)."""
    _, queue = run.queue()
    run.schedule("one-hours", "rate(1 hours)", queue)
    run.schedule("five-minute", "rate(5 minute)", queue)
    assert sorted(d.at for d in run.wakes.pending) == [START, START]


def test_cron_day_fields_the_reference_forbids_or_leaves_open_are_refused(run: AwsRun) -> None:
    _, queue = run.queue()
    with pytest.raises(ClientError) as both_stars:
        run.schedule("stars", "cron(0 9 * * * *)", queue)
    assert _code(both_stars) == ("ValidationException", 400)
    with pytest.raises(ClientError) as both_open:
        run.schedule("open", "cron(0 9 ? * ? *)", queue)
    assert _code(both_open) == ("NotImplemented", 501)


def test_a_cron_time_that_daylight_saving_skips_is_skipped(run: AwsRun) -> None:
    """Spring forward on 14 March 2027 in Los Angeles: 02:30 does not exist that day, so the next run is 15 March
    ("your schedule invocation is skipped", User Guide, Daylight savings time on EventBridge Scheduler)."""
    _, queue = run.queue()
    run.schedule(
        "nightly",
        "cron(30 2 * * ? *)",
        queue,
        ScheduleExpressionTimezone="America/Los_Angeles",
        StartDate=datetime(2027, 3, 13, 12, tzinfo=UTC),
    )
    assert [d.at for d in run.wakes.pending] == [datetime(2027, 3, 15, 9, 30, tzinfo=UTC)]


def test_a_cron_schedule_whose_start_date_is_past_is_accepted_as_sent(run: AwsRun) -> None:
    """CreateSchedule's StartDate is "The date, in UTC, after which the schedule can begin invoking its target"; no
    page limits how far back it may be, so moto's own 5-minute rule is not applied."""
    _, queue = run.queue()
    run.schedule("daily", "cron(0 9 * * ? *)", queue, StartDate=START - timedelta(days=1))
    assert [d.at for d in run.wakes.pending] == [datetime(2026, 8, 25, 9, 0, tzinfo=UTC)]
    assert run.scheduler.get_schedule(Name="daily")["StartDate"] == START - timedelta(days=1)


def test_md5_digests_are_awss_own(run: AwsRun) -> None:
    """MD5OfBody is the body's MD5; MD5OfMessageAttributes is computed as the SQS Developer Guide's "Calculating
    the MD5 message digest for message attributes" says: each attribute, by name, as length-prefixed name, data
    type, a transport byte (1 string, 2 binary) and length-prefixed value."""
    url, _ = run.queue()
    attributes = {
        "b": {"DataType": "Binary", "BinaryValue": b"\x00\xff"},
        "a": {"DataType": "Number", "StringValue": "1.50"},
    }
    sent = run.sqs.send_message(QueueUrl=url, MessageBody="héllo", MessageAttributes=attributes)
    digest = hashlib.md5()
    for name in sorted(attributes):
        attribute = attributes[name]
        for part in (name.encode(), str(attribute["DataType"]).encode()):
            digest.update(struct.pack("!I", len(part)) + part)
        value = attribute["BinaryValue"] if "BinaryValue" in attribute else str(attribute["StringValue"]).encode()
        assert isinstance(value, bytes)
        digest.update(bytes([2 if "BinaryValue" in attribute else 1]) + struct.pack("!I", len(value)) + value)
    assert sent["MD5OfMessageBody"] == hashlib.md5("héllo".encode()).hexdigest()
    assert sent["MD5OfMessageAttributes"] == digest.hexdigest()
    [got] = run.sqs.receive_message(QueueUrl=url, MessageAttributeNames=["All"])["Messages"]
    assert got["MessageAttributes"] == {
        "a": {"StringValue": "1.50", "DataType": "Number"},
        "b": {"BinaryValue": b"\x00\xff", "DataType": "Binary"},
    }
    assert json.dumps(got["Body"]) == json.dumps("héllo")
