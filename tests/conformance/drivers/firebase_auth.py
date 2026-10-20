"""Firebase Authentication, driven through Identity Toolkit v1's admin methods
(https://cloud.google.com/identity-platform/docs/reference/rest/v1/projects.accounts) as the Admin SDK calls them:
`/v1/projects/{projectId}/accounts...`, JSON both ways, no other credential needed to name the project.

A world is one Firebase project, its own by `tag`, seeded through the provider's own seed (`FirebaseAuthSeed`) and
reached by the project id its admin calls name in their path. Its accounts are the app's end users, not the
scenario's people, so this driver is in the accounts family only, and its writes are accounts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx

from minutehand.adapters.control.wire import Claims, CreateWorld, WorldView
from minutehand.domain.scenario import Seed
from tests.conformance.contract import Api, Driver, IdKind, PersonSeen, Session, ok, uncredentialed

PROVIDER = "firebase_auth"
API = "https://identitytoolkit.googleapis.com/v1/projects/"
UNSEEDED = "ya29.a0unseededaccesstokennobodywasevergiven0000"
"""An access token of Google's shape (`ya29.`) that no world claims."""

NO_PEOPLE = (
    "a Firebase project's accounts are its app's own end users, made by sign-up or the Admin SDK "
    "(https://firebase.google.com/docs/auth/admin/manage-users); the scenario's people hold no account in it, so it "
    "has no account of theirs to list or act as"
)
NO_WHOAMI = (
    "the Admin SDK's calls act as the project's service account, and Identity Toolkit has no call that answers who "
    "that is: accounts.lookup answers whose an end user's ID token is "
    "(https://cloud.google.com/identity-platform/docs/reference/rest/v1/accounts/lookup)"
)

Doc = Mapping[str, Any]
"""A JSON object as Identity Toolkit answered it, read only inside this driver."""


def project_of(tag: str) -> str:
    """A project id of the documented shape (6 to 30 lowercase letters, digits or hyphens, starting with a letter),
    unique to `tag`."""
    return f"conf-{hashlib.sha256(tag.encode()).hexdigest()[:20]}"


def when(stamp: object) -> datetime:
    """`createdAt`: milliseconds since the epoch, as a string of digits (UserInfo, the discovery document)."""
    if not isinstance(stamp, str) or not stamp.isdigit():
        raise ValueError(f"{stamp!r} is not milliseconds since the epoch")
    return datetime.fromtimestamp(int(stamp) / 1000, tz=UTC)


class FirebaseSession(Session):
    def __init__(self, api: Api, project: str) -> None:
        self._http = api.http()
        self._project = project
        self.secrets = (UNSEEDED,)

    def close(self) -> None:
        self._http.close()

    def _url(self, method: str) -> str:
        return f"{API}{self._project}/{method}"

    def _json(self, what: str, method: str, path: str, **more: Any) -> Doc:
        answered = ok(what, self._http.request(method, self._url(path), **more)).json()
        if not isinstance(answered, dict):
            raise TypeError(f"{what} answered no JSON object")
        return answered

    def whoami(self) -> str:
        raise NotImplementedError(NO_WHOAMI)

    def people(self) -> list[PersonSeen]:
        raise NotImplementedError(NO_PEOPLE)

    def people_pages(self, page_size: int) -> list[list[str]]:
        raise NotImplementedError(NO_PEOPLE)

    def stranger(self, *, credentialed: bool) -> httpx.Response:
        if credentialed:
            return self._http.get(self._url("accounts:batchGet"), headers={"Authorization": f"Bearer {UNSEEDED}"})
        return uncredentialed(self._http, "GET", self._url("accounts:batchGet"))

    def observe(self) -> str:
        """`accounts:batchGet` at its largest page."""
        return ok("batchGet", self._http.get(self._url("accounts:batchGet"), params={"maxResults": "1000"})).text

    def _create(self, label: str) -> Doc:
        made = self._json("accounts", "POST", "accounts", json={"displayName": label})
        found = self._json("accounts:lookup", "POST", "accounts:lookup", json={"localId": [made["localId"]]})
        return found["users"][0]

    def change(self, label: str) -> None:
        self._create(label)

    def stamp(self, label: str) -> tuple[str, datetime]:
        made = self._create(label)
        return str(made["localId"]), when(made["createdAt"])

    def stamped(self, made: str) -> datetime:
        found = self._json("accounts:lookup", "POST", "accounts:lookup", json={"localId": [made]})
        return when(found["users"][0]["createdAt"])


class FirebaseDriver(Driver):
    provider: ClassVar[str] = PROVIDER
    session: ClassVar[type[Session]] = FirebaseSession
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
    }
    # No id kind of the accounts family applies: the provider lists no person (`accounts.people` is absent).
    id_formats: ClassVar[Mapping[IdKind, Any]] = {}
    page_floor: ClassVar[Mapping[str, int]] = {}
    time_resolution: ClassVar[timedelta] = timedelta(milliseconds=1)
    """`createdAt` is milliseconds since the epoch."""

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise NotImplementedError(f"a Firebase project has no login of a person's own: {NO_PEOPLE}")
        given = seed["provider_seeds"] if "provider_seeds" in seed else []
        if not isinstance(given, list):
            raise TypeError("the seed's provider_seeds is not a list")
        project = project_of(tag)
        ours = {"provider": PROVIDER, "body": {"project_id": project, "api_key": f"key-{project}"}}
        merged = dict(seed) | {"provider_seeds": [*given, ours]}
        return CreateWorld(seed=Seed.model_validate(merged), claims=Claims(keys=[project]))

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        if person is not None:
            raise NotImplementedError(NO_PEOPLE)
        return FirebaseSession(api, world.claims.keys[0])


DRIVER = FirebaseDriver()
