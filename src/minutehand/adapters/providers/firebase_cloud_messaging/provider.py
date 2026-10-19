"""Firebase Cloud Messaging, as a server sending push notifications sees it.

A server sends each message to a device's registration token with the FCM HTTP v1 API's `messages.send` (what
`firebase-admin` does for every token of a multicast). There are no devices in a run, so the registrations are the
scenario's: each seeded token is `active` or `unregistered`, and a token the seed does not name is one FCM never
issued. A message to an active token is delivered: it is answered with its name and kept, with what it carried, so
a test reads what reached each device. `validate_only` validates the same way and delivers nothing.

What the world log holds: every seeded registration and every delivered message are RECORD entities
(`RecordSnapshot`, `resource` "registration" or "message"); a message is the agent's, listed under the registration
it went to. Nothing lives outside it.

What it cannot do, loudly: a message to a topic, a condition or a Firebase Installation ID, and every other method
of the API (topic subscriptions), answer 501, naming it. No credential is ever checked. Where each behaviour comes
from is in `CLAIMS.md`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, JsonValue, ValidationError, model_validator
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, Router

from minutehand.adapters.providers.firebase_cloud_messaging import wire
from minutehand.adapters.providers.firebase_cloud_messaging.manifest import MANIFEST
from minutehand.domain.errors import NotServed, Rendered
from minutehand.domain.provider import Manifest
from minutehand.domain.scenario import Model, Scenario
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, RecordSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.provider import ASGIApp
from minutehand.ports.store import Store

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class RegistrationState(StrEnum):
    ACTIVE = "active"
    UNREGISTERED = "unregistered"


class SeededRegistration(Model):
    """A registration token a device's app was given by FCM, and whether it is still registered."""

    token: str = Field(min_length=1)
    state: RegistrationState = RegistrationState.ACTIVE


class FcmSeed(Model):
    """`ProviderSeed.body` for `firebase_cloud_messaging`."""

    tokens: list[SeededRegistration] = []

    @model_validator(mode="after")
    def _each_token_once(self) -> FcmSeed:
        given = [t.token for t in self.tokens]
        twice = sorted({t for t in given if given.count(t) > 1})
        if twice:
            raise ValueError(f"a registration token is seeded twice: {', '.join(twice)}")
        return self


class Notification(Model):
    title: str | None = None
    body: str | None = None
    image: str | None = None


class MessageRecord(Model):
    """A message FCM delivered: what reached the device, and the request that sent it, verbatim."""

    name: str
    project: str
    token: str
    data: dict[str, str]
    notification: Notification | None
    webpush_headers: dict[str, str]
    request: str = Field(description="The messages.send body as the agent sent it")


def _registration_id(token: str) -> str:
    return f"registration/{token}"


def _registration_ref(token: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=_registration_id(token))


def _message_ref(name: str) -> EntityRef:
    return EntityRef(provider=MANIFEST.key, kind=EntityKind.RECORD, external_id=name)


class Registrations:
    """The seeded registrations and the delivered messages, as the world log holds them."""

    def __init__(self, world: Store) -> None:
        self.world = world

    def registration(self, token: str) -> SeededRegistration | None:
        stored = self.world.get(_registration_ref(token))
        return SeededRegistration.model_validate_json(stored.body) if stored is not None else None

    def put_registration(self, registration: SeededRegistration) -> None:
        self.world.apply(
            Change(
                entity=_registration_ref(registration.token),
                operation=Operation.CREATE,
                actor=Actor.SCENARIO,
                body=registration.model_dump_json(),
                after=RecordSnapshot(resource="registration", text=f"{registration.token} {registration.state}"),
            )
        )

    def put_message(self, message: MessageRecord) -> None:
        said = [*(f"{k} {v}" for k, v in message.data.items())]
        if message.notification is not None:
            said += [t for t in (message.notification.title, message.notification.body) if t]
        self.world.apply(
            Change(
                entity=_message_ref(message.name),
                operation=Operation.CREATE,
                actor=Actor.AGENT,
                body=message.model_dump_json(),
                parent=_registration_id(message.token),
                after=RecordSnapshot(resource="message", text=" ".join([message.token, *said])),
            )
        )


def _parsed(raw: bytes) -> wire.SendIn:
    try:
        return wire.SendIn.model_validate_json(raw or b"{}")
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        said = f"{wire.INVALID_ARGUMENT} {problems}"
        raise wire.FcmRefusal(400, "INVALID_ARGUMENT", said, wire.bad_request("message", said)) from e


def _data(given: JsonValue) -> dict[str, str]:
    """`Message.data` as sent: a map of strings to strings, refused as FCM refuses it otherwise."""
    if given is None:
        return {}
    if not isinstance(given, dict):
        said = f"{wire.INVALID_ARGUMENT} message.data: a map (key: string, value: string)"
        raise wire.FcmRefusal(400, "INVALID_ARGUMENT", said, wire.bad_request("message.data", said))
    found: dict[str, str] = {}
    for n, (key, value) in enumerate(given.items()):
        if not isinstance(value, str):
            raise wire.invalid_value(f"message.data[{n}].value", json.dumps(value))
        found[key] = value
    reserved = [k for k in found if k in wire.RESERVED_KEYS or k.startswith(wire.RESERVED_PREFIXES)]
    if reserved:
        said = f"{wire.INVALID_ARGUMENT} message.data key {reserved[0]!r}: {wire.RESERVED_RULE}"
        raise wire.FcmRefusal(400, "INVALID_ARGUMENT", said, wire.bad_request("message.data", said))
    size = sum(len(k.encode()) + len(v.encode()) for k, v in found.items())
    if size > wire.PAYLOAD_LIMIT:
        said = f"{wire.INVALID_ARGUMENT} message.data is {size} bytes: {wire.TOO_BIG}"
        raise wire.FcmRefusal(400, "INVALID_ARGUMENT", said, wire.bad_request("message.data", said))
    return found


def _message_id(now: datetime, project: str, head: int) -> str:
    """A message id of the form FCM's reference shows (`0:1500415314455276%31bd1c9631bd1c96`,
    https://firebase.google.com/docs/cloud-messaging/send-message): the moment in microseconds and 16 hex digits,
    drawn from the project and the log's position so it is unique in the run and the same when replayed. Nothing
    more about its form is documented, and a client must not parse it."""
    micros = (now - EPOCH) // _MICROSECOND
    return f"0:{micros}%{hashlib.sha256(f'{project}@{head}'.encode()).hexdigest()[:16]}"


_MICROSECOND = datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=UTC) - EPOCH


class FcmProvider:
    manifest: Manifest = MANIFEST
    seed_model = FcmSeed

    def error(self, status: int, code: str, message: str) -> Rendered:
        return wire.error_answer(status, "UNIMPLEMENTED" if status == 501 else "INTERNAL", message, [])

    def seed(self, scenario: Scenario, world: Store) -> None:
        """The registrations `FcmSeed` declares. A `SignIn` on this provider is refused: FCM has no sign-in of its
        own (a server brings the access token Google's token endpoint mints), so it would be dropped."""
        if any(s.provider == MANIFEST.key for s in scenario.sign_ins):
            raise ValueError(
                f"a seeded sign_in is on {MANIFEST.key}, which has no sign-in of its own: an FCM client sends the "
                "access token Google's token endpoint mints, and no credential names a person here"
            )
        given = scenario.provider_seed(MANIFEST.key)
        if given is None:
            return
        registrations = Registrations(world)
        for registration in FcmSeed.model_validate_json(given.body).tokens:
            registrations.put_registration(registration)

    def app(self, world: Store, clock: Clock) -> ASGIApp:
        registrations = Registrations(world)

        async def send(request: Request) -> Response:
            raw = await request.body()
            project = f"projects/{request.path_params['project']}"
            asked = _parsed(raw)
            message = asked.message
            targets = message.targets()
            if len(targets) != 1:
                given = ", ".join(targets) or "none"
                said = f"{wire.INVALID_ARGUMENT} message sets {given}: {wire.TARGET_RULE}"
                raise wire.FcmRefusal(400, "INVALID_ARGUMENT", said, wire.bad_request("message", said))
            if message.token is None:
                raise NotServed(
                    f"fcm.projects.messages.send to a {targets[0]}: only messages to a registration token are "
                    "served, and a world's devices are its seeded registration tokens"
                )
            data = _data(message.data)
            registration = registrations.registration(message.token)
            if registration is None:
                raise wire.FcmRefusal(400, "INVALID_ARGUMENT", wire.INVALID_TOKEN, wire.fcm_error("INVALID_ARGUMENT"))
            if registration.state is RegistrationState.UNREGISTERED:
                raise wire.FcmRefusal(404, "NOT_FOUND", wire.UNREGISTERED, wire.fcm_error("UNREGISTERED"))
            name = f"{project}/messages/{_message_id(clock.now(), project, world.head())}"
            if not asked.validateOnly:
                notification = message.notification
                registrations.put_message(
                    MessageRecord(
                        name=name,
                        project=project,
                        token=message.token,
                        data=data,
                        notification=Notification.model_validate(notification.model_dump())
                        if notification is not None
                        else None,
                        webpush_headers=message.webpush.headers if message.webpush is not None else {},
                        request=raw.decode("utf-8", errors="replace"),
                    )
                )
            return Response(json.dumps({"name": name}), media_type=wire.JSON)

        async def unserved(request: Request) -> Response:
            called = wire.rest_method(request.method, request.url.path)
            if called is None:
                raise NotServed(f"no method of the FCM HTTP v1 API has the route {request.method} {request.url.path}")
            raise NotServed(f"{called}: {wire.REST_REFUSED[called]}")

        return Router(
            routes=[
                Route("/v1/projects/{project}/messages:send", send, methods=["POST"]),
                Route("/{rest:path}", unserved, methods=["GET", "POST", "PATCH", "PUT", "DELETE"]),
            ]
        )


def build() -> FcmProvider:
    return FcmProvider()
