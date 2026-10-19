"""Firebase Cloud Messaging, driven through its HTTP v1 API (https://firebase.google.com/docs/reference/fcm/rest/v1/
projects.messages), as a server sends with it: an `Authorization: Bearer` access token on every call, proto3 JSON
both ways.

FCM has no accounts of the scenario's people, so this driver is in the accounts family only, and its writes are
messages to a device. A world is reached by the access token it claims, its own by `tag`; its devices are seeded
through the provider's own seed (`FcmSeed.tokens`), as each device's app was given its registration token.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import ClassVar

import httpx

from minutehand.adapters.control.wire import Claims, CreateWorld, WorldView
from minutehand.domain.scenario import Seed
from tests.conformance.contract import Api, Driver, IdKind, PersonSeen, Session, ok

PROVIDER = "firebase_cloud_messaging"
SEND = "https://fcm.googleapis.com/v1/projects/conformance-project/messages:send"
DEVICE = "fGcm-conformance-device:APA91bH-active"
GONE = "fGcm-conformance-device:APA91bH-unregistered"
UNSEEDED = "ya29.a0unseededaccesstokennobodywasevergiven0000"
"""An access token of Google's shape (`ya29.`) that no world claims."""

NO_PEOPLE = (
    "the scenario's people are no FCM principals: FCM's resources are messages and a device's topic "
    "subscriptions, and who may send is IAM's business (https://firebase.google.com/docs/reference/fcm/rest), so it "
    "has no account of theirs to list or act as"
)
NO_WHOAMI = (
    "the FCM HTTP v1 API has no call that answers who the caller is (https://firebase.google.com/docs/reference/fcm/"
    "rest); reading a token's identity is Google's token endpoint's business, which this provider does not serve"
)
NO_TIME = (
    "a Message carries no time: messages.send answers only its name, and the Message resource has no timestamp "
    "(https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages)"
)
NO_READ_BACK = (
    "nothing written through the API is ever read back: messages.send is the API's only message method and no "
    "method lists or gets a message (https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages)"
)


def token(tag: str) -> str:
    """An access token of Google's shape (`ya29.`) for the world's agent."""
    return f"ya29.c-{tag}-fcm"


class FcmSession(Session):
    def __init__(self, api: Api, access: str) -> None:
        self._http = api.http({"Authorization": f"Bearer {access}"})
        self.secrets = (access, UNSEEDED)

    def close(self) -> None:
        self._http.close()

    def whoami(self) -> str:
        raise NotImplementedError(NO_WHOAMI)

    def people(self) -> list[PersonSeen]:
        raise NotImplementedError(NO_PEOPLE)

    def people_pages(self, page_size: int) -> list[list[str]]:
        raise NotImplementedError(NO_PEOPLE)

    def stranger(self, *, credentialed: bool) -> httpx.Response:
        body = {"message": {"token": DEVICE}, "validate_only": True}
        if credentialed:
            return self._http.post(SEND, json=body, headers={"Authorization": f"Bearer {UNSEEDED}"})
        request = self._http.build_request("POST", SEND, json=body)
        request.headers.pop("authorization", None)
        return self._http.send(request)

    def observe(self) -> str:
        """A dry run to the seeded unregistered device: what FCM says of it, the 404 UNREGISTERED it documents
        (https://firebase.google.com/docs/reference/fcm/rest/v1/ErrorCode); any other refusal is the vendor's."""
        answered = self._http.post(SEND, json={"message": {"token": GONE}, "validate_only": True})
        if answered.status_code != 404:
            ok("messages.send", answered)
        return f"{answered.status_code} {answered.text}"

    def change(self, label: str) -> None:
        """A message to the seeded device whose data holds `label`."""
        ok("messages.send", self._http.post(SEND, json={"message": {"token": DEVICE, "data": {"label": label}}}))


class FcmDriver(Driver):
    provider: ClassVar[str] = PROVIDER
    session: ClassVar[type[Session]] = FcmSession
    absent: ClassVar[Mapping[str, str]] = {
        "accounts.people": NO_PEOPLE,
        "accounts.whoami": NO_WHOAMI,
        "accounts.person_credentials": NO_PEOPLE,
        "accounts.title": NO_PEOPLE,
        "accounts.bot": NO_PEOPLE,
        "accounts.guest": NO_PEOPLE,
        "accounts.deactivated": NO_PEOPLE,
        "accounts.no_email": NO_PEOPLE,
        "accounts.vendor_login": NO_PEOPLE,
        "listing.people": NO_PEOPLE,
        "time.stamp": NO_TIME,
        "state.read_back": NO_READ_BACK,
    }
    # No id kind of the accounts family applies: the provider lists no person (`accounts.people` is absent).
    id_formats: ClassVar[Mapping[IdKind, re.Pattern[str]]] = {}
    page_floor: ClassVar[Mapping[str, int]] = {}

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise NotImplementedError(f"FCM has no login of a person's own: {NO_PEOPLE}")
        given = seed["provider_seeds"] if "provider_seeds" in seed else []
        if not isinstance(given, list):
            raise TypeError("the seed's provider_seeds is not a list")
        ours = {"provider": PROVIDER, "body": {"tokens": [{"token": DEVICE}, {"token": GONE, "state": "unregistered"}]}}
        merged = dict(seed) | {"provider_seeds": [*given, ours]}
        return CreateWorld(seed=Seed.model_validate(merged), claims=Claims(tokens=[token(tag)]))

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        if person is not None:
            raise NotImplementedError(NO_PEOPLE)
        return FcmSession(api, world.claims.tokens[0])

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        return None if person is not None else world.claims.tokens[0]


DRIVER = FcmDriver()
