"""The Cloud Tasks provider's surface against Google's machine-readable ones: the v2 discovery document
(`tests/data/google_cloud_tasks/cloudtasks_v2_discovery.json`, fetched from
https://cloudtasks.googleapis.com/$discovery/rest?version=v2, revision 20260930) for REST, and the installed
`google-cloud-tasks` client's `gapic_metadata.json` (generated from its protos) for gRPC. Every method in either is
served or refused by name; each refused one, called through the proxy, answers 501 (UNIMPLEMENTED) naming it."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import google.cloud.tasks_v2
import pytest

from minutehand.adapters.providers.google_cloud_tasks import wire
from tests.providers.google_cloud_tasks.test_cloud_tasks import GRPC, QUEUE, Tasks, opened, seeded

pytestmark = pytest.mark.timeout(120)

DISCOVERY = Path(__file__).parents[2] / "data" / "google_cloud_tasks" / "cloudtasks_v2_discovery.json"
Doc = dict[str, object]


def _doc(value: object) -> Doc:
    assert isinstance(value, dict)
    return cast(Doc, value)


def discovery_methods() -> dict[str, Doc]:
    found: dict[str, Doc] = {}

    def walk(resource: Doc) -> None:
        for method in _doc(resource["methods"]).values() if "methods" in resource else []:
            found[str(_doc(method)["id"])] = _doc(method)
        for child in _doc(resource["resources"]).values() if "resources" in resource else []:
            walk(_doc(child))

    walk(_doc(json.loads(DISCOVERY.read_text())))
    return found


def grpc_methods() -> list[str]:
    metadata = Path(google.cloud.tasks_v2.__file__).parent / "gapic_metadata.json"
    services = _doc(_doc(json.loads(metadata.read_text()))["services"])
    clients = _doc(_doc(services["CloudTasks"])["clients"])
    return sorted(_doc(_doc(clients["grpc"])["rpcs"]))


def sample(flat_path: str) -> str:
    """A path the discovery method's `flatPath` matches: each `{…Id}` filled in."""
    return "/" + re.sub(r"\{[^}]+\}", "x", flat_path)


def test_the_committed_discovery_document_is_cloud_tasks_v2() -> None:
    document = _doc(json.loads(DISCOVERY.read_text()))
    assert (document["name"], document["version"], document["revision"]) == ("cloudtasks", "v2", "20260930")


def test_every_rest_method_is_served_or_refused_by_name_on_its_own_route() -> None:
    methods = discovery_methods()
    assert set(wire.REST_METHODS) == set(methods)
    assert wire.REST_SERVED.isdisjoint(wire.REST_REFUSED) and wire.REST_SERVED | set(wire.REST_REFUSED) == set(methods)
    for name, method in methods.items():
        path = sample(str(method["flatPath"]))
        assert wire.rest_method(str(method["httpMethod"]), path) == name, f"{name}: {path}"


def test_every_grpc_method_is_served_or_refused_by_name() -> None:
    methods = grpc_methods()
    assert sorted(wire.GRPC_METHODS) == methods
    assert wire.GRPC_SERVED.isdisjoint(wire.GRPC_REFUSED) and wire.GRPC_SERVED | set(wire.GRPC_REFUSED) == set(methods)


@pytest.fixture
async def tasks(tmp_path: Path) -> AsyncIterator[Tasks]:
    async for found in opened(tmp_path, seeded()):
        yield found


async def test_each_refused_rest_method_called_through_the_proxy_answers_501_naming_it(tasks: Tasks) -> None:
    calls = [(name, str(m["httpMethod"]), sample(str(m["flatPath"]))) for name, m in discovery_methods().items()]
    client = await tasks.client(
        f"""
import requests
answers = {{}}
for name, verb, path in {calls!r}:
    if name in {sorted(wire.REST_SERVED)!r}:
        continue
    answered = requests.request(verb, "https://cloudtasks.googleapis.com" + path, json={{}}, timeout=30)
    answers[name] = [answered.status_code, answered.json()["error"]["status"], answered.json()["error"]["message"]]
say(answers=answers)
"""
    )
    answers = _doc((await client.heard())["answers"])
    await client.finished()
    assert set(answers) == set(wire.REST_REFUSED)
    for name, answer in answers.items():
        status, word, message = cast(list[object], answer)
        assert (status, word) == (501, "UNIMPLEMENTED"), name
        assert f"{name}: {wire.REST_REFUSED[name]}" in str(message), message


async def test_each_refused_grpc_method_called_by_googles_client_answers_unimplemented_naming_it(
    tasks: Tasks,
) -> None:
    location = QUEUE.rsplit("/queues/", 1)[0]
    requests = {
        "UpdateQueue": {"queue": {"name": QUEUE}},
        "PurgeQueue": {"name": QUEUE},
        "PauseQueue": {"name": QUEUE},
        "ResumeQueue": {"name": QUEUE},
        "GetIamPolicy": {"resource": QUEUE},
        "SetIamPolicy": {"resource": QUEUE, "policy": {}},
        "TestIamPermissions": {"resource": QUEUE, "permissions": ["cloudtasks.tasks.create"]},
        "RunTask": {"name": QUEUE + "/tasks/x"},
        "BatchCreateTasks": {"parent": QUEUE},
        "BatchDeleteTasks": {"parent": QUEUE},
        "GetCmekConfig": {"name": location + "/cmekConfig"},
        "UpdateCmekConfig": {"cmek_config": {"name": location + "/cmekConfig"}},
    }
    assert set(requests) == set(wire.GRPC_REFUSED)
    client = await tasks.client(
        f"""
import re
answers = {{}}
for name, request in {requests!r}.items():
    call = getattr(client, re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower())
    answers[name] = refused(lambda: call(request=request))
say(answers=answers)
""",
        transport=GRPC,
    )
    answers = _doc((await client.heard())["answers"])
    await client.finished()
    for name, answer in answers.items():
        kind, message = cast(list[str], answer)
        assert kind == "MethodNotImplemented", f"{name}: {kind} {message}"
        assert f"google.cloud.tasks.v2.CloudTasks/{name}: {wire.GRPC_REFUSED[name]}" in message


async def test_any_credential_or_none_is_answered(tasks: Tasks) -> None:
    client = await tasks.client(
        f"""
import requests
url = "https://cloudtasks.googleapis.com/v2/{QUEUE}"
answers = [requests.get(url, headers=headers, timeout=30).status_code for headers in (
    {{}}, {{"Authorization": "Bearer ya29.nobody-ever-issued-this"}}, {{"Authorization": "Basic Zm9vOmJhcg=="}})]
say(answers=answers)
"""
    )
    assert (await client.heard())["answers"] == [200, 200, 200]
    await client.finished()
