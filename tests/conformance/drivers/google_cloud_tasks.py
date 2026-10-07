"""Google Cloud Tasks, driven through its REST API v2 (https://cloud.google.com/tasks/docs/reference/rest), as a
client on the REST transport calls it: an `Authorization: Bearer` access token on every call, proto3 JSON both ways.

The scenario's people are no Cloud Tasks principals, so this driver is in the accounts family only, and its writes
are HTTP tasks. A world is reached by the access token it claims, its own by `tag`; the queue every task goes into is
seeded through the provider's own seed (`CloudTasksSeed.queues`), as infrastructure makes a queue before an agent is
deployed. Every task is scheduled far past any clock a property sets, so none falls due while a property runs.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx

from minutehand.adapters.control.wire import Claims, CreateWorld, WorldView
from minutehand.domain.scenario import Seed
from tests.conformance.contract import Api, Driver, IdKind, PersonSeen, Session, ok

PROVIDER = "google_cloud_tasks"
API = "https://cloudtasks.googleapis.com/v2/"
LOCATION = "projects/conformance-project/locations/us-central1"
QUEUE = f"{LOCATION}/queues/conformance"
TARGET = "http://127.0.0.1:9/conformance"
"""Where each task would be delivered: this machine, as the provider calls no other, at a port nothing answers."""
DUE = "2040-01-01T00:00:00Z"
UNSEEDED = "ya29.a0unseededaccesstokennobodywasevergiven0000"
"""An access token of Google's shape (`ya29.`) that no world claims."""

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{3}|\.\d{6}|\.\d{9})?Z$")
NO_PEOPLE = (
    "the scenario's people are no Cloud Tasks principals: Cloud Tasks has queues and tasks and no accounts, and who "
    "may call it is IAM's business (https://cloud.google.com/tasks/docs/reference-access-control), so it has no "
    "account of theirs to list or act as"
)
NO_WHOAMI = (
    "the Cloud Tasks API has no call that answers who the caller is: its resources are locations, queues and tasks "
    "(https://cloud.google.com/tasks/docs/reference/rest); reading a token's identity is Google's token endpoint's "
    "business, which this provider does not serve"
)

Doc = Mapping[str, Any]
"""A JSON object as Cloud Tasks answered it, read only inside this driver."""


def token(tag: str) -> str:
    """An access token of Google's shape (`ya29.`) for the world's agent."""
    return f"ya29.c-{tag}-tasks"


def when(stamp: object) -> datetime:
    """A proto3 JSON Timestamp: RFC 3339 in UTC with `Z` and 0, 3, 6 or 9 fractional digits
    (https://protobuf.dev/reference/protobuf/google.protobuf/#timestamp); anything else is refused."""
    found = STAMP.match(stamp) if isinstance(stamp, str) else None
    if found is None:
        raise ValueError(f"{stamp!r} is not a proto3 JSON timestamp")
    fraction = (found.group(2) or ".0")[:7]
    return datetime.fromisoformat(found.group(1) + fraction).replace(tzinfo=UTC)


class TasksSession(Session):
    def __init__(self, api: Api, access: str) -> None:
        self._http = api.http({"Authorization": f"Bearer {access}"})
        self.secrets = (access, UNSEEDED)

    def close(self) -> None:
        self._http.close()

    def _json(self, what: str, method: str, path: str, **more: Any) -> Doc:
        answered = ok(what, self._http.request(method, API + path, **more)).json()
        if not isinstance(answered, dict):
            raise TypeError(f"{what} answered no JSON object")
        return answered

    # ------------------------------------------------------------------ accounts

    def whoami(self) -> str:
        raise NotImplementedError(NO_WHOAMI)

    def people(self) -> list[PersonSeen]:
        raise NotImplementedError(NO_PEOPLE)

    def people_pages(self, page_size: int) -> list[list[str]]:
        raise NotImplementedError(NO_PEOPLE)

    def unknown_credential(self) -> httpx.Response:
        return self._http.get(API + f"{LOCATION}/queues", headers={"Authorization": f"Bearer {UNSEEDED}"})

    # ------------------------------------------------------------------ queues and tasks

    def observe(self) -> str:
        """`queues.list` in the location, then `tasks.list` of each queue it lists, in the FULL view."""
        listed = self._http.get(API + f"{LOCATION}/queues")
        answered = [ok("queues.list", listed).text]
        queues = listed.json()["queues"] if "queues" in listed.json() else []
        for queue in queues:
            tasks = self._http.get(API + f"{queue['name']}/tasks", params={"responseView": "FULL"})
            answered.append(ok("tasks.list", tasks).text)
        return "\n".join(answered)

    def _create(self, label: str) -> Doc:
        """`tasks.create`: an HTTP task POSTing `label` to the target, due long after any clock a property sets
        (https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks/create). The label is
        also a header of the task's request, which the FULL view answers as written, where the body is base64."""
        task = {
            "httpRequest": {
                "url": TARGET,
                "httpMethod": "POST",
                "headers": {"Content-Type": "text/plain", "X-Conformance-Label": label},
                "body": base64.b64encode(label.encode()).decode(),
            },
            "scheduleTime": DUE,
        }
        return self._json("tasks.create", "POST", f"{QUEUE}/tasks", json={"task": task})

    def change(self, label: str) -> None:
        self._create(label)

    def stamp(self, label: str) -> tuple[str, datetime]:
        made = self._create(label)
        return str(made["name"]), when(made["createTime"])

    def stamped(self, made: str) -> datetime:
        """`tasks.get`'s `createTime`."""
        return when(self._json("tasks.get", "GET", made)["createTime"])


class TasksDriver(Driver):
    provider: ClassVar[str] = PROVIDER
    session: ClassVar[type[Session]] = TasksSession
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
    id_formats: ClassVar[Mapping[IdKind, re.Pattern[str]]] = {}
    page_floor: ClassVar[Mapping[str, int]] = {}
    unknown_refusal: ClassVar[tuple[int, str]] = (401, "Request had invalid authentication credentials")
    """Google's answer to an access token it does not accept: 401 UNAUTHENTICATED, "Request had invalid
    authentication credentials. Expected OAuth 2 access token, login cookie or other valid authentication
    credential." (https://cloud.google.com/apis/design/errors#handling_errors)."""

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise NotImplementedError(f"Cloud Tasks has no login of a person's own: {NO_PEOPLE}")
        given = seed["provider_seeds"] if "provider_seeds" in seed else []
        if not isinstance(given, list):
            raise TypeError("the seed's provider_seeds is not a list")
        ours = {"provider": PROVIDER, "body": {"queues": [{"name": QUEUE}]}}
        merged = dict(seed) | {"provider_seeds": [*given, ours]}
        return CreateWorld(seed=Seed.model_validate(merged), claims=Claims(tokens=[token(tag)]))

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        if person is not None:
            raise NotImplementedError(NO_PEOPLE)
        return TasksSession(api, self._token(world))

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        return None if person is not None else self._token(world)

    @staticmethod
    def _token(world: WorldView) -> str:
        return world.claims.tokens[0]


DRIVER = TasksDriver()
