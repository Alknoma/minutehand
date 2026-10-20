"""Google's identity infrastructure beside the Workspace APIs, through `minutehand serve`'s proxy: IAM Credentials'
signBlob and signJwt, the metadata server's runtime service account over plain HTTP, and the ID token Google's token
endpoint adds for an identity scope, verified by google-auth against the certificates the world publishes."""

from __future__ import annotations

import base64
import json
import ssl
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.adapters.providers.google_workspace.identity import key_id, service_account_key
from minutehand.domain.scenario import Seed
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld

RUNTIME = "runner@identity-project.iam.gserviceaccount.com"
REFRESH = "1//identity-refresh"


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[MinutehandClient, dict[str, str]]]:
    with serve_in_background(tmp_path_factory.mktemp("serve")) as url, MinutehandClient(url) as client:
        yield client, client.environment()


def _world(client: MinutehandClient, *, runtime: str | None) -> OpenWorld:
    seed = Seed.model_validate(
        {
            "name": "identity",
            "people": [{"key": "ida", "name": "Ida Identity", "email": "ida@example.com"}],
            "provider_seeds": [
                {"provider": "google_workspace", "body": {"runtime_service_account": runtime} if runtime else {}}
            ],
        }
    )
    claims = Claims(tokens=[REFRESH], hosts=["metadata.google.internal"])
    return OpenWorld(client, client.create_world(CreateWorld(seed=seed, claims=claims)))


@pytest.fixture
def world(served: tuple[MinutehandClient, dict[str, str]]) -> Iterator[OpenWorld]:
    client, _ = served
    opened = _world(client, runtime=RUNTIME)
    try:
        yield opened
    finally:
        client.close_world(opened.world_id)


def _http(environment: dict[str, str]) -> httpx.Client:
    trust = ssl.create_default_context(cafile=environment["SSL_CERT_FILE"])
    return httpx.Client(
        proxy=environment["HTTPS_PROXY"],
        verify=trust,
        trust_env=False,
        timeout=30,
        headers={"Authorization": f"Bearer {REFRESH}"},
    )


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_sign_blob_signs_with_the_service_accounts_key_and_names_it(
    served: tuple[MinutehandClient, dict[str, str]], world: OpenWorld
) -> None:
    _, environment = served
    url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{RUNTIME}:signBlob"
    with _http(environment) as http:
        answered = http.post(url, json={"payload": base64.b64encode(b"bytes to sign").decode()})
    assert answered.status_code == 200, answered.text
    found = answered.json()
    key = service_account_key(RUNTIME)
    assert found["keyId"] == key_id(key) and len(found["keyId"]) == 40
    key.private_key.public_key().verify(
        base64.b64decode(found["signedBlob"]), b"bytes to sign", padding.PKCS1v15(), hashes.SHA256()
    )


def test_sign_jwt_signs_the_claims_as_given(served: tuple[MinutehandClient, dict[str, str]], world: OpenWorld) -> None:
    _, environment = served
    claims = json.dumps({"iss": RUNTIME, "sub": RUNTIME, "aud": "https://example.com", "iat": 1, "exp": 2})
    url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{RUNTIME}:signJwt"
    with _http(environment) as http:
        answered = http.post(url, json={"payload": claims})
    assert answered.status_code == 200, answered.text
    head, body, signature = answered.json()["signedJwt"].split(".")
    assert json.loads(_unb64(head)) == {"alg": "RS256", "kid": answered.json()["keyId"], "typ": "JWT"}
    assert _unb64(body).decode() == claims
    service_account_key(RUNTIME).private_key.public_key().verify(
        _unb64(signature), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_a_sign_call_naming_a_project_instead_of_the_wildcard_is_refused(
    served: tuple[MinutehandClient, dict[str, str]], world: OpenWorld
) -> None:
    _, environment = served
    with _http(environment) as http:
        named = http.post(
            f"https://iamcredentials.googleapis.com/v1/projects/my-project/serviceAccounts/{RUNTIME}:signBlob",
            json={"payload": "YQ=="},
        )
        delegated = http.post(
            f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{RUNTIME}:signBlob",
            json={"payload": "YQ==", "delegates": ["projects/-/serviceAccounts/other@x.iam.gserviceaccount.com"]},
        )
    assert named.status_code == 400 and named.json()["error"]["status"] == "INVALID_ARGUMENT"
    assert delegated.status_code == 501 and "delegates" in delegated.text


def test_the_metadata_server_answers_the_runtime_service_account_over_plain_http(
    served: tuple[MinutehandClient, dict[str, str]], world: OpenWorld
) -> None:
    _, environment = served
    url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"
    with httpx.Client(proxy=environment["HTTP_PROXY"], trust_env=False, timeout=30) as http:
        answered = http.get(url, headers={"Metadata-Flavor": "Google"})
        bare = http.get(url)
        other = http.get(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
    assert (answered.status_code, answered.text, answered.headers["metadata-flavor"]) == (200, RUNTIME, "Google")
    assert bare.status_code == 501 and "Metadata-Flavor" in bare.text
    assert other.status_code == 501 and "/token" in other.text
    assert [c.exchange.host for c in world.calls()].count("metadata.google.internal") == 3


def test_a_world_that_seeds_no_runtime_service_account_refuses_the_metadata_server_by_name(
    served: tuple[MinutehandClient, dict[str, str]],
) -> None:
    client, environment = served
    opened = _world(client, runtime=None)
    try:
        with httpx.Client(proxy=environment["HTTP_PROXY"], trust_env=False, timeout=30) as http:
            answered = http.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
                headers={"Metadata-Flavor": "Google"},
            )
        assert answered.status_code == 501 and "runtime_service_account" in answered.text
    finally:
        client.close_world(opened.world_id)


def test_an_identity_scope_adds_an_id_token_google_auth_verifies_against_the_published_certificates(
    served: tuple[MinutehandClient, dict[str, str]], world: OpenWorld, tmp_path: Path
) -> None:
    import google.auth.transport.requests
    import google.oauth2.id_token
    import requests

    _, environment = served
    form = {"grant_type": "refresh_token", "refresh_token": REFRESH, "client_id": "web.apps.googleusercontent.com"}
    with _http(environment) as http:
        plain = http.post("https://oauth2.googleapis.com/token", data=form).json()
        identity = http.post("https://oauth2.googleapis.com/token", data={**form, "scope": "openid email"}).json()
    assert "id_token" not in plain
    session = requests.Session()
    session.proxies = {"https": environment["HTTPS_PROXY"]}
    session.verify = environment["SSL_CERT_FILE"]
    claims = google.oauth2.id_token.verify_oauth2_token(
        identity["id_token"], google.auth.transport.requests.Request(session), audience="web.apps.googleusercontent.com"
    )
    assert (claims["iss"], claims["email"], claims["email_verified"], claims["name"]) == (
        "https://accounts.google.com", "ida@example.com", True, "Ida Identity",
    )  # fmt: skip
    with _http(environment) as http:
        userinfo = http.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {identity['access_token']}"},
        ).json()
    assert claims["sub"] == userinfo["id"], "the ID token names the account as userinfo does"
