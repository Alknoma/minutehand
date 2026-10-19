"""The Firebase Cloud Messaging provider's surface against Google's machine-readable one: the FCM v1 discovery
document the installed `google-api-python-client` builds its client from (committed as
`tests/data/firebase_cloud_messaging/fcm_v1_discovery.json`, revision 20260911). Every method in it is served or
refused by name; each refused one, called through the proxy, answers 501 naming it, and so does a route that is no
method of it (the retired batch endpoint and legacy HTTP API)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest

from minutehand.adapters.providers.firebase_cloud_messaging import wire
from tests.providers.firebase_cloud_messaging.test_fcm import SEED, Fcm, opened

pytestmark = pytest.mark.timeout(120)

DISCOVERY = Path(__file__).parents[2] / "data" / "firebase_cloud_messaging" / "fcm_v1_discovery.json"
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


def sample(flat_path: str) -> str:
    """A path the discovery method's `flatPath` matches: each `{…Id}` filled in."""
    return "/" + re.sub(r"\{[^}]+\}", "x", flat_path)


def test_the_committed_discovery_document_is_fcm_v1() -> None:
    document = _doc(json.loads(DISCOVERY.read_text()))
    assert (document["name"], document["version"], document["revision"]) == ("fcm", "v1", "20260911")


def test_every_rest_method_is_served_or_refused_by_name_on_its_own_route() -> None:
    methods = discovery_methods()
    assert set(wire.REST_METHODS) == set(methods)
    assert wire.REST_SERVED.isdisjoint(wire.REST_REFUSED) and wire.REST_SERVED | set(wire.REST_REFUSED) == set(methods)
    for name, method in methods.items():
        path = sample(str(method["flatPath"]))
        assert wire.rest_method(str(method["httpMethod"]), path) == name, f"{name}: {path}"


def test_every_message_field_of_the_document_is_read() -> None:
    schemas = _doc(_doc(json.loads(DISCOVERY.read_text()))["schemas"])
    message = _doc(_doc(schemas["Message"])["properties"])
    request = _doc(_doc(schemas["SendMessageRequest"])["properties"])
    assert set(message) == set(wire.MessageIn.model_fields)
    assert set(request) == set(wire.SendIn.model_fields)


@pytest.fixture
async def fcm(tmp_path: Path) -> AsyncIterator[Fcm]:
    async for found in opened(tmp_path, SEED):
        yield found


async def test_each_refused_rest_method_called_through_the_proxy_answers_501_naming_it(fcm: Fcm) -> None:
    calls = [(name, str(m["httpMethod"]), sample(str(m["flatPath"]))) for name, m in discovery_methods().items()]
    client = await fcm.client(
        f"""
answers = {{}}
for name, verb, path in {calls!r}:
    if name in {sorted(wire.REST_SERVED)!r}:
        continue
    answered = session.request(verb, "https://fcm.googleapis.com" + path, json={{}}, timeout=30)
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


async def test_a_route_that_is_no_method_of_the_v1_api_answers_501_naming_it(fcm: Fcm) -> None:
    client = await fcm.client(
        """
answers = [[r.status_code, r.json()["error"]["message"]] for r in (
    session.post("https://fcm.googleapis.com/batch", data=b"--batch--"),
    session.post("https://fcm.googleapis.com/fcm/send", json={"to": "x"}))]
say(answers=answers)
"""
    )
    answers = cast(list[list[object]], (await client.heard())["answers"])
    await client.finished()
    assert [a[0] for a in answers] == [501, 501]
    assert "no method of the FCM HTTP v1 API has the route POST /batch" in str(answers[0][1])
    assert "no method of the FCM HTTP v1 API has the route POST /fcm/send" in str(answers[1][1])


async def test_any_credential_or_none_is_answered(fcm: Fcm) -> None:
    client = await fcm.client(
        """
import requests
answers = [requests.post(SEND, headers=headers, json={"message": {"token": "fGcm-active-phone:APA91bH-phone"}},
                         timeout=30).status_code
           for headers in ({}, {"Authorization": "Bearer ya29.nobody-ever-issued-this"})]
say(answers=answers)
"""
    )
    assert (await client.heard())["answers"] == [200, 200]
    await client.finished()
