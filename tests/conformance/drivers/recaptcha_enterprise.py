"""reCAPTCHA Enterprise, driven through its REST API v1 (https://cloud.google.com/recaptcha/docs/reference/rest), as a
page's backend calls it: an `Authorization: Bearer` access token on every call, proto3 JSON both ways.

reCAPTCHA has no accounts of the scenario's people, so this driver is in the accounts family only, and its writes
are assessments. A world is reached by the access token it claims, its own by `tag`; its tokens are seeded through
the provider's own seed (`RecaptchaSeed.tokens`), as a browser's grecaptcha would have minted them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from minutehand.adapters.control.wire import Claims, CreateWorld, WorldView
from minutehand.domain.scenario import Seed
from tests.conformance.contract import Api, Driver, IdKind, PersonSeen, Session, ok

PROVIDER = "recaptcha_enterprise"
ASSESSMENTS = "https://recaptchaenterprise.googleapis.com/v1/projects/conformance-project/assessments"
SEEDED = "03AFcWeA-conformance-seeded"
UNMINTED = "03AFcWeA-conformance-never-minted"
"""A token no browser minted in any world: assessed, it answers MALFORMED."""
UNSEEDED = "ya29.a0unseededaccesstokennobodywasevergiven0000"
"""An access token of Google's shape (`ya29.`) that no world claims."""

NO_PEOPLE = (
    "the scenario's people are no reCAPTCHA Enterprise principals: its resources are assessments, keys, firewall "
    "policies and related account groups, and who may call it is IAM's business "
    "(https://cloud.google.com/recaptcha/docs/reference/rest), so it has no account of theirs to list or act as"
)
NO_WHOAMI = (
    "the reCAPTCHA Enterprise API has no call that answers who the caller is "
    "(https://cloud.google.com/recaptcha/docs/reference/rest); reading a token's identity is Google's token "
    "endpoint's business, which this provider does not serve"
)
NO_TIME = (
    "an Assessment carries no time of its own: its only timestamp, tokenProperties.createTime, is when the browser "
    "minted the token, and no method reads an assessment again "
    "(https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments)"
)
NO_READ_BACK = (
    "nothing written through the API is ever read back: createAssessment answers from the token alone and no "
    "method lists or gets an assessment (https://cloud.google.com/recaptcha/docs/reference/rest/v1/projects.assessments)"
)

Doc = Mapping[str, Any]


def token(tag: str) -> str:
    """An access token of Google's shape (`ya29.`) for the world's agent."""
    return f"ya29.c-{tag}-recaptcha"


class RecaptchaSession(Session):
    def __init__(self, api: Api, access: str) -> None:
        self._http = api.http({"Authorization": f"Bearer {access}"})
        self.secrets = (access, UNSEEDED)

    def close(self) -> None:
        self._http.close()

    def _assess(self, what: str, event: Mapping[str, str]) -> httpx.Response:
        return ok(what, self._http.post(ASSESSMENTS, json={"event": dict(event)}))

    def whoami(self) -> str:
        raise NotImplementedError(NO_WHOAMI)

    def people(self) -> list[PersonSeen]:
        raise NotImplementedError(NO_PEOPLE)

    def people_pages(self, page_size: int) -> list[list[str]]:
        raise NotImplementedError(NO_PEOPLE)

    def stranger(self, *, credentialed: bool) -> httpx.Response:
        body = {"event": {"token": UNMINTED}}
        if credentialed:
            return self._http.post(ASSESSMENTS, json=body, headers={"Authorization": f"Bearer {UNSEEDED}"})
        request = self._http.build_request("POST", ASSESSMENTS, json=body)
        request.headers.pop("authorization", None)
        return self._http.send(request)

    def observe(self) -> str:
        """An assessment of a token no browser minted: what the world says of it."""
        answered = self._assess("assessments.create", {"token": UNMINTED, "expectedAction": "observe"}).json()
        return str(answered["tokenProperties"])

    def change(self, label: str) -> None:
        """An assessment of the seeded token, its expected action `label`."""
        self._assess("assessments.create", {"token": SEEDED, "expectedAction": label})


class RecaptchaDriver(Driver):
    provider: ClassVar[str] = PROVIDER
    session: ClassVar[type[Session]] = RecaptchaSession
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
            raise NotImplementedError(f"reCAPTCHA Enterprise has no login of a person's own: {NO_PEOPLE}")
        given = seed["provider_seeds"] if "provider_seeds" in seed else []
        if not isinstance(given, list):
            raise TypeError("the seed's provider_seeds is not a list")
        ours = {"provider": PROVIDER, "body": {"tokens": [{"token": SEEDED, "action": "checkout", "score": 0.9}]}}
        merged = dict(seed) | {"provider_seeds": [*given, ours]}
        return CreateWorld(seed=Seed.model_validate(merged), claims=Claims(tokens=[token(tag)]))

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        if person is not None:
            raise NotImplementedError(NO_PEOPLE)
        return RecaptchaSession(api, world.claims.tokens[0])

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        return None if person is not None else world.claims.tokens[0]


DRIVER = RecaptchaDriver()
