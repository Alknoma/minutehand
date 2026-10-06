"""A seeded Drive over a real `SqliteStore` and `RunClock`, driven through the ASGI app."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.google_drive import state, wire
from minutehand.adapters.providers.google_drive.app import DOCS_HOST, DRIVE_HOST, OAUTH_HOST, SLIDES_HOST
from minutehand.adapters.providers.google_drive.provider import GoogleDriveProvider, build
from minutehand.adapters.providers.google_drive.state import DriveWorld
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import Person, Scenario, SeededDocument

START = datetime(2026, 9, 14, 8, 30, 0, tzinfo=UTC)
LATER = START + timedelta(hours=3, minutes=7, seconds=11, milliseconds=250)
TOKEN = "ya29.a token the run issued to the owner"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OWNER = "mara@example.com"
ROOT_ID = state.root_id(OWNER)
"""The owner's My Drive: with no sign-in declared, the agent signs in as the owner."""

DOC = "application/vnd.google-apps.document"
FOLDER = "application/vnd.google-apps.folder"

SCENARIO = Scenario(
    name="supplier_review",
    goal="Every supplier has a reviewed contract summary.",
    owner="mara",
    starts_at=START,
    people=[
        Person(key="mara", name="Mara Lindqvist", email="mara@example.com", title="Procurement lead"),
        Person(key="dov", name="Dov Aranha", email="dov@example.com"),
    ],
    documents=[
        SeededDocument(
            provider="google_drive",
            title="Supplier Shortlist",
            text="Three suppliers remain.\nPrices due Friday.",
            folder="Procurement",
        ),
        SeededDocument(provider="google_drive", title="Kickoff Notes", text="The review starts in September."),
        SeededDocument(provider="slack", title="Not a Drive file", text="belongs to another provider"),
    ],
)


@dataclass
class Drive:
    provider: GoogleDriveProvider
    store: SqliteStore
    clock: RunClock
    path: Path

    @property
    def world(self) -> DriveWorld:
        return DriveWorld(self.store)


@pytest.fixture
def drive(tmp_path: Path) -> Drive:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "world.db", "root", clock)
    provider = build()
    provider.seed(SCENARIO, store)
    issue(store, TOKEN, OWNER)
    return Drive(provider=provider, store=store, clock=clock, path=tmp_path / "world.db")


def issue(store: SqliteStore, token: str, email: str, *, lasts: timedelta = timedelta(days=30)) -> None:
    """A token as `/token` would have issued it, lasting long enough for a test that jumps the clock. The token
    endpoint itself is tested through Google's own client."""
    DriveWorld(store).keep_token(
        token,
        wire.AccessToken(email=email, expires=wire.rfc3339(START + lasts), credential=state.ANY_CREDENTIAL),
    )


def client_for(
    provider: GoogleDriveProvider, store: SqliteStore, clock: RunClock, host: str = DRIVE_HOST
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=provider.app(store, clock)), base_url=f"https://{host}")


@pytest.fixture
async def api(drive: Drive) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(drive.provider, drive.store, drive.clock) as c:
        yield c


@pytest.fixture
async def docs(drive: Drive) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(drive.provider, drive.store, drive.clock, DOCS_HOST) as c:
        yield c


@pytest.fixture
async def presentations(drive: Drive) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(drive.provider, drive.store, drive.clock, SLIDES_HOST) as c:
        yield c


@pytest.fixture
async def oauth(drive: Drive) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(drive.provider, drive.store, drive.clock, OAUTH_HOST) as c:
        yield c


Answer = dict[str, object]


def answer(response: httpx.Response, status: int = 200) -> Answer:
    assert response.status_code == status, response.text
    found = json.loads(response.text)
    assert isinstance(found, dict)
    return found


def files_of(listed: Answer) -> list[dict[str, object]]:
    found = listed["files"]
    assert isinstance(found, list)
    return found


def names_of(listed: Answer) -> list[str]:
    return sorted(str(f["name"]) for f in files_of(listed))


def reason_of(response: httpx.Response, status: int) -> str:
    """The `reason` of a Drive error, after checking the envelope is Google's."""
    body = answer(response, status)
    error = body["error"]
    assert isinstance(error, dict) and error["code"] == status and isinstance(error["message"], str)
    errors = error["errors"]
    assert isinstance(errors, list) and len(errors) == 1
    assert set(errors[0]) >= {"domain", "reason", "message"}
    return str(errors[0]["reason"])


async def create_doc(api: httpx.AsyncClient, name: str, text: str, *, parent: str | None = None) -> Answer:
    """A Google Doc uploaded the way googleapiclient sends it: multipart/related, metadata then text/plain."""
    metadata: dict[str, object] = {"name": name, "mimeType": DOC}
    if parent is not None:
        metadata["parents"] = [parent]
    return answer(
        await api.post(
            "/upload/drive/v3/files",
            params={"uploadType": "multipart", "fields": "id,name,mimeType,parents,createdTime,modifiedTime"},
            content=multipart(json.dumps(metadata), text.encode(), "text/plain"),
            headers={**AUTH, "Content-Type": 'multipart/related; boundary="===b0undary=="'},
        )
    )


def multipart(metadata: str, media: bytes, media_type: str, *, newline: bytes = b"\n") -> bytes:
    n = newline
    return (
        b"--===b0undary=="
        + n
        + b"Content-Type: application/json"
        + n
        + b"MIME-Version: 1.0"
        + n
        + n
        + metadata.encode()
        + n
        + b"--===b0undary=="
        + n
        + b"Content-Type: "
        + media_type.encode()
        + n
        + b"MIME-Version: 1.0"
        + n
        + b"Content-Transfer-Encoding: binary"
        + n
        + n
        + media
        + n
        + b"--===b0undary==--"
    )


async def listed(api: httpx.AsyncClient, q: str, **params: str) -> Answer:
    return answer(
        await api.get(
            "/drive/v3/files", params={"q": q, "fields": "nextPageToken,files(id,name)", **params}, headers=AUTH
        )
    )


def unsigned_assertion(issuer: str, *, subject: str | None = None) -> str:
    """A service account's JWT as google-auth shapes it, signed by nobody: the fake reads its claims only."""

    def part(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    claims: dict[str, object] = {"iss": issuer, "scope": "https://www.googleapis.com/auth/drive", "aud": "x"}
    if subject is not None:
        claims["sub"] = subject
    return f"{part({'alg': 'RS256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJl"
