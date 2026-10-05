"""What stock boto3 raises when the fake cannot answer: an operation it does not implement, a path no route of the
service has, a cron operator it does not reproduce, and Minutehand's own error. Each is a `ClientError` carrying the
message, marked `x-minutehand-answer` so nobody mistakes it for AWS's answer; a refusal AWS itself makes is not."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from minutehand.adapters.answering import ANSWER_HEADER, INTERNAL_PREFIX
from tests.providers.aws.test_aws_provider import AwsRun


@pytest.fixture
def run(tmp_path: Path) -> Iterator[AwsRun]:
    aws = AwsRun(tmp_path / "a", "run-a")
    yield aws
    assert aws.proxy.refused == [], "a call left for a host no provider claims"
    aws.stop()


def _answered(error: ClientError) -> tuple[int, str | None, str]:
    metadata = error.response["ResponseMetadata"]
    headers = metadata["HTTPHeaders"]
    return metadata["HTTPStatusCode"], headers.get(ANSWER_HEADER), error.response["Error"]["Message"]


def test_an_operation_moto_does_not_implement_is_a_client_error_naming_it(run: AwsRun) -> None:
    _, queue = run.queue()
    with pytest.raises(ClientError) as raised:
        run.sqs.start_message_move_task(SourceArn=queue)
    status, answer, message = _answered(raised.value)
    assert (status, answer) == (501, "not_implemented")
    assert raised.value.response["Error"]["Code"] == "minutehand_not_implemented"
    assert "does not implement this operation" in message
    assert "start_message_move_task action has not been implemented" in message
    assert "does not implement" in str(raised.value)


def test_a_path_no_route_of_the_service_has_names_the_closest_it_has(run: AwsRun) -> None:
    """moto has AWS Batch, but no route for its newer service environments."""
    with pytest.raises(ClientError) as raised:
        run.proxy.client("batch").describe_service_environments()
    status, answer, message = _answered(raised.value)
    assert (status, answer) == (501, "not_implemented")
    assert (
        "does not implement this operation: POST batch.us-east-1.amazonaws.com/v1/describeserviceenvironments"
        in message
    )
    assert "The closest it has is POST /v1/describe" in message


def test_a_cron_operator_the_fake_does_not_reproduce_is_a_client_error_naming_it(run: AwsRun) -> None:
    _, queue = run.queue()
    with pytest.raises(ClientError) as raised:
        run.schedule("last-day", "cron(0 9 L * ? *)", queue)
    status, answer, message = _answered(raised.value)
    assert (status, answer) == (501, "not_implemented")
    assert "the L, W or # cron operators (in cron(0 9 L * ? *))" in message
    assert run.wakes.pending == []


def test_an_invalid_expression_is_refused_as_aws_refuses_it_and_not_marked(run: AwsRun) -> None:
    _, queue = run.queue()
    with pytest.raises(ClientError) as raised:
        run.schedule("bad", "rate(1 hours)", queue)
    status, answer, message = _answered(raised.value)
    assert (status, answer) == (400, None)
    assert raised.value.response["Error"]["Code"] == "ValidationException"
    assert message == "Invalid Schedule Expression rate(1 hours)."


def test_an_internal_error_is_a_client_error_saying_so(run: AwsRun) -> None:
    """The delete reads the run's store before moto answers it; with the store closed, that read fails."""
    url, queue = run.queue()
    run.schedule("follow-up", "at(2026-08-25T00:00:00)", queue)
    run.advance()
    [received] = run.sqs.receive_message(QueueUrl=url)["Messages"]
    run.proxy.on_loop(run.store.close)
    with pytest.raises(ClientError) as raised:
        run.sqs.delete_message(QueueUrl=url, ReceiptHandle=received["ReceiptHandle"])
    status, answer, message = _answered(raised.value)
    assert (status, answer) == (500, "internal_error")
    assert raised.value.response["Error"]["Code"] == "minutehand_internal_error"
    assert message.startswith(f"{INTERNAL_PREFIX} aws POST /: ProgrammingError")
