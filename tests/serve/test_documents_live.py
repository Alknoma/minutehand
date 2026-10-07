"""What the agent does to documents is recorded the same way across document providers: a document it creates
says who owns it and the shared place it is in, and access it gives is a `GrantSnapshot` naming the document, the
person and the role; the `document_created` and `document_shared` expectations read them."""

from __future__ import annotations

import ssl
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import AccessRole, Seed
from minutehand.domain.world import Actor, DocumentSnapshot, GrantSnapshot, Operation
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served

REFRESH = "1//refresh-documents-live"
DRIVE = "https://www.googleapis.com/drive/v3"
PEOPLE = [
    {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}},
    {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": {"kind": "silent"}},
]


@contextmanager
def _drive(served: Served, *, expect: list[dict[str, object]]) -> Iterator[tuple[OpenWorld, httpx.Client]]:
    seed = Seed.model_validate(
        {
            "starts_at": "2026-09-01T09:00:00Z",
            "people": PEOPLE,
            "spaces": [{"provider": "google_workspace", "name": "Launch Team",
                        "members": [{"person": "owen", "role": "organizer"}]}],
            "sign_ins": [{"provider": "google_workspace", "credential": REFRESH, "person": "owen"}],
            "expect": expect,
        }
    )  # fmt: skip
    world = OpenWorld(
        served.client, served.client.create_world(CreateWorld(seed=seed, claims=Claims(tokens=[REFRESH])))
    )
    trust = ssl.create_default_context(cafile=served.bundle)
    try:
        with httpx.Client(proxy=served.proxy, verify=trust, trust_env=False, timeout=30) as http:
            signed = http.post(
                "https://oauth2.googleapis.com/token",
                data={"grant_type": "refresh_token", "refresh_token": REFRESH, "client_id": "c", "client_secret": "s"},
            )
            assert signed.status_code == 200, signed.text
            http.headers["Authorization"] = f"Bearer {signed.json()['access_token']}"
            yield world, http
    finally:
        served.client.close_world(world.world_id)


def _shared_drive(http: httpx.Client) -> str:
    drives = http.get(f"{DRIVE}/drives").json()["drives"]
    return next(d["id"] for d in drives if d["name"] == "Launch Team")


def test_a_document_the_agent_creates_and_shares_carries_its_owner_space_and_grant(served: Served) -> None:
    expect = [
        {"kind": "document_created", "titled": ["plan"], "space": "Launch Team", "at_least": 1},
        {"kind": "document_shared", "person": "sofia", "titled": ["plan"], "role": "writer"},
    ]
    with _drive(served, expect=expect) as (world, http):
        mine = http.post(f"{DRIVE}/files", json={"name": "Owen's notes", "mimeType": "text/plain"})
        assert mine.status_code == 200, mine.text
        made = http.post(
            f"{DRIVE}/files",
            params={"supportsAllDrives": "true"},
            json={"name": "Launch plan", "mimeType": "application/vnd.google-apps.document",
                  "parents": [_shared_drive(http)]},
        )  # fmt: skip
        assert made.status_code == 200, made.text
        shared = http.post(
            f"{DRIVE}/files/{made.json()['id']}/permissions",
            params={"supportsAllDrives": "true"},
            json={"type": "user", "role": "writer", "emailAddress": "sofia@example.com"},
        )
        assert shared.status_code == 200, shared.text

        created = {
            e.after.title: e.after
            for e in world.events(provider="google_workspace", actor=Actor.AGENT, operation=Operation.CREATE)
            if isinstance(e.after, DocumentSnapshot)
        }
        assert created["Owen's notes"].owner == "owen@example.com" and created["Owen's notes"].space is None
        assert created["Launch plan"].space == "Launch Team" and created["Launch plan"].owner is None
        grants = [e.after for e in world.events(provider="google_workspace") if isinstance(e.after, GrantSnapshot)]
        assert GrantSnapshot(document="Launch plan", to="sofia@example.com", role=AccessRole.WRITER) in grants
        met = [f for f in world.checks().result.findings if f.check == "expectations"]
        assert met and all("met by" in f.message for f in met), [f.message for f in met]


def test_the_document_expectations_fail_when_the_agent_neither_creates_in_the_space_nor_shares(
    served: Served,
) -> None:
    expect = [
        {"kind": "document_created", "titled": ["plan"], "space": "Launch Team"},
        {"kind": "document_shared", "person": "sofia", "role": "reader"},
    ]
    with _drive(served, expect=expect) as (world, http):
        made = http.post(f"{DRIVE}/files", json={"name": "Launch plan", "mimeType": "text/plain"})
        assert made.status_code == 200, made.text
        failed = [f for f in world.checks().result.findings if f.check == "expectations" and "wanted" in f.message]
        assert len(failed) == 2, [f.message for f in world.checks().result.findings]
