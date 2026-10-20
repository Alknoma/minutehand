"""Firebase Authentication, reached as services reach it: stock `firebase_admin` on Application Default Credentials
of type `authorized_user` in a process of its own, and the REST API's end-user calls with a web API key, each world
one Firebase project of one `minutehand serve`."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from minutehand.adapters.providers.firebase_auth import wire
from minutehand.adapters.providers.firebase_auth.provider import KEY, key_id
from minutehand.domain.world import Actor, EntityKind, Operation
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from tests.providers.firebase_auth.support import RUNTIME, Project, open_project

pytestmark = pytest.mark.timeout(180)

SAM = {"uid": "sam-uid", "email": "sam@example.com", "password": "hunter22", "display_name": "Sam Seeded"}


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[MinutehandClient, dict[str, str]]]:
    with serve_in_background(tmp_path_factory.mktemp("serve")) as url, MinutehandClient(url) as client:
        yield client, client.environment()


@contextmanager
def _project(
    served: tuple[MinutehandClient, dict[str, str]], tmp_path: Path, tag: str, **more: Any
) -> Iterator[Project]:
    client, environment = served
    opened = open_project(client, environment, tmp_path / tag, tag, **more)
    try:
        yield opened
    finally:
        client.close_world(opened.world.world_id)


@pytest.fixture
def project(
    served: tuple[MinutehandClient, dict[str, str]], tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Project]:
    tag = "".join(c for c in request.node.name.lower() if c.isalnum())[-20:]
    with _project(served, tmp_path, tag, users=[SAM], metadata=True) as opened:
        yield opened


def _claims(token: str) -> dict[str, object]:
    payload = token.split(".")[1]
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert isinstance(decoded, dict)
    return decoded


def _signed_in(project: Project, email: str = "sam@example.com", password: str = "hunter22") -> dict[str, str]:
    answered = project.rest("signInWithPassword", {"email": email, "password": password, "returnSecureToken": True})
    assert answered.status_code == 200, answered.text
    found = answered.json()
    assert isinstance(found, dict)
    return found


# ------------------------------------------------------------------ the Admin SDK's account management


def test_firebase_admin_creates_reads_updates_and_deletes_accounts_and_raises_its_typed_errors(
    project: Project,
) -> None:
    said = project.firebase_admin(
        """
made = auth.create_user(uid="chosen-uid", email="chosen@example.com", email_verified=True, display_name="Chosen",
                        password="secret99")
say(made=[made.uid, made.email, made.email_verified, made.display_name, [p.provider_id for p in made.provider_data]])
say(by_email=auth.get_user_by_email("SAM@example.com").uid)
say(seeded=[auth.get_user("sam-uid").display_name, auth.get_user("sam-uid").user_metadata.creation_timestamp > 0])
auth.update_user("chosen-uid", email_verified=False, display_name="Renamed", photo_url="https://example.com/p.png",
                 disabled=True, password="another1")
changed = auth.get_user("chosen-uid")
say(changed=[changed.email_verified, changed.display_name, changed.photo_url, changed.disabled])
auth.update_user("chosen-uid", display_name=auth.DELETE_ATTRIBUTE)
say(cleared=auth.get_user("chosen-uid").display_name)
auth.set_custom_user_claims("chosen-uid", {"role": "editor"})
say(claims=auth.get_user("chosen-uid").custom_claims)
found = auth.get_users([auth.UidIdentifier("chosen-uid"), auth.EmailIdentifier("nobody@example.com")])
say(get_users=[[u.uid for u in found.users], [type(n).__name__ for n in found.not_found]])
say(listed=sorted(u.uid for u in auth.list_users(max_results=1).iterate_all()))
say(no_user=refused(lambda: auth.get_user("nobody")))
say(no_email=refused(lambda: auth.get_user_by_email("nobody@example.com")))
say(email_taken=refused(lambda: auth.create_user(email="sam@example.com")))
say(uid_taken=refused(lambda: auth.create_user(uid="sam-uid")))
say(update_missing=refused(lambda: auth.update_user("nobody", display_name="x")))
auth.delete_user("chosen-uid")
say(deleted=refused(lambda: auth.get_user("chosen-uid")))
say(delete_missing=refused(lambda: auth.delete_user("chosen-uid")))
"""
    )
    answers = {k: v for line in said for k, v in line.items()}
    assert answers["made"] == ["chosen-uid", "chosen@example.com", True, "Chosen", ["password"]]
    assert answers["by_email"] == "sam-uid", "an address is matched whatever its letter case"
    assert answers["seeded"] == ["Sam Seeded", True]
    assert answers["changed"] == [False, "Renamed", "https://example.com/p.png", True]
    assert answers["cleared"] is None
    assert answers["claims"] == {"role": "editor"}
    assert answers["get_users"] == [["chosen-uid"], ["EmailIdentifier"]]
    assert answers["listed"] == ["chosen-uid", "sam-uid"], "a listing paged one at a time holds each account once"
    assert answers["no_user"] == "UserNotFoundError"
    assert answers["no_email"] == "UserNotFoundError"
    assert answers["email_taken"] == "EmailAlreadyExistsError"
    assert answers["uid_taken"] == "UidAlreadyExistsError"
    assert answers["update_missing"] == "UserNotFoundError"
    assert answers["deleted"] == "UserNotFoundError"
    assert answers["delete_missing"] == "UserNotFoundError"

    users = [
        e for e in project.world.entities(provider="firebase_auth", kind=EntityKind.RECORD)
        if e.parent == f"projects/{project.project_id}/accounts"
    ]  # fmt: skip
    assert [json.loads(e.body)["localId"] for e in users] == ["sam-uid"], "the deleted account is gone from the record"
    created = project.world.events(provider="firebase_auth", operation=Operation.CREATE, actor=Actor.AGENT)
    assert any(e.entity.external_id.endswith("/chosen-uid") for e in created)


def test_admin_calls_with_no_credential_are_routed_by_the_project_in_their_path(project: Project) -> None:
    made = project.admin("accounts", {"localId": "plain-rest", "email": "plain@example.com", "emailVerified": True})
    assert made.status_code == 200, made.text
    assert made.json() == {
        "kind": "identitytoolkit#SignupNewUserResponse", "localId": "plain-rest", "email": "plain@example.com",
    }  # fmt: skip
    assert project.admin("accounts:update", {"localId": "plain-rest", "emailVerified": False}).status_code == 200
    by_id = project.admin("accounts:lookup", {"localId": ["plain-rest", "sam-uid"]}).json()["users"]
    assert [(u["localId"], u["emailVerified"]) for u in by_id] == [("plain-rest", False), ("sam-uid", False)]
    by_email = project.admin("accounts:lookup", {"email": ["plain@example.com"]}).json()["users"]
    assert [u["localId"] for u in by_email] == ["plain-rest"]
    nobody = project.admin("accounts:lookup", {"localId": ["nobody"]})
    assert nobody.json() == {"kind": "identitytoolkit#GetAccountInfoResponse"}, "an id it holds no account for"
    deleted = project.admin("accounts:batchDelete", {"localIds": ["plain-rest", "nobody"], "force": True})
    assert deleted.status_code == 200 and deleted.json() == {}
    assert "users" not in project.admin("accounts:lookup", {"localId": ["plain-rest"]}).json()
    assert project.world.unmatched_calls() == []


def test_a_batch_delete_without_force_leaves_accounts_that_are_not_disabled_and_reports_them(project: Project) -> None:
    project.admin("accounts", {"localId": "off", "email": "off@example.com", "disabled": True})
    answered = project.admin("accounts:batchDelete", {"localIds": ["sam-uid", "off"]}).json()
    assert [(e["index"], e["localId"]) for e in answered["errors"]] == [(0, "sam-uid")]
    left = project.admin("accounts:lookup", {"localId": ["sam-uid", "off"]}).json()["users"]
    assert [u["localId"] for u in left] == ["sam-uid"]


# ------------------------------------------------------------------ an end user's sign-up and sign-in


def test_sign_up_and_sign_in_with_a_password_answer_tokens_the_admin_sdk_verifies(project: Project) -> None:
    signed_up = project.rest("signUp", {"email": "new@example.com", "password": "secret1", "returnSecureToken": True})
    assert signed_up.status_code == 200, signed_up.text
    made = signed_up.json()
    assert made["kind"] == "identitytoolkit#SignupNewUserResponse" and made["expiresIn"] == "3600"
    assert made["email"] == "new@example.com" and len(made["localId"]) == 28
    signed_in = _signed_in(project)
    assert signed_in["kind"] == "identitytoolkit#VerifyPasswordResponse" and signed_in["registered"] is True
    assert signed_in["localId"] == "sam-uid" and signed_in["displayName"] == "Sam Seeded"

    header = json.loads(base64.urlsafe_b64decode(signed_in["idToken"].split(".")[0] + "=="))
    assert header == {"alg": "RS256", "kid": key_id(), "typ": "JWT"}
    claims = _claims(signed_in["idToken"])
    assert claims["iss"] == f"https://securetoken.google.com/{project.project_id}"
    assert claims["aud"] == project.project_id
    assert claims["sub"] == claims["user_id"] == "sam-uid"
    assert claims["email"] == "sam@example.com" and claims["email_verified"] is False
    assert claims["firebase"] == {"identities": {"email": ["sam@example.com"]}, "sign_in_provider": "password"}
    assert isinstance(claims["exp"], int) and isinstance(claims["iat"], int) and claims["exp"] - claims["iat"] == 3600

    said = project.firebase_admin(
        f"""
for token in ({signed_in["idToken"]!r}, {made["idToken"]!r}):
    claims = auth.verify_id_token(token, check_revoked=True)
    say(uid=claims["uid"], provider=claims["firebase"]["sign_in_provider"], auth_time=claims["auth_time"] > 0)
"""
    )
    assert said == [
        {"uid": "sam-uid", "provider": "password", "auth_time": True},
        {"uid": made["localId"], "provider": "password", "auth_time": True},
    ]
    looked = project.rest("lookup", {"idToken": signed_in["idToken"]}).json()["users"]
    assert [u["localId"] for u in looked] == ["sam-uid"]
    assert looked[0]["lastLoginAt"].isdigit() and looked[0]["lastRefreshAt"].endswith("Z")


def test_sign_up_and_sign_in_refusals_are_firebases_codes(project: Project) -> None:
    def code(method: str, body: dict[str, object]) -> tuple[int, str]:
        answered = project.rest(method, {**body, "returnSecureToken": True})
        found = answered.json()
        assert found["error"]["errors"] == [
            {"message": found["error"]["message"], "domain": "global", "reason": "invalid"}
        ]
        return answered.status_code, found["error"]["message"]

    assert code("signUp", {"email": "sam@example.com", "password": "secret1"}) == (400, "EMAIL_EXISTS")
    assert code("signUp", {"email": "short@example.com", "password": "12345"}) == (
        400, "WEAK_PASSWORD : Password should be at least 6 characters",
    )  # fmt: skip
    assert code("signUp", {"email": "not-an-address", "password": "secret1"}) == (400, "INVALID_EMAIL")
    assert code("signUp", {"email": "nopass@example.com"}) == (400, "MISSING_PASSWORD")
    assert code("signInWithPassword", {"email": "sam@example.com", "password": "wrong-one"}) == (
        400, "INVALID_LOGIN_CREDENTIALS",
    )  # fmt: skip
    assert code("signInWithPassword", {"email": "nobody@example.com", "password": "wrong-one"}) == (
        400, "INVALID_LOGIN_CREDENTIALS",
    ), "with email enumeration protection, an unknown address reads as a wrong password"  # fmt: skip
    project.admin("accounts:update", {"localId": "sam-uid", "disableUser": True})
    assert code("signInWithPassword", {"email": "sam@example.com", "password": "hunter22"}) == (400, "USER_DISABLED")
    assert code("lookup", {"idToken": "not-a-token"}) == (400, "INVALID_ID_TOKEN")
    assert code("update", {"localId": "nobody", "displayName": "x"}) == (400, "USER_NOT_FOUND")


def test_without_email_enumeration_protection_sign_in_says_which_part_was_wrong_and_is_refused(
    served: tuple[MinutehandClient, dict[str, str]], tmp_path: Path
) -> None:
    with _project(served, tmp_path, "noprotect", users=[SAM], enumeration_protection=False) as project:
        wrong = project.rest("signInWithPassword", {"email": "sam@example.com", "password": "nope-nope"})
        unknown = project.rest("signInWithPassword", {"email": "nobody@example.com", "password": "nope-nope"})
        assert (wrong.json()["error"]["message"], unknown.json()["error"]["message"]) == (
            "INVALID_PASSWORD",
            "EMAIL_NOT_FOUND",
        )
        said = project.firebase_admin(
            """
say(found=refused(lambda: auth.get_user_by_email("nobody@example.com")))
"""
        )
        assert said == [{"found": "UserNotFoundError"}]


def test_an_anonymous_sign_up_signs_in_with_no_address(project: Project) -> None:
    made = project.rest("signUp", {"returnSecureToken": True}).json()
    claims = _claims(made["idToken"])
    assert claims["firebase"] == {"identities": {}, "sign_in_provider": "anonymous"} and "email" not in claims


# ------------------------------------------------------------------ the refresh grant


def test_a_refresh_token_is_exchanged_for_an_id_token_until_its_account_is_revoked_or_disabled(
    project: Project,
) -> None:
    signed_in = _signed_in(project)
    with project.http() as http:

        def refresh(token: str, grant: str = "refresh_token") -> tuple[int, dict[str, Any]]:
            answered = http.post(
                f"https://securetoken.googleapis.com/v1/token?key={project.api_key}",
                data={"grant_type": grant, "refresh_token": token},
            )
            found = answered.json()
            assert isinstance(found, dict)
            return answered.status_code, found

        status, answered = refresh(signed_in["refreshToken"])
        assert status == 200, answered
        assert sorted(answered) == [
            "access_token", "expires_in", "id_token", "project_id", "refresh_token", "token_type", "user_id",
        ]  # fmt: skip
        assert (answered["user_id"], answered["project_id"], answered["expires_in"], answered["token_type"]) == (
            "sam-uid", project.project_id, "3600", "Bearer",
        )  # fmt: skip
        assert _claims(answered["id_token"])["auth_time"] == _claims(signed_in["idToken"])["auth_time"]

        assert refresh("not-a-refresh-token")[1]["error"]["message"] == "INVALID_REFRESH_TOKEN"
        assert refresh(signed_in["refreshToken"], grant="password")[1]["error"]["message"] == "INVALID_GRANT_TYPE"
        assert refresh("")[1]["error"]["message"] == "MISSING_REFRESH_TOKEN"

        project.firebase_admin("""auth.revoke_refresh_tokens("sam-uid")""")
        assert refresh(signed_in["refreshToken"])[1]["error"]["message"] == "TOKEN_EXPIRED"

        again = _signed_in(project)
        project.admin("accounts:update", {"localId": "sam-uid", "disableUser": True})
        assert refresh(again["refreshToken"])[1]["error"]["message"] == "USER_DISABLED"


# ------------------------------------------------------------------ verify_id_token


def test_verify_id_token_refuses_a_revoked_token_a_disabled_account_and_another_projects_token(
    served: tuple[MinutehandClient, dict[str, str]], project: Project, tmp_path: Path
) -> None:
    token = _signed_in(project)["idToken"]
    with _project(served, tmp_path, "otherproject", users=[SAM]) as other:
        foreign = _signed_in(other)["idToken"]
        said = project.firebase_admin(
            f"""
say(own=auth.verify_id_token({token!r})["uid"])
say(foreign=refused(lambda: auth.verify_id_token({foreign!r})))
say(custom=refused(lambda: auth.verify_id_token(auth.create_custom_token("x").decode())))
auth.update_user("sam-uid", disabled=True)
say(disabled=refused(lambda: auth.verify_id_token({token!r}, check_revoked=True)))
auth.update_user("sam-uid", disabled=False)
auth.revoke_refresh_tokens("sam-uid")
say(revoked=refused(lambda: auth.verify_id_token({token!r}, check_revoked=True)))
say(unchecked=auth.verify_id_token({token!r})["uid"])
"""
        )
    assert said == [
        {"own": "sam-uid"},
        {"foreign": "InvalidIdTokenError"},
        {"custom": "InvalidIdTokenError"},
        {"disabled": "UserDisabledError"},
        {"revoked": "RevokedIdTokenError"},
        {"unchecked": "sam-uid"},
    ]


def test_verify_id_token_refuses_a_token_the_worlds_clock_issued_more_than_an_hour_ago(
    served: tuple[MinutehandClient, dict[str, str]], tmp_path: Path
) -> None:
    past = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _project(served, tmp_path, "expired", users=[SAM], starts_at=past) as project:
        token = _signed_in(project)["idToken"]
        said = project.firebase_admin(f"""say(expired=refused(lambda: auth.verify_id_token({token!r})))""")
        assert said == [{"expired": "ExpiredIdTokenError"}]
        assert project.rest("lookup", {"idToken": token}).status_code == 200, "by the world's own clock it is not"
        project.world.advance(by=timedelta(hours=1, seconds=1))
        assert project.rest("lookup", {"idToken": token}).json()["error"]["message"] == "INVALID_ID_TOKEN"


def test_the_certificates_are_answered_with_no_world_and_verify_the_tokens_signature(
    served: tuple[MinutehandClient, dict[str, str]], project: Project
) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    client, _ = served
    with project.http() as http:
        answered = http.get(f"https://www.googleapis.com{wire.CERTIFICATES_PATH}")
    assert answered.status_code == 200
    assert "max-age=" in answered.headers["cache-control"]
    certificates = answered.json()
    assert list(certificates) == [key_id()]
    public = x509.load_pem_x509_certificate(certificates[key_id()].encode()).public_key()
    assert isinstance(public, rsa.RSAPublicKey)
    token = _signed_in(project)["idToken"]
    head, body, signature = token.split(".")
    public.verify(
        base64.urlsafe_b64decode(signature + "=="), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    assert public.public_numbers() == KEY.private_key.public_key().public_numbers()
    assert all(c.exchange.path != wire.CERTIFICATES_PATH for c in project.world.calls()), "no world holds it"
    assert [c for c in client.unmatched().calls if c.exchange.path == wire.CERTIFICATES_PATH] == []


# ------------------------------------------------------------------ custom tokens


def test_create_custom_token_on_user_credentials_signs_through_iam_as_the_runtime_service_account(
    project: Project,
) -> None:
    said = project.firebase_admin(
        """
say(token=auth.create_custom_token("custom-uid", {"role": "admin"}).decode())
"""
    )
    token = said[0]["token"]
    assert isinstance(token, str)
    claims = _claims(token)
    runtime = RUNTIME.format(project=project.project_id)
    assert claims["iss"] == claims["sub"] == runtime and claims["uid"] == "custom-uid"
    assert claims["aud"] == wire.FIREBASE_AUDIENCE and claims["claims"] == {"role": "admin"}
    asked = [(c.exchange.method, c.exchange.host, c.exchange.path) for c in project.world.calls()]
    assert ("GET", "metadata.google.internal", "/computeMetadata/v1/instance/service-accounts/default/email") in asked
    assert ("POST", "iamcredentials.googleapis.com", f"/v1/projects/-/serviceAccounts/{runtime}:signBlob") in asked

    signed = project.rest("signInWithCustomToken", {"token": token, "returnSecureToken": True})
    assert signed.status_code == 200, signed.text
    answered = signed.json()
    assert answered["kind"] == "identitytoolkit#VerifyCustomTokenResponse" and answered["isNewUser"] is True
    id_claims = _claims(answered["idToken"])
    assert id_claims["sub"] == "custom-uid" and id_claims["role"] == "admin"
    assert id_claims["firebase"] == {"identities": {}, "sign_in_provider": "custom"}
    again = project.rest("signInWithCustomToken", {"token": token, "returnSecureToken": True}).json()
    assert again["isNewUser"] is False
    verified = project.firebase_admin(f"""say(uid=auth.verify_id_token({answered["idToken"]!r})["uid"])""")
    assert verified == [{"uid": "custom-uid"}]


def test_a_custom_token_for_another_audience_or_expired_is_refused(project: Project) -> None:
    def token(**claims: object) -> str:
        def part(value: dict[str, object]) -> str:
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

        return f"{part({'alg': 'RS256'})}.{part(claims)}.c2ln"

    now = int(datetime.now(UTC).timestamp())
    good = {"uid": "u1", "aud": wire.FIREBASE_AUDIENCE, "iat": now, "exp": now + 600}
    assert project.rest("signInWithCustomToken", {"token": token(**good)}).status_code == 200
    for bad in ({**good, "aud": "someone-else"}, {**good, "exp": now - 7200}, {**good, "uid": ""}):
        answered = project.rest("signInWithCustomToken", {"token": token(**bad)})
        assert answered.json()["error"]["message"] == "INVALID_CUSTOM_TOKEN"
    assert project.rest("signInWithCustomToken", {"token": "no"}).json()["error"]["message"] == "INVALID_CUSTOM_TOKEN"


# ------------------------------------------------------------------ email links


def test_a_sign_in_link_the_admin_sdk_generates_signs_in_once(project: Project) -> None:
    said = project.firebase_admin(
        """
settings = auth.ActionCodeSettings(url="https://app.example.com/finish", handle_code_in_app=True)
say(link=auth.generate_sign_in_with_email_link("linked@example.com", settings))
say(reset=refused(lambda: auth.generate_password_reset_link("nobody@example.com")))
say(verify=auth.generate_email_verification_link("sam@example.com"))
"""
    )
    link = said[0]["link"]
    assert isinstance(link, str)
    assert link.startswith(
        f"https://{project.project_id}.firebaseapp.com/__/auth/action?apiKey={project.api_key}&mode=signIn&oobCode="
    )
    assert "continueUrl=https%3A%2F%2Fapp.example.com%2Ffinish" in link
    assert said[1] == {"reset": "EmailNotFoundError"}
    assert "mode=verifyEmail" in str(said[2]["verify"])
    code = link.split("oobCode=")[1].split("&")[0]
    first = project.rest("signInWithEmailLink", {"email": "linked@example.com", "oobCode": code})
    assert first.status_code == 200 and first.json()["isNewUser"] is True
    assert _claims(first.json()["idToken"])["email_verified"] is True
    again = project.rest("signInWithEmailLink", {"email": "linked@example.com", "oobCode": code})
    assert again.json()["error"]["message"] == "INVALID_OOB_CODE", "a code signs in once"


# ------------------------------------------------------------------ Google sign-in


def test_a_google_id_token_from_the_worlds_own_sign_in_signs_in_the_account_linked_to_that_person(
    served: tuple[MinutehandClient, dict[str, str]], tmp_path: Path
) -> None:
    people = [
        {"key": "gia", "name": "Gia Linked", "email": "gia@example.com"},
        {"key": "noor", "name": "Noor New", "email": "noor@example.com"},
    ]
    users = [{"uid": "gia-uid", "person": "gia", "google": "gia"}]
    with _project(served, tmp_path, "googlesignin", people=people, users=users) as project:

        def google_id_token(refresh: str) -> str:
            with project.http() as http:
                answered = http.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh,
                        "client_id": "web.apps.googleusercontent.com",
                        "client_secret": "s",
                        "scope": "openid email profile",
                    },
                )
            assert answered.status_code == 200, answered.text
            found = answered.json()["id_token"]
            assert isinstance(found, str)
            return found

        gia = google_id_token(project.refresh)
        signed = project.rest(
            "signInWithIdp",
            {
                "postBody": f"id_token={gia}&providerId=google.com",
                "requestUri": "http://localhost",
                "returnSecureToken": True,
            },
        )
        assert signed.status_code == 200, signed.text
        answered = signed.json()
        assert answered["localId"] == "gia-uid" and answered["providerId"] == "google.com"
        assert "isNewUser" not in answered
        assert _claims(answered["idToken"])["firebase"]["sign_in_provider"] == "google.com"  # type: ignore[index]
        access = project.rest(
            "signInWithIdp",
            {"postBody": "access_token=ya29.x&providerId=google.com", "requestUri": "http://localhost"},
        )
        assert access.status_code == 501 and "id_token" in access.text


# ------------------------------------------------------------------ two projects at once


def test_two_projects_at_once_each_answer_only_their_own_accounts(
    served: tuple[MinutehandClient, dict[str, str]], tmp_path: Path
) -> None:
    with _project(served, tmp_path, "parallelone", users=[SAM]) as one:
        with _project(served, tmp_path, "paralleltwo", users=[{**SAM, "uid": "sam-two"}]) as two:
            assert _signed_in(one)["localId"] == "sam-uid"
            assert _signed_in(two)["localId"] == "sam-two"
            listed = {
                p.project_id: [u["localId"] for u in p.admin("accounts:lookup", {"email": ["sam@example.com"]}).json()["users"]]
                for p in (one, two)
            }  # fmt: skip
            assert listed == {one.project_id: ["sam-uid"], two.project_id: ["sam-two"]}
            assert one.world.unmatched_calls() == [] and two.world.unmatched_calls() == []


# ------------------------------------------------------------------ the surface

DISCOVERY = Path(__file__).parents[2] / "data" / "firebase_auth" / "identitytoolkit_v1_discovery.json"


def _methods() -> list[tuple[str, str, str]]:
    """Every method of Identity Toolkit v1's discovery document: its id, HTTP method and path."""
    found: list[tuple[str, str, str]] = []

    def walk(resources: dict[str, dict[str, object]]) -> None:
        for resource in resources.values():
            methods = resource["methods"] if "methods" in resource else {}
            assert isinstance(methods, dict)
            for method in methods.values():
                found.append((method["id"], method["httpMethod"], "/" + method["flatPath"]))
            inner = resource["resources"] if "resources" in resource else {}
            assert isinstance(inner, dict)
            walk(inner)

    document = json.loads(DISCOVERY.read_text())
    walk(document["resources"])
    return found


def name_of(method_id: str) -> str:
    return method_id.removeprefix("identitytoolkit.")


def test_every_method_of_the_discovery_document_is_served_or_refused_by_name(project: Project) -> None:
    from minutehand.adapters.providers.firebase_auth.provider import SERVED

    methods = _methods()
    assert len(methods) == 41
    with project.http() as http:
        for method_id, verb, path in methods:
            concrete = path.replace("{projectsId}", project.project_id).replace("{tenantsId}", "tenant-1")
            body = {"returnOobLink": True} if name_of(method_id).endswith("sendOobCode") else {}
            answered = http.request(
                verb,
                f"https://identitytoolkit.googleapis.com{concrete}?key={project.api_key}",
                json=body if verb == "POST" else None,
            )
            name = method_id.removeprefix("identitytoolkit.")
            if name in SERVED:
                assert answered.status_code != 501, (method_id, answered.text)
            else:
                assert answered.status_code == 501, (method_id, answered.text)
                message = answered.json()["error"]["message"]
                assert name in message or "tenants" in message, (method_id, message)
    assert len(SERVED) == 16 and {m.removeprefix("identitytoolkit.") for m, _, _ in methods} >= SERVED


def test_a_field_the_method_has_and_the_fake_does_not_serve_is_refused_by_name(project: Project) -> None:
    answered = project.rest("signUp", {"email": "x@example.com", "password": "secret1", "phoneNumber": "+15555550100"})
    assert answered.status_code == 501 and "phoneNumber" in answered.json()["error"]["message"]
    tenant = project.rest("signInWithPassword", {"email": "sam@example.com", "password": "hunter22", "tenantId": "t"})
    assert tenant.status_code == 501 and "tenantId" in tenant.json()["error"]["message"]


def test_a_call_naming_another_project_is_refused_by_name(project: Project) -> None:
    with project.http() as http:
        answered = http.post(
            "https://identitytoolkit.googleapis.com/v1/projects/proj-other/accounts:lookup",
            json={"localId": ["sam-uid"]},
            headers={"Authorization": f"Bearer {project.refresh}"},
        )
    assert answered.status_code == 501 and project.project_id in answered.json()["error"]["message"]
