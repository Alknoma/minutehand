"""Firebase Cloud Messaging, reached the way a server reaches it: Google's own `firebase-admin`, signed in with a
user's refresh token (`credentials.RefreshToken`), in a process of its own configured only by the environment
Minutehand hands an agent, through the proxy; and the HTTP v1 API called with stock `google-auth` where
`firebase-admin` refuses a message itself before sending it. The refresh grant is answered by the Google Workspace
provider; the sends by this one."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand.adapters.providers.firebase_cloud_messaging.manifest import MANIFEST
from minutehand.adapters.providers.firebase_cloud_messaging.provider import (
    FcmSeed,
    MessageRecord,
    RegistrationState,
    SeededRegistration,
    build,
)
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import AfterScript, Person, ProviderSeed, Scenario, Scripted, SignIn
from minutehand.domain.world import Actor, EntityKind, RecordSnapshot
from tests.providers.google_workspace.proxied import Client, client_environment

pytestmark = pytest.mark.timeout(120)

START = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
REFRESH = "1//omar-fcm-refresh-token"
PROJECT = "shop-app"
ACTIVE = "fGcm-active-phone:APA91bH-phone"
LAPTOP = "fGcm-active-laptop:APA91bH-laptop"
GONE = "fGcm-uninstalled:APA91bH-gone"

PRELUDE = f"""
import json, sys
import firebase_admin
from firebase_admin import credentials, messaging, exceptions
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import AuthorizedSession

app = firebase_admin.initialize_app(
    credentials.RefreshToken({{"type": "authorized_user", "client_id": "1234.apps.googleusercontent.com",
                              "client_secret": "client-secret", "refresh_token": {REFRESH!r}}}),
    {{"projectId": {PROJECT!r}}})
creds = Credentials(token=None, refresh_token={REFRESH!r}, token_uri="https://oauth2.googleapis.com/token",
                    client_id="1234.apps.googleusercontent.com", client_secret="client-secret",
                    scopes=["https://www.googleapis.com/auth/firebase.messaging"])
session = AuthorizedSession(creds)
SEND = "https://fcm.googleapis.com/v1/projects/{PROJECT}/messages:send"

def say(**found):
    print(json.dumps(found), flush=True)

def outcome(response):
    if response.success:
        return ["sent", response.message_id]
    return [type(response.exception).__name__, response.exception.code, str(response.exception)]

def raw(message, **more):
    answered = session.post(SEND, json={{"message": message, **more}})
    return [answered.status_code, answered.json()]
"""


@dataclass
class Fcm:
    store: SqliteStore
    environment: dict[str, str]

    async def client(self, program: str) -> Client:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            PRELUDE + program,
            env=self.environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return Client(child)

    def messages(self) -> list[MessageRecord]:
        return [
            MessageRecord.model_validate_json(self.store.versions(e.entity)[-1].body)
            for e in self.store.events()
            if e.entity.provider == MANIFEST.key and e.actor is Actor.AGENT
        ]

    def sends(self) -> list[tuple[str, str, int | None]]:
        return [
            (c.exchange.method, c.exchange.path, c.exchange.status)
            for c in self.store.calls()
            if c.exchange.host == "fcm.googleapis.com"
        ]


SEED = FcmSeed(
    tokens=[
        SeededRegistration(token=ACTIVE),
        SeededRegistration(token=LAPTOP),
        SeededRegistration(token=GONE, state=RegistrationState.UNREGISTERED),
    ]
)


def scenario(given: FcmSeed) -> Scenario:
    return Scenario(
        name="order_updates",
        goal="Every customer hears when their order ships.",
        owner="omar",
        starts_at=START,
        people=[
            Person(key="omar", name="Omar Haddad", email="omar@example.com", reply=Scripted(then=AfterScript.SILENT))
        ],
        sign_ins=[SignIn(provider="google_workspace", credential=REFRESH, person="omar")],
        provider_seeds=[ProviderSeed(provider=MANIFEST.key, body=given.model_dump_json())],
    )


async def opened(tmp_path: Path, given: FcmSeed) -> AsyncIterator[Fcm]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    registry = Registry()
    registry.discover("minutehand.adapters.providers")
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        proxy.mount(store, clock, {}, scenario=scenario(given))
        yield Fcm(store, client_environment(proxy))


@pytest.fixture
async def fcm(tmp_path: Path) -> AsyncIterator[Fcm]:
    async for found in opened(tmp_path, SEED):
        yield found


async def test_a_multicast_is_one_send_per_token_and_each_outcome_is_firebase_admins_own(fcm: Fcm) -> None:
    client = await fcm.client(
        f"""
multicast = messaging.MulticastMessage(
    tokens=[{ACTIVE!r}, {GONE!r}, "fGcm-never-issued", {LAPTOP!r}],
    data={{"order": "A-1043", "status": "shipped"}},
    notification=messaging.Notification(title="Your order shipped", body="A-1043 is on its way"),
    webpush=messaging.WebpushConfig(headers={{"Urgency": "high", "TTL": "3600"}}))
answered = messaging.send_each_for_multicast(multicast)
say(outcomes=[outcome(r) for r in answered.responses], sent=answered.success_count, failed=answered.failure_count)
"""
    )
    heard = await client.heard()
    await client.finished()

    outcomes = heard["outcomes"]
    assert isinstance(outcomes, list)
    phone, gone, never, laptop = outcomes
    assert phone[0] == "sent" and str(phone[1]).startswith(f"projects/{PROJECT}/messages/0:")
    assert laptop[0] == "sent" and laptop[1] != phone[1]
    assert gone == ["UnregisteredError", "NOT_FOUND", "Requested entity was not found."]
    assert never == [
        "InvalidArgumentError",
        "INVALID_ARGUMENT",
        "The registration token is not a valid FCM registration token",
    ]
    assert (heard["sent"], heard["failed"]) == (2, 2)

    sends = fcm.sends()
    assert sorted(sends) == sorted(
        [("POST", f"/v1/projects/{PROJECT}/messages:send", s) for s in (200, 404, 400, 200)]
    ), "firebase-admin sends each token its own messages:send, never the batch endpoint"

    delivered = sorted(fcm.messages(), key=lambda m: m.token)
    assert [m.token for m in delivered] == sorted([ACTIVE, LAPTOP])
    for message in delivered:
        assert message.data == {"order": "A-1043", "status": "shipped"}
        assert message.notification is not None
        assert (message.notification.title, message.notification.body) == ("Your order shipped", "A-1043 is on its way")
        assert message.webpush_headers == {"Urgency": "high", "TTL": "3600"}
        assert json.loads(message.request)["message"]["token"] == message.token
    made = [e for e in fcm.store.events() if e.entity.provider == MANIFEST.key and e.actor is Actor.AGENT]
    assert {e.entity.kind for e in made} == {EntityKind.RECORD}
    assert all(isinstance(e.after, RecordSnapshot) and e.after.resource == "message" for e in made)


async def test_a_dry_run_validates_as_a_send_does_and_delivers_nothing(fcm: Fcm) -> None:
    client = await fcm.client(
        f"""
dry = messaging.send(messaging.Message(token={ACTIVE!r}, data={{"order": "A-1"}}), dry_run=True)
try:
    messaging.send(messaging.Message(token={GONE!r}, data={{"order": "A-1"}}), dry_run=True)
    gone = None
except messaging.UnregisteredError as error:
    gone = type(error).__name__
say(dry=dry, gone=gone, body=raw({{"token": {ACTIVE!r}}}, validateOnly=True))
"""
    )
    heard = await client.heard()
    await client.finished()
    assert str(heard["dry"]).startswith(f"projects/{PROJECT}/messages/")
    assert heard["gone"] == "UnregisteredError"
    body = heard["body"]
    assert isinstance(body, list) and body[0] == 200
    assert fcm.messages() == [], "validate_only sends nothing"
    sent = [
        json.loads(c.exchange.request_body or "{}")
        for c in fcm.store.calls()
        if c.exchange.host == "fcm.googleapis.com"
    ]
    assert sent[0]["validate_only"] is True, "firebase-admin spells it validate_only"


async def test_a_message_with_no_target_or_two_is_refused_invalid_argument(fcm: Fcm) -> None:
    client = await fcm.client(
        f"""
say(none=raw({{"data": {{"a": "b"}}}}), two=raw({{"token": {ACTIVE!r}, "topic": "weather"}}))
"""
    )
    heard = await client.heard()
    await client.finished()
    none, two = heard["none"], heard["two"]
    assert isinstance(none, list) and isinstance(two, list)
    for status, body in (none, two):
        assert status == 400 and body["error"]["status"] == "INVALID_ARGUMENT"
        assert "mutually exclusive fields: token, fid, topic, condition" in body["error"]["message"]
    assert (
        "message sets none" in none[1]["error"]["message"] and "message sets token, topic" in two[1]["error"]["message"]
    )


async def test_a_data_value_that_is_not_a_string_is_refused_as_fcm_words_it(fcm: Fcm) -> None:
    client = await fcm.client(f"""say(answer=raw({{"token": {ACTIVE!r}, "data": {{"order": "A-1", "count": 12}}}}))""")
    heard = await client.heard()
    await client.finished()
    assert heard["answer"] == [
        400,
        {
            "error": {
                "code": 400,
                "message": "Invalid value at 'message.data[1].value' (TYPE_STRING), 12",
                "status": "INVALID_ARGUMENT",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.BadRequest",
                        "fieldViolations": [
                            {
                                "field": "message.data[1].value",
                                "description": "Invalid value at 'message.data[1].value' (TYPE_STRING), 12",
                            }
                        ],
                    }
                ],
            }
        },
    ]


async def test_a_payload_over_4096_bytes_or_a_reserved_key_is_refused_and_4096_is_sent(fcm: Fcm) -> None:
    client = await fcm.client(
        f"""
say(exact=raw({{"token": {ACTIVE!r}, "data": {{"k": "v" * 4095}}}}),
    over=raw({{"token": {ACTIVE!r}, "data": {{"k": "v" * 4096}}}}),
    reserved=raw({{"token": {ACTIVE!r}, "data": {{"google.sent_at": "now"}}}}),
    from_=raw({{"token": {ACTIVE!r}, "data": {{"from": "shop"}}}}))
"""
    )
    heard = await client.heard()
    await client.finished()
    exact, over, reserved, from_ = heard["exact"], heard["over"], heard["reserved"], heard["from_"]
    assert isinstance(exact, list) and exact[0] == 200
    assert isinstance(over, list) and over[0] == 400 and "message.data is 4097 bytes" in over[1]["error"]["message"]
    for refused in (reserved, from_):
        assert isinstance(refused, list) and refused[0] == 400
        assert "The key should not be a reserved word" in refused[1]["error"]["message"]
    assert len(fcm.messages()) == 1


async def test_a_message_to_a_topic_or_a_condition_is_refused_by_name(fcm: Fcm) -> None:
    client = await fcm.client(
        """
try:
    messaging.send(messaging.Message(topic="weather", data={"a": "b"}))
    topic = None
except exceptions.FirebaseError as error:
    topic = [type(error).__name__, str(error)]
say(topic=topic, condition=raw({"condition": "'a' in topics"}))
"""
    )
    heard = await client.heard()
    await client.finished()
    topic, condition = heard["topic"], heard["condition"]
    assert isinstance(topic, list) and "fcm.projects.messages.send to a topic" in str(topic[1])
    assert isinstance(condition, list) and condition[0] == 501
    assert "fcm.projects.messages.send to a condition" in condition[1]["error"]["message"]


def test_a_seeded_sign_in_on_fcm_is_refused_at_seeding(tmp_path: Path) -> None:
    signed = scenario(SEED).model_copy(
        update={"sign_ins": [SignIn(provider=MANIFEST.key, credential="ya29.omar", person="omar")]}
    )
    with pytest.raises(ValueError, match=r"a seeded sign_in is on firebase_cloud_messaging, which has no sign-in"):
        build().seed(signed, SqliteStore(tmp_path / "world.db", "run", RunClock(START)))


def test_a_seed_naming_a_token_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="a registration token is seeded twice: t1"):
        FcmSeed(tokens=[SeededRegistration(token="t1"), SeededRegistration(token="t1")])
