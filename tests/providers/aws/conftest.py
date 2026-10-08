"""One run of the AWS provider per test, reached through a real proxy by stock boto3."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.providers.aws.test_aws_provider import AwsRun


@pytest.fixture
def run(tmp_path: Path) -> Iterator[AwsRun]:
    aws = AwsRun(tmp_path / "a", "run-a")
    yield aws
    assert aws.proxy.refused == [], "a call left for a host no provider claims"
    aws.stop()
