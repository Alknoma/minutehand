"""What `googleapiclient` raises when the fake cannot answer: an endpoint Drive has and the fake does not build, and
Minutehand's own error. Each is the library's own `HttpError` with the message as its reason, in Google's envelope,
marked `x-minutehand-answer` so nobody mistakes it for Google's answer; a refusal Google itself makes is not marked,
and a fault the scenario armed is recorded as one."""

from __future__ import annotations

import httpx
import pytest
from google.oauth2 import credentials as user_credentials
from googleapiclient.errors import HttpError

from minutehand.adapters.answering import ANSWER_HEADER, INTERNAL_PREFIX, OUTCOME, Outcome
from minutehand.adapters.providers.google_drive.app import DOCS_HOST, OAUTH_HOST
from minutehand.domain.errors import AnswerKind
from minutehand.ports.provider import DeliversInBackground
from tests.providers.google_drive.drive_world import AUTH, TOKEN, Drive, answer, client_for
from tests.providers.google_drive.test_drive_google_client import Google, off_loop

MISSING = "1F00009999doesnotexist0000"


def signed_in() -> user_credentials.Credentials:
    """The owner's access token as the run issued it: no sign-in call, so the first call is the one under test."""
    return user_credentials.Credentials(token=TOKEN)


async def raised(call: object) -> HttpError:
    assert callable(call)
    with pytest.raises(HttpError) as caught:
        await off_loop(call)
    return caught.value


async def test_an_endpoint_the_fake_does_not_build_is_raised_by_the_client_naming_it(google: Google) -> None:
    revisions = google.drive(signed_in()).revisions()
    error = await raised(lambda: revisions.list(fileId=MISSING).execute())

    assert error.resp.status == 501
    assert error.resp[ANSWER_HEADER] == "not_implemented"
    assert f"does not implement this operation: GET {google.base.removeprefix('https://')}/drive/v3/files/" in str(
        error
    )
    assert "The closest it has is GET /drive/v3/files/{file_id}/" in error.reason
    assert isinstance(error.error_details, list) and error.error_details[0]["reason"] == "notImplemented"


async def test_minutehands_own_error_is_raised_by_the_client_in_googles_shape(drive: Drive, google: Google) -> None:
    files = google.drive(signed_in()).files()
    drive.store.close()
    error = await raised(lambda: files.list().execute())

    assert error.resp.status == 500
    assert error.resp[ANSWER_HEADER] == "internal_error"
    assert error.reason.startswith(f"{INTERNAL_PREFIX} google_drive GET /drive/v3/files: ProgrammingError")


async def test_a_path_another_google_host_serves_is_refused_404_as_google_refuses_it(drive: Drive) -> None:
    async with client_for(drive.provider, drive.store, drive.clock, DOCS_HOST) as docs_host:
        response = await docs_host.get("/drive/v3/files", headers=AUTH)
    assert response.status_code == 404 and ANSWER_HEADER not in response.headers


async def test_an_unknown_file_is_refused_404_unmarked(google: Google) -> None:
    files = google.drive(signed_in()).files()
    error = await raised(lambda: files.get(fileId=MISSING).execute())

    assert (error.resp.status, error.reason) == (404, f"File not found: {MISSING}.")
    assert ANSWER_HEADER not in error.resp


async def test_a_declared_rate_limit_is_refused_as_an_injected_fault(drive: Drive, api: httpx.AsyncClient) -> None:
    drive.provider.declare(
        '{"faults": [{"operation": "files.list", "kind": "rate_limited"}]}', drive.store, drive.clock
    )
    outcome = Outcome()
    token = OUTCOME.set(outcome)
    try:
        response = await api.get("/drive/v3/files", headers=AUTH)
    finally:
        OUTCOME.reset(token)

    assert answer(response, 403)["error"] == {
        "code": 403,
        "message": "Rate Limit Exceeded",
        "errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded", "message": "Rate Limit Exceeded"}],
    }
    assert outcome.kind is AnswerKind.INJECTED_FAULT
    assert outcome.failure is not None and outcome.failure.code == "rateLimitExceeded"


async def test_a_declared_fault_on_sign_in_is_refused_in_oauths_shape_as_an_injected_fault(drive: Drive) -> None:
    drive.provider.declare('{"faults": [{"operation": "token", "kind": "unavailable"}]}', drive.store, drive.clock)
    outcome = Outcome()
    token = OUTCOME.set(outcome)
    try:
        async with client_for(drive.provider, drive.store, drive.clock, OAUTH_HOST) as oauth_host:
            response = await oauth_host.post("/token", data={"grant_type": "refresh_token", "refresh_token": "r"})
    finally:
        OUTCOME.reset(token)

    assert answer(response, 503) == {
        "error": "temporarily_unavailable",
        "error_description": "The service is currently unavailable.",
    }
    assert outcome.kind is AnswerKind.INJECTED_FAULT


def test_the_guarded_app_still_says_what_it_is_pushing_in_the_background(drive: Drive) -> None:
    app = drive.provider.app(drive.store, drive.clock)
    assert isinstance(app, DeliversInBackground) and app.delivering() == 0
