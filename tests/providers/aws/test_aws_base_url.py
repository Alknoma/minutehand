"""AWS by base URL, with stock boto3 given only an `endpoint_url`: the `QueueUrl` SQS answers with names the base
URL, and a message sent, received and deleted through it is the queue's."""

from __future__ import annotations

import asyncio
from pathlib import Path

import boto3
from botocore.config import Config

from minutehand.adapters.providers.aws.provider import build
from minutehand.adapters.proxy.base_url import base_url
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.clock import Due
from tests.providers.aws.test_aws_provider import START

SQS = "sqs.us-east-1.amazonaws.com"


class NoWakes:
    def book(self, due: Due) -> None:
        raise AssertionError("nothing is booked here")

    def cancel(self, ref: str) -> None:
        raise AssertionError("nothing is booked here")


async def test_a_queue_url_names_the_base_url_and_its_messages_go_round(tmp_path: Path) -> None:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.bind(NoWakes())
    async with Proxy(Routing(Registry.installed()), store, clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(store, clock, {"aws": provider.app(store, clock)})
        endpoint = base_url(proxy.url, SQS)

        def round_trip() -> tuple[str, list[str], list[str]]:
            sqs = boto3.Session(
                aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1"
            ).client("sqs", endpoint_url=endpoint, config=Config(retries={"max_attempts": 1}))
            url = sqs.create_queue(QueueName="agent-wakes")["QueueUrl"]
            sqs.send_message(QueueUrl=url, MessageBody="wake")
            got = sqs.receive_message(QueueUrl=url).get("Messages", [])
            for message in got:
                sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
            left = sqs.receive_message(QueueUrl=url).get("Messages", [])
            return url, [m["Body"] for m in got], [m["Body"] for m in left]

        url, received, left = await asyncio.to_thread(round_trip)
    assert url.startswith(f"{endpoint}/"), url
    assert (received, left) == (["wake"], [])
    assert {c.exchange.host for c in store.calls()} == {SQS}
