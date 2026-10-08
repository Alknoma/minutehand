"""The AWS provider's surface against AWS's own machine-readable one: botocore's service models for `scheduler`
(2021-06-30) and `sqs` (2012-11-05), as installed. Every operation in either is served or refused by name, the
provider's route table is the model's, and each refused operation, called by stock boto3 through the proxy,
answers 501 `NotImplemented` naming it."""

from __future__ import annotations

import re
import ssl
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import cast

import pytest
from botocore import xform_name
from botocore.exceptions import ClientError
from botocore.loaders import Loader

from minutehand.adapters.providers.aws.wire import (
    REFUSED_BECAUSE,
    SCHEDULER_ROUTES,
    SERVED,
    SQS_OPERATIONS,
    Service,
)
from tests.providers.aws.test_aws_provider import AwsRun

MODELS = {Service.SCHEDULER: "scheduler", Service.SQS: "sqs"}
Doc = dict[str, object]


def _doc(value: object) -> Doc:
    assert isinstance(value, dict)
    return cast(Doc, value)


def model(service: Service) -> Doc:
    """botocore's service model for `service`, as installed (`botocore/data/<service>/<version>/service-2.json`)."""
    return _doc(Loader().load_service_model(MODELS[service], "service-2"))


def operations(service: Service) -> dict[str, Doc]:
    return {name: _doc(op) for name, op in _doc(model(service)["operations"]).items()}


def every_operation() -> list[tuple[Service, str]]:
    return [(service, name) for service in Service for name in operations(service)]


def test_the_models_are_the_versions_the_provider_was_read_against() -> None:
    assert _doc(model(Service.SCHEDULER)["metadata"])["apiVersion"] == "2021-06-30"
    assert _doc(model(Service.SQS)["metadata"])["apiVersion"] == "2012-11-05"


def test_every_operation_in_the_models_is_served_or_refused_by_name_and_nothing_else_is_listed() -> None:
    for service in Service:
        names = set(operations(service))
        listed = set(SCHEDULER_ROUTES) if service is Service.SCHEDULER else set(SQS_OPERATIONS)
        assert listed == names, f"{service}: the provider's list is not botocore's"
        assert SERVED[service] <= names
        refused = names - SERVED[service]
        assert refused <= set(REFUSED_BECAUSE), f"{service}: refused with no reason: {refused - set(REFUSED_BECAUSE)}"
    every = {name for _, name in every_operation()}
    assert set(REFUSED_BECAUSE) <= every and not set(REFUSED_BECAUSE) & (
        SERVED[Service.SCHEDULER] | SERVED[Service.SQS]
    )


def test_the_scheduler_route_table_is_the_models() -> None:
    for name, op in operations(Service.SCHEDULER).items():
        http = _doc(op["http"])
        method, pattern = SCHEDULER_ROUTES[name]
        uri = re.sub(r"\{[^}]+\}", "x", str(http["requestUri"]).split("?")[0])
        assert method == http["method"] and pattern.fullmatch(uri), name
        clashes = [o for o, (m, p) in SCHEDULER_ROUTES.items() if o != name and m == method and p.fullmatch(uri)]
        assert clashes == [], f"{name}'s route also matches {clashes}"


def least(shapes: Doc, name: str) -> object:
    """The least value botocore's validation accepts for shape `name`: required members only."""
    shape = _doc(shapes[name])
    kind = shape["type"]
    if kind == "structure":
        members = _doc(shape["members"])
        required = cast(list[str], shape["required"]) if "required" in shape else []
        return {member: least(shapes, str(_doc(members[member])["shape"])) for member in required}
    if kind == "list":
        return []
    if kind == "map":
        return {}
    if kind == "string":
        enum = cast(list[str], shape["enum"]) if "enum" in shape else []
        return enum[0] if enum else "x" * max(int(cast(int, shape["min"])) if "min" in shape else 1, 1)
    if kind in ("integer", "long"):
        return max(int(cast(int, shape["min"])) if "min" in shape else 1, 1)
    if kind == "boolean":
        return False
    if kind == "timestamp":
        return datetime(2030, 1, 1, tzinfo=UTC)
    if kind == "blob":
        return b"x"
    raise AssertionError(f"no least value for a {kind}")


def _call(run: AwsRun, service: Service, name: str) -> ClientError | None:
    client = run.scheduler if service is Service.SCHEDULER else run.sqs
    op = operations(service)[name]
    shapes = _doc(model(service)["shapes"])
    kwargs = least(shapes, str(_doc(op["input"])["shape"])) if "input" in op else {}
    assert isinstance(kwargs, dict)
    try:
        getattr(client, xform_name(name))(**kwargs)
    except ClientError as error:
        return error
    return None


@pytest.mark.parametrize(("service", "name"), [(s, n) for s, n in every_operation() if n in REFUSED_BECAUSE])
def test_a_refused_operation_called_by_boto3_is_refused_501_naming_it(run: AwsRun, service: Service, name: str) -> None:
    refused = _call(run, service, name)
    assert refused is not None, f"{name} was answered"
    assert refused.response["ResponseMetadata"]["HTTPStatusCode"] == 501
    assert refused.response["Error"]["Code"] == "NotImplemented"
    assert f" {name}: {REFUSED_BECAUSE[name]}" in refused.response["Error"]["Message"]


def test_every_served_operation_is_answered_by_the_service_not_refused_as_unserved(run: AwsRun) -> None:
    for service in Service:
        for name in operations(service):
            if name not in SERVED[service]:
                continue
            answered = _call(run, service, name)
            if answered is not None:
                code = answered.response["Error"]["Code"]
                assert code != "NotImplemented" and answered.response["ResponseMetadata"]["HTTPStatusCode"] < 500, (
                    f"{name}: {answered}"
                )


def test_a_refused_operation_in_sqss_query_protocol_is_refused_in_its_xml(run: AwsRun) -> None:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": f"http://127.0.0.1:{run.proxy.port}"}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(run.proxy.ca))),
    )
    request = urllib.request.Request(
        "https://sqs.us-east-1.amazonaws.com/",
        data=b"Action=PurgeQueue&QueueUrl=https%3A%2F%2Fsqs.us-east-1.amazonaws.com%2F1%2Fq&Version=2012-11-05",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        opener.open(request, timeout=10)
    body = refused.value.read().decode()
    assert refused.value.code == 501
    assert "<Code>NotImplemented</Code>" in body and "SQS PurgeQueue" in body
