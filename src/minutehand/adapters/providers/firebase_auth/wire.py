"""Firebase Authentication as Identity Toolkit v1 and the Secure Token API put it on the wire: JSON in camelCase
(the token endpoint's answer in snake_case), times as milliseconds or seconds since the epoch written as strings,
and the Auth error envelope whose `message` is an error code, optionally followed by ` : ` and a sentence.

The one place this provider's JSON is parsed or built. A request body is read field by field against the method's
documented request (Identity Toolkit's discovery document, revision 20260311, committed as
`tests/data/firebase_auth/identitytoolkit_v1_discovery.json`): a field the method has and this fake does not serve
is refused 501 by name, never ignored.

https://firebase.google.com/docs/reference/rest/auth (the REST API, `REST` below);
https://cloud.google.com/identity-platform/docs/reference/rest/v1/projects.accounts (the admin methods).
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qs

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from minutehand.domain.errors import Asked, NotServed, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model

JSON = "application/json; charset=UTF-8"
REST = "https://firebase.google.com/docs/reference/rest/auth"

FIREBASE_AUDIENCE = "https://identitytoolkit.googleapis.com/google.identity.identitytoolkit.v1.IdentityToolkit"
"""The `aud` of every custom token (https://firebase.google.com/docs/auth/admin/create-custom-tokens#create_custom_tokens_using_a_third-party_jwt_library)."""
ISSUER_PREFIX = "https://securetoken.google.com/"
"""An ID token's `iss` is this and the project id (https://firebase.google.com/docs/auth/admin/verify-id-tokens#verify_id_tokens_using_a_third-party_jwt_library)."""
SECURETOKEN_ACCOUNT = "securetoken@system.gserviceaccount.com"
CERTIFICATES_PATH = f"/robot/v1/metadata/x509/{SECURETOKEN_ACCOUNT}"
"""Where an ID token's signing certificates are published (verify-id-tokens, "Finally, ensure that the ID token
was signed by the private key corresponding to the token's kid claim. Grab the public key from
https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com")."""
CERTIFICATES_CACHE = "public, max-age=19800, must-revalidate, no-transform"
"""verify-id-tokens: "Use the value of max-age in the Cache-Control header of the response from that endpoint to
know when to refresh the public keys"."""
ID_TOKEN_LIFETIME = 3600
"""An ID token lasts an hour: `expiresIn` "3600" on every sign-in answer (REST)."""
MIN_PASSWORD = 6
"""A password policy's minimum length "ranges from 6 to 30 characters, with a default of 6"
(https://cloud.google.com/identity-platform/docs/password-policy); with no policy, the default holds."""
MAX_LIST = 1000
DEFAULT_LIST = 20
"""batchGet: "maxResults: The maximum number of results to return. Must be at least 1 and no greater than 1000. By
default, it is 20." (the discovery document)."""

KIND = Literal[
    "identitytoolkit#SignupNewUserResponse",
    "identitytoolkit#VerifyPasswordResponse",
    "identitytoolkit#VerifyCustomTokenResponse",
    "identitytoolkit#VerifyAssertionResponse",
    "identitytoolkit#EmailLinkSigninResponse",
    "identitytoolkit#GetAccountInfoResponse",
    "identitytoolkit#SetAccountInfoResponse",
    "identitytoolkit#DeleteAccountResponse",
    "identitytoolkit#DownloadAccountResponse",
    "identitytoolkit#GetOobConfirmationCodeResponse",
]


# --------------------------------------------------------------------------------------------- errors


class AuthRefusal(ServiceRefusal):
    """Firebase Auth refused: "{"error": {"errors": [{"domain": "global", "reason": "invalid", "message": "CODE"}],
    "code": 400, "message": "CODE"}}" (REST, "Error Response Format"); a sentence after the code follows ` : `,
    which the Admin SDK reads apart from it (`firebase_admin._auth_utils._parse_error_body`)."""

    def __init__(self, code: str, detail: str | None = None, *, status: int = 400) -> None:
        self.message = code if detail is None else f"{code} : {detail}"
        self.status = status
        super().__init__(self.message)

    def render(self, asked: Asked) -> Rendered:
        return Rendered(status=self.status, content_type=JSON, body=error_body(self.status, self.message))


class _ErrorItem(Model):
    message: str
    domain: str = "global"
    reason: str = "invalid"


class _ErrorBody(Model):
    code: int
    message: str
    errors: list[_ErrorItem] | None = None
    status: str | None = None


class _Error(Model):
    error: _ErrorBody


def error_body(status: int, message: str) -> bytes:
    return (
        _Error(error=_ErrorBody(code=status, message=message, errors=[_ErrorItem(message=message)]))
        .model_dump_json(exclude_none=True)
        .encode()
    )


def unimplemented_body(status: int, message: str) -> bytes:
    """What Minutehand answers in the fake's place (501, 500), in Google's API envelope with its status name."""
    named = "UNIMPLEMENTED" if status == 501 else "INTERNAL"
    return (
        _Error(error=_ErrorBody(code=status, message=message, status=named)).model_dump_json(exclude_none=True).encode()
    )


# --------------------------------------------------------------------------------------------- reading


def read[M: BaseModel](model: type[M], raw: bytes, method: str) -> M:
    """A request body as `model`: a field the method has and this fake does not serve, or one it does not have, is
    refused by name; a body that is not a JSON object of the documented fields is refused by name too, since Google's
    words for it are not documented."""
    try:
        decoded = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise NotServed(f"{method} with a body that is not JSON") from e
    if not isinstance(decoded, dict):
        raise NotServed(f"{method} with a body that is not a JSON object")
    unserved = sorted(name for name, value in decoded.items() if name not in model.model_fields and value is not None)
    if unserved:
        raise NotServed(f"{method} with {', '.join(unserved)}")
    try:
        return model.model_validate({k: v for k, v in decoded.items() if v is not None})
    except ValidationError as e:
        where = ", ".join(".".join(str(p) for p in err["loc"]) for err in e.errors())
        raise NotServed(f"{method} with {where} not of its documented type") from e


class Request(Model):
    """Fields every client may send that change nothing in a project with no reCAPTCHA Enterprise: "The reCAPTCHA
    token ... Required when reCAPTCHA enterprise is enabled", "The client type ... Required when reCAPTCHA
    Enterprise is enabled", "Should always be true" (returnSecureToken) (the discovery document)."""

    clientType: str | None = None
    recaptchaVersion: str | None = None
    captchaResponse: str | None = None
    returnSecureToken: bool | None = None
    targetProjectId: str | None = None


class SignUpIn(Request):
    email: str | None = None
    password: str | None = None
    displayName: str | None = None
    photoUrl: str | None = None
    emailVerified: bool | None = None
    disabled: bool | None = None
    localId: str | None = None


class PasswordIn(Request):
    email: str | None = None
    password: str | None = None


class CustomTokenIn(Request):
    token: str | None = None


class IdpIn(Request):
    postBody: str | None = None
    requestUri: str | None = None
    returnIdpCredential: bool | None = None
    returnRefreshToken: bool | None = None


class EmailLinkIn(Request):
    email: str | None = None
    oobCode: str | None = None


class FederatedUserId(Model):
    providerId: str
    rawId: str


class LookupIn(Request):
    localId: list[str] = []
    email: list[str] = []
    federatedUserId: list[FederatedUserId] = []
    idToken: str | None = None


DeletableAttribute = Literal["DISPLAY_NAME", "PHOTO_URL"]


class UpdateIn(Request):
    localId: str | None = None
    idToken: str | None = None
    email: str | None = None
    password: str | None = None
    displayName: str | None = None
    photoUrl: str | None = None
    emailVerified: bool | None = None
    disableUser: bool | None = None
    validSince: int | None = Field(default=None, description="int64 seconds, sent as a number or as a string")
    customAttributes: str | None = None
    deleteAttribute: list[DeletableAttribute] = []
    deleteProvider: list[str] = []


class DeleteIn(Request):
    localId: str | None = None
    idToken: str | None = None


class BatchDeleteIn(Request):
    localIds: list[str] = []
    force: bool = False


OobType = Literal["PASSWORD_RESET", "VERIFY_EMAIL", "EMAIL_SIGNIN"]


class OobIn(Request):
    requestType: OobType | None = None
    email: str | None = None
    returnOobLink: bool | None = None
    continueUrl: str | None = None
    canHandleCodeInApp: bool | None = None
    dynamicLinkDomain: str | None = None
    linkDomain: str | None = None
    iOSBundleId: str | None = None
    androidPackageName: str | None = None
    androidInstallApp: bool | None = None
    androidMinimumVersion: str | None = None


class RefreshIn(Model):
    """The Secure Token API's refresh grant (REST, "Exchange a refresh token for an ID token")."""

    grant_type: str | None = None
    refresh_token: str | None = None


def read_refresh(form: dict[str, str], body: bytes) -> RefreshIn:
    if form:
        return RefreshIn(
            grant_type=form["grant_type"] if "grant_type" in form else None,
            refresh_token=form["refresh_token"] if "refresh_token" in form else None,
        )
    return read(RefreshIn, body, "securetoken.token")


# --------------------------------------------------------------------------------------------- stored


class Link(Model):
    """One provider an account signs in with (`ProviderUserInfo`)."""

    providerId: str
    rawId: str
    federatedId: str | None = None
    email: str | None = None
    displayName: str | None = None
    photoUrl: str | None = None


class StoredUser(Model):
    """An account as the world holds it. The password is kept as a salted digest, never as sent."""

    localId: str
    email: str | None = None
    emailVerified: bool = False
    displayName: str | None = None
    photoUrl: str | None = None
    password_salt: str | None = None
    password_digest: str | None = None
    passwordUpdatedAt: int | None = None
    disabled: bool = False
    customAttributes: str | None = None
    createdAt: int
    lastLoginAt: int | None = None
    lastRefreshAt: datetime | None = None
    validSince: int | None = None
    links: list[Link] = []


class StoredRefresh(Model):
    """A refresh token the world issued, under its digest."""

    uid: str
    auth_time: int
    provider: str
    claims: str | None = Field(default=None, description="The custom token's developer claims, as JSON")


class StoredCode(Model):
    """An out-of-band code a link carries, under its digest."""

    email: str
    request_type: OobType
    used: bool = False


class Project(Model):
    """The project a world is, as its seed says."""

    project_id: str
    api_key: str
    email_enumeration_protection: bool


# --------------------------------------------------------------------------------------------- answered


class UserInfo(Model):
    """`GoogleCloudIdentitytoolkitV1UserInfo`, as lookup and batchGet answer it."""

    localId: str
    email: str | None = None
    emailVerified: bool = False
    displayName: str | None = None
    photoUrl: str | None = None
    passwordUpdatedAt: int | None = None
    providerUserInfo: list[Link] = []
    validSince: str | None = None
    disabled: bool | None = None
    lastLoginAt: str | None = None
    createdAt: str
    customAttributes: str | None = None
    lastRefreshAt: str | None = None


def millis(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def user_info(user: StoredUser) -> UserInfo:
    return UserInfo(
        localId=user.localId,
        email=user.email,
        emailVerified=user.emailVerified,
        displayName=user.displayName,
        photoUrl=user.photoUrl,
        passwordUpdatedAt=user.passwordUpdatedAt,
        providerUserInfo=user.links,
        validSince=str(user.validSince) if user.validSince is not None else None,
        disabled=user.disabled or None,
        lastLoginAt=str(user.lastLoginAt) if user.lastLoginAt is not None else None,
        createdAt=str(user.createdAt),
        customAttributes=user.customAttributes,
        lastRefreshAt=rfc3339(user.lastRefreshAt) if user.lastRefreshAt is not None else None,
    )


class SignedIn(Model):
    """What every sign-in answers: the ID token, its refresh token and its lifetime in seconds."""

    idToken: str
    refreshToken: str
    expiresIn: str = str(ID_TOKEN_LIFETIME)
    localId: str


class SignUpOut(SignedIn):
    kind: KIND = "identitytoolkit#SignupNewUserResponse"
    email: str | None = None
    displayName: str | None = None


class AdminCreated(Model):
    kind: KIND = "identitytoolkit#SignupNewUserResponse"
    localId: str
    email: str | None = None
    displayName: str | None = None


class PasswordOut(SignedIn):
    kind: KIND = "identitytoolkit#VerifyPasswordResponse"
    email: str
    displayName: str = ""
    registered: bool = True


class CustomTokenOut(Model):
    kind: KIND = "identitytoolkit#VerifyCustomTokenResponse"
    idToken: str
    refreshToken: str
    expiresIn: str = str(ID_TOKEN_LIFETIME)
    isNewUser: bool


class IdpOut(SignedIn):
    kind: KIND = "identitytoolkit#VerifyAssertionResponse"
    federatedId: str
    providerId: str
    email: str | None = None
    emailVerified: bool = False
    displayName: str | None = None
    fullName: str | None = None
    firstName: str | None = None
    lastName: str | None = None
    photoUrl: str | None = None
    oauthIdToken: str | None = None
    rawUserInfo: str
    isNewUser: bool | None = None


class EmailLinkOut(SignedIn):
    kind: KIND = "identitytoolkit#EmailLinkSigninResponse"
    email: str
    isNewUser: bool


class LookupOut(Model):
    kind: KIND = "identitytoolkit#GetAccountInfoResponse"
    users: list[UserInfo] | None = None


class UpdateOut(Model):
    kind: KIND = "identitytoolkit#SetAccountInfoResponse"
    localId: str
    email: str | None = None
    displayName: str | None = None
    photoUrl: str | None = None
    emailVerified: bool = False
    providerUserInfo: list[Link] = []


class DeleteOut(Model):
    kind: KIND = "identitytoolkit#DeleteAccountResponse"


class BatchDeleteError(Model):
    index: int
    localId: str
    message: str


class BatchDeleteOut(Model):
    errors: list[BatchDeleteError] | None = None


class DownloadOut(Model):
    kind: KIND = "identitytoolkit#DownloadAccountResponse"
    users: list[UserInfo] | None = None
    nextPageToken: str | None = None


class OobOut(Model):
    kind: KIND = "identitytoolkit#GetOobConfirmationCodeResponse"
    email: str
    oobLink: str | None = None


class RefreshOut(Model):
    """The Secure Token API's answer (REST, "Exchange a refresh token for an ID token": `expires_in`, `token_type`,
    `refresh_token`, `id_token`, `user_id`, `project_id`); `access_token` is the same ID token, which the Firebase
    JS SDK reads (`packages/auth/src/api/authentication/token.ts`)."""

    access_token: str
    expires_in: str = str(ID_TOKEN_LIFETIME)
    token_type: Literal["Bearer"] = "Bearer"
    refresh_token: str
    id_token: str
    user_id: str
    project_id: str


def dump(answer: Model) -> bytes:
    return answer.model_dump_json(exclude_none=True).encode()


# --------------------------------------------------------------------------------------------- tokens


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class CustomTokenClaims(_Loose):
    """What signInWithCustomToken reads of a custom token (create-custom-tokens, "Custom token claims")."""

    iss: str | None = None
    sub: str | None = None
    aud: str | None = None
    uid: str | None = None
    iat: int | None = None
    exp: int | None = None
    tenant_id: str | None = None
    claims: dict[str, JsonValue] | None = None


class GoogleIdClaims(_Loose):
    """What signInWithIdp reads of a Google ID token
    (https://developers.google.com/identity/openid-connect/openid-connect#an-id-tokens-payload)."""

    iss: str | None = None
    sub: str | None = None
    email: str | None = None
    email_verified: bool | None = None
    name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    picture: str | None = None
    exp: int | None = None


class IdTokenClaims(_Loose):
    """What this provider reads back of an ID token it minted."""

    iss: str
    aud: str
    sub: str
    iat: int
    exp: int
    auth_time: int


class _Header(_Loose):
    alg: str | None = None
    kid: str | None = None


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def jwt_parts[M: BaseModel](token: str, model: type[M]) -> tuple[_Header, M, bytes, bytes] | None:
    """A JWT's header, claims as `model`, signing input and signature; None when it is not one."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        header = _Header.model_validate_json(_unb64(parts[0]))
        claims = model.model_validate_json(_unb64(parts[1]))
        signature = _unb64(parts[2])
    except (binascii.Error, ValueError, ValidationError):
        return None
    return header, claims, f"{parts[0]}.{parts[1]}".encode(), signature


def id_token_payload(
    *,
    project: str,
    user: StoredUser,
    auth_time: int,
    issued: int,
    provider: str,
    developer_claims: str | None,
) -> bytes:
    """An ID token's claims (verify-id-tokens, "ID Token Payload Claims", and the `firebase` claim the Admin SDK
    reads `sign_in_provider` from): the user's custom claims, then a custom token's developer claims, at the top
    level, then the reserved ones."""
    identities: dict[str, list[str]] = {}
    if user.email is not None and provider != "anonymous":
        identities["email"] = [user.email]
    for link in user.links:
        if link.providerId != "password":
            identities.setdefault(link.providerId, []).append(link.rawId)
    claims: dict[str, JsonValue] = {}
    for spelled in (user.customAttributes, developer_claims):
        if spelled:
            parsed = json.loads(spelled)
            if isinstance(parsed, dict):
                claims.update(parsed)
    if user.displayName is not None:
        claims["name"] = user.displayName
    if user.photoUrl is not None:
        claims["picture"] = user.photoUrl
    claims.update(
        {
            "iss": ISSUER_PREFIX + project,
            "aud": project,
            "auth_time": auth_time,
            "user_id": user.localId,
            "sub": user.localId,
            "iat": issued,
            "exp": issued + ID_TOKEN_LIFETIME,
        }
    )
    if user.email is not None:
        claims["email"] = user.email
        claims["email_verified"] = user.emailVerified
    claims["firebase"] = {"identities": {k: list(v) for k, v in identities.items()}, "sign_in_provider": provider}
    return json.dumps(claims, separators=(",", ":")).encode()


def jwt_header(kid: str) -> bytes:
    return json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}, separators=(",", ":")).encode()


def custom_attributes(spelled: str) -> str:
    """`customAttributes` as sent, once it is a JSON object: "the claims must be a JSON object" and reserved names
    are refused by the Admin SDK before they are sent."""
    try:
        parsed = json.loads(spelled)
    except json.JSONDecodeError as e:
        raise AuthRefusal("INVALID_CLAIMS") from e
    if not isinstance(parsed, dict):
        raise AuthRefusal("INVALID_CLAIMS")
    return spelled


def raw_user_info(claims: GoogleIdClaims) -> str:
    return claims.model_dump_json(exclude_none=True)


def form_value(form: str, name: str) -> str | None:
    """One field of an `application/x-www-form-urlencoded` string (signInWithIdp's `postBody`)."""
    found = parse_qs(form)
    return found[name][0] if found.get(name) else None
