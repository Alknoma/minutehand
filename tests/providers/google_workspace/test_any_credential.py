"""Minutehand is a simulation and enforces no credential: every Google API answers any token, or none, and only
the shapes of requests and answers are Google's. A seeded credential acts as its person; anything else as the
seed's `unknown_credentials_act_as`, by default the scenario's owner. Who may see what is still world data."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.providers.google_workspace.app import DOCS_HOST, DRIVE_HOST, OAUTH_HOST
from minutehand.adapters.providers.google_workspace.gmail import GMAIL_HOST
from minutehand.adapters.providers.google_workspace.provider import build
from minutehand.adapters.providers.google_workspace.seed import WorkspaceSeed
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.scenario import ProviderSeed, Scenario, SignIn
from tests.providers.google_workspace.drive_world import (
    AUTH,
    SCENARIO,
    START,
    Drive,
    answer,
    client_for,
    issue,
    unsigned_assertion,
)

ABOUT = {"fields": "user(emailAddress)"}
SIGNED_IN = SCENARIO.model_copy(
    update={"sign_ins": [SignIn(provider="google_workspace", credential="1//dov-refresh", person="dov")]}
)
"""A scenario that names a sign-in: dov's refresh token acts as dov, anything else as the owner, mara."""


def about_user(response: httpx.Response) -> str:
    user = answer(response)["user"]
    assert isinstance(user, dict)
    return str(user["emailAddress"])


def seeded(tmp_path: Path, scenario: Scenario = SIGNED_IN) -> tuple[SqliteStore, RunClock]:
    clock = RunClock(START)
    store = SqliteStore(tmp_path / "w.db", "root", clock)
    build().seed(scenario, store)
    return store, clock


@pytest.fixture
async def oauth_and_drive(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    store, clock = seeded(tmp_path)
    async with (
        client_for(build(), store, clock, OAUTH_HOST) as oauth,
        client_for(build(), store, clock, DRIVE_HOST) as drive,
    ):
        yield oauth, drive


async def test_no_token_acts_as_the_owner_on_drive_docs_gmail_and_calendar(drive: Drive) -> None:
    async with (
        client_for(drive.provider, drive.store, drive.clock) as api,
        client_for(drive.provider, drive.store, drive.clock, DOCS_HOST) as docs,
        client_for(drive.provider, drive.store, drive.clock, GMAIL_HOST) as gmail,
    ):
        about = await api.get("/drive/v3/about", params=ABOUT)
        made = answer(await docs.post("/v1/documents", json={"title": "Unsigned notes"}))
        profile = answer(await gmail.get("/gmail/v1/users/me/profile"))
        calendar = answer(await api.get("/calendar/v3/calendars/primary"))
        owners = answer(await api.get(f"/drive/v3/files/{made['documentId']}", params={"fields": "owners"}))
    assert about_user(about) == "mara@example.com"
    assert profile["emailAddress"] == "mara@example.com" and calendar["id"] == "mara@example.com"
    assert [o["emailAddress"] for o in owners["owners"]] == ["mara@example.com"]  # type: ignore[union-attr]


async def test_an_unknown_token_acts_as_the_owner_and_an_expired_or_revoked_one_as_its_user(
    drive: Drive, api: httpx.AsyncClient, oauth: httpx.AsyncClient
) -> None:
    unknown = await api.get("/drive/v3/about", params=ABOUT, headers={"Authorization": "Bearer ya29.never-issued"})
    empty = await api.get("/drive/v3/about", params=ABOUT, headers={"Authorization": "Bearer "})
    issue(drive.store, "ya29.short", "dov@example.com", lasts=timedelta(hours=1))
    drive.clock.jump(START + timedelta(hours=2))
    expired = await api.get("/drive/v3/about", params=ABOUT, headers={"Authorization": "Bearer ya29.short"})
    revoked = await oauth.post("/revoke", params={"token": "ya29.short"})
    after = await api.get("/drive/v3/about", params=ABOUT, headers={"Authorization": "Bearer ya29.short"})
    again = await oauth.post("/revoke", params={"token": "ya29.short"})
    never = await oauth.post("/revoke", params={"token": "ya29.never-issued"})

    assert about_user(unknown) == about_user(empty) == "mara@example.com"
    assert about_user(expired) == about_user(after) == "dov@example.com"
    assert revoked.status_code == again.status_code == never.status_code == 200


async def test_revoke_without_a_token_is_refused_invalid_request(oauth: httpx.AsyncClient) -> None:
    """The one refusal left: a request missing Google's required `token` parameter is the wrong shape."""
    refused = await oauth.post("/revoke")
    assert answer(refused, 400) == {
        "error": "invalid_request",
        "error_description": "Missing required parameter: token",
    }


async def test_a_seeded_refresh_token_acts_as_its_person_and_any_other_as_the_owner(
    oauth_and_drive: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    oauth, drive = oauth_and_drive
    known = answer(await oauth.post("/token", data={"grant_type": "refresh_token", "refresh_token": "1//dov-refresh"}))
    stranger = answer(await oauth.post("/token", data={"grant_type": "refresh_token", "refresh_token": "1//stranger"}))
    as_bearer = await drive.get("/drive/v3/about", params=ABOUT, headers={"Authorization": "Bearer 1//dov-refresh"})

    assert set(known) == set(stranger) == {"access_token", "expires_in", "token_type"}
    assert known["token_type"] == "Bearer" and known["expires_in"] == 3599
    for token, who in [(known, "dov@example.com"), (stranger, "mara@example.com")]:
        auth = {"Authorization": f"Bearer {token['access_token']}"}
        assert about_user(await drive.get("/drive/v3/about", params=ABOUT, headers=auth)) == who
    assert about_user(as_bearer) == "dov@example.com"


async def test_a_revoked_refresh_token_signs_in_again(
    oauth_and_drive: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    oauth, drive = oauth_and_drive
    grant = {"grant_type": "refresh_token", "refresh_token": "1//dov-refresh"}
    answer(await oauth.post("/token", data=grant))
    assert (await oauth.post("/revoke", data={"token": "1//dov-refresh"})).status_code == 200
    again = answer(await oauth.post("/token", data=grant))
    auth = {"Authorization": f"Bearer {again['access_token']}"}
    assert about_user(await drive.get("/drive/v3/about", params=ABOUT, headers=auth)) == "dov@example.com"


async def test_an_authorization_code_is_answered_with_a_refresh_token_that_signs_in_again(
    oauth_and_drive: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """Google's answer to the code grant: access_token, expires_in, refresh_token, scope and token_type
    (https://developers.google.com/identity/protocols/oauth2/web-server#exchange-authorization-code)."""
    oauth, drive = oauth_and_drive
    form = {
        "grant_type": "authorization_code",
        "code": "4/0-any-code",
        "client_id": "any.apps.googleusercontent.com",
        "client_secret": "any",
        "redirect_uri": "http://localhost:8080/",
        "scope": "https://www.googleapis.com/auth/drive",
    }
    coded = answer(await oauth.post("/token", data=form))
    refreshed = answer(
        await oauth.post("/token", data={"grant_type": "refresh_token", "refresh_token": str(coded["refresh_token"])})
    )

    assert set(coded) == {"access_token", "expires_in", "refresh_token", "scope", "token_type"}
    assert coded["scope"] == "https://www.googleapis.com/auth/drive"
    assert str(coded["refresh_token"]).startswith("1//")
    for token in (coded, refreshed):
        auth = {"Authorization": f"Bearer {token['access_token']}"}
        assert about_user(await drive.get("/drive/v3/about", params=ABOUT, headers=auth)) == "mara@example.com"


async def test_any_service_account_assertion_signs_in(
    oauth_and_drive: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    """A service account nobody declared, or an assertion that is no JWT, signs in as the owner; one
    impersonating a user the world holds acts as that user."""
    oauth, drive = oauth_and_drive
    bearer = "urn:ietf:params:oauth:grant-type:jwt-bearer"
    who: list[str] = []
    for assertion in [
        unsigned_assertion("nobody@x.iam.gserviceaccount.com"),
        "not-a-jwt",
        unsigned_assertion("nobody@x.iam.gserviceaccount.com", subject="dov@example.com"),
        unsigned_assertion("nobody@x.iam.gserviceaccount.com", subject="nobody@example.com"),
    ]:
        token = answer(await oauth.post("/token", data={"grant_type": bearer, "assertion": assertion}))
        auth = {"Authorization": f"Bearer {token['access_token']}"}
        who.append(about_user(await drive.get("/drive/v3/about", params=ABOUT, headers=auth)))
    assert who == ["mara@example.com", "mara@example.com", "dov@example.com", "mara@example.com"]


@pytest.mark.parametrize(
    ("form", "missing"),
    [
        ({"grant_type": "refresh_token"}, "refresh_token"),
        ({"grant_type": "authorization_code"}, "code"),
        ({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer"}, "assertion"),
    ],
)
async def test_a_grant_missing_its_required_parameter_is_refused_invalid_request(
    oauth: httpx.AsyncClient, form: dict[str, str], missing: str
) -> None:
    refused = answer(await oauth.post("/token", data=form), 400)
    assert refused == {"error": "invalid_request", "error_description": f"Missing required parameter: {missing}"}


async def test_the_seed_names_who_an_unknown_credential_acts_as(tmp_path: Path) -> None:
    named = SCENARIO.model_copy(
        update={
            "provider_seeds": [
                ProviderSeed(
                    provider="google_workspace", body=WorkspaceSeed(unknown_credentials_act_as="dov").model_dump_json()
                )
            ]
        }
    )
    store, clock = seeded(tmp_path, named)
    async with client_for(build(), store, clock) as api:
        nobody = await api.get("/drive/v3/about", params=ABOUT)
        stranger = await api.get("/drive/v3/about", params=ABOUT, headers={"Authorization": "Bearer ya29.anything"})
    assert about_user(nobody) == about_user(stranger) == "dov@example.com"


def test_a_seed_naming_nobody_to_act_as_is_refused(tmp_path: Path) -> None:
    named = SCENARIO.model_copy(
        update={
            "provider_seeds": [
                ProviderSeed(
                    provider="google_workspace", body=WorkspaceSeed(unknown_credentials_act_as="zed").model_dump_json()
                )
            ]
        }
    )
    with pytest.raises(ValueError, match=r"unknown_credentials_act_as names 'zed', who is not a person"):
        seeded(tmp_path, named)


async def test_another_users_mailbox_is_still_refused_whatever_the_credential(drive: Drive) -> None:
    """Credentials are not enforced; whose mailbox it is still is world data."""
    async with client_for(drive.provider, drive.store, drive.clock, GMAIL_HOST) as gmail:
        mine = await gmail.get("/gmail/v1/users/mara@example.com/profile", headers=AUTH)
        theirs = await gmail.get("/gmail/v1/users/dov@example.com/profile")
    assert answer(mine)["emailAddress"] == "mara@example.com"
    assert answer(theirs, 403)["error"]["message"] == "Delegation denied for mara@example.com"  # type: ignore[index]
