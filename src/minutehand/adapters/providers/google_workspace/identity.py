"""Google's identity infrastructure beside the Workspace APIs, on the hosts this provider already answers: IAM
Credentials' `signBlob` and `signJwt` (`iamcredentials.googleapis.com`), the certificates Google's OAuth2 ID tokens
are signed with (`www.googleapis.com/oauth2/v1/certs` and `/oauth2/v3/certs`), the ID token `/token` adds to an
answer whose scope asks for one, and the metadata server's runtime service account
(`metadata.google.internal`, plain HTTP, as on Cloud Run and Compute Engine).

**Keys.** Each service account signs with a key derived from its address, and Google's OAuth2 ID tokens with a key
derived from a fixed seed (`adapters.derived_key`): the same in every world, since a verifier caches the
certificates it fetched. A blob signed for a service account the world does not hold is signed all the same, as any
credential acts (`docs/design.md`, "Authentication is out of scope"); who may sign as whom is IAM's permission,
which nothing here checks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from functools import cache

from cryptography.hazmat.primitives import serialization
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from minutehand.adapters.derived_key import DerivedKey, b64url
from minutehand.adapters.google_account import account_id
from minutehand.adapters.providers.google_workspace import wire
from minutehand.adapters.providers.google_workspace.state import DriveWorld
from minutehand.domain.errors import NotServed
from minutehand.domain.scenario import Model
from minutehand.ports.clock import Clock

OAUTH2_KEY = DerivedKey("minutehand google oauth2 id token signing key, version 1")
GOOGLE_ISSUER = "https://accounts.google.com"
IDENTITY_SCOPES = frozenset({"openid", "email", "profile"})
"""The scopes that make Google's token endpoint answer an `id_token` too: "id_token ... A JWT that contains
identity information about the user ... returned if your request included an identity scope, such as openid,
profile, or email" (https://developers.google.com/identity/protocols/oauth2/web-server#exchange-authorization-code)."""
METADATA_HOST = "metadata.google.internal"
METADATA_FLAVOR = "Google"
JSON = "application/json; charset=UTF-8"


@cache
def service_account_key(email: str) -> DerivedKey:
    """The key a service account signs with: one per address, the same in every world."""
    return DerivedKey(f"minutehand google service account key, version 1, {email.lower()}")


def key_id(key: DerivedKey) -> str:
    """A key's id as Google names its service account keys and its OAuth2 certificates: 40 hexadecimal digits."""
    public = key.private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha1(public).hexdigest()


def jwt(key: DerivedKey, claims: Model | str) -> str:
    """An RS256 JWT of `claims` (a model, or a JSON object already serialized), signed with `key`."""
    header = b64url(json.dumps({"alg": "RS256", "kid": key_id(key), "typ": "JWT"}).encode())
    body = claims if isinstance(claims, str) else claims.model_dump_json(exclude_none=True)
    signing_input = f"{header}.{b64url(body.encode())}"
    return f"{signing_input}.{b64url(key.sign(signing_input.encode()))}"


class IdTokenClaims(Model):
    """An OpenID Connect ID token Google issues (https://developers.google.com/identity/openid-connect/openid-connect#an-id-tokens-payload)."""

    iss: str = GOOGLE_ISSUER
    azp: str
    aud: str
    sub: str
    email: str
    email_verified: bool = True
    name: str | None = None
    iat: int
    exp: int


def id_token(*, email: str, name: str | None, client_id: str, issued: int, lifetime: int) -> str:
    """The ID token Google's token endpoint answers beside an access token, for the account `email`."""
    claims = IdTokenClaims(
        azp=client_id, aud=client_id, sub=account_id(email), email=email, name=name, iat=issued, exp=issued + lifetime
    )
    return jwt(OAUTH2_KEY, claims)


def asks_for_identity(scope: str | None) -> bool:
    return scope is not None and bool(IDENTITY_SCOPES & set(scope.split()))


class SignIn(Model):
    """`signBlob`'s and `signJwt`'s request body (https://cloud.google.com/iam/docs/reference/credentials/rest/v1/projects.serviceAccounts/signBlob)."""

    payload: str
    delegates: list[str] = []


class SignedBlob(Model):
    keyId: str
    signedBlob: str


class SignedJwt(Model):
    keyId: str
    signedJwt: str


class Jwk(Model):
    kty: str = "RSA"
    alg: str = "RS256"
    use: str = "sig"
    kid: str
    n: str
    e: str


class JwkSet(Model):
    keys: list[Jwk]


def _json(answer: Model) -> Response:
    return Response(answer.model_dump_json(exclude_none=True), media_type=JSON)


def _invalid(message: str) -> wire.Refusal:
    return wire.docs_refusal(400, "INVALID_ARGUMENT", message)


class IdentityApi:
    def __init__(self, drive: DriveWorld, clock: Clock) -> None:
        self._drive = drive
        self._clock = clock

    async def _signing(self, request: Request) -> tuple[str, SignIn]:
        """The service account a sign call names, and its body. "The `-` wildcard character is required; replacing
        it with a project ID is invalid" (signBlob's `name`)."""
        if request.path_params["project"] != "-":
            raise _invalid(
                f"projects/{request.path_params['project']}: the - wildcard character is required in place of the "
                "project id"
            )
        try:
            asked = SignIn.model_validate_json(await request.body() or b"{}")
        except ValidationError as e:
            raise _invalid(f"Invalid JSON payload received. {e.errors()[0]['msg']}") from e
        if asked.delegates:
            raise NotServed("signing through a chain of delegates (`delegates`) is not served")
        return request.path_params["account"], asked

    async def sign_blob(self, request: Request) -> Response:
        try:
            account, asked = await self._signing(request)
            try:
                blob = base64.b64decode(asked.payload, validate=True)
            except binascii.Error as e:
                raise _invalid("Invalid value at 'payload' (TYPE_BYTES), Base64 decoding failed") from e
        except wire.Refusal as refusal:
            return _refused(refusal)
        key = service_account_key(account)
        return _json(SignedBlob(keyId=key_id(key), signedBlob=base64.b64encode(key.sign(blob)).decode()))

    async def sign_jwt(self, request: Request) -> Response:
        try:
            account, asked = await self._signing(request)
            try:
                claims = json.loads(asked.payload)
            except json.JSONDecodeError as e:
                raise _invalid("payload: the JWT payload must be a serialized JSON object") from e
            if not isinstance(claims, dict):
                raise _invalid("payload: the JWT payload must be a serialized JSON object")
        except wire.Refusal as refusal:
            return _refused(refusal)
        key = service_account_key(account)
        return _json(SignedJwt(keyId=key_id(key), signedJwt=jwt(key, asked.payload)))

    async def certs_v1(self, request: Request) -> Response:
        """The x509 certificates Google's OAuth2 ID tokens verify against, by key id
        (https://www.googleapis.com/oauth2/v1/certs, google-auth's `_GOOGLE_OAUTH2_CERTS_URL`)."""
        certificates = {key_id(OAUTH2_KEY): OAUTH2_KEY.certificate("accounts.google.com")}
        return Response(json.dumps(certificates), media_type=JSON)

    async def certs_v3(self, request: Request) -> Response:
        """The same key as a JSON Web Key Set (https://www.googleapis.com/oauth2/v3/certs, the `jwks_uri` of
        https://accounts.google.com/.well-known/openid-configuration)."""
        return _json(JwkSet(keys=[Jwk(kid=key_id(OAUTH2_KEY), n=OAUTH2_KEY.modulus, e=OAUTH2_KEY.exponent)]))

    async def metadata_email(self, request: Request) -> Response:
        """`/computeMetadata/v1/instance/service-accounts/{default|email}/email`: the runtime service account's
        address (https://cloud.google.com/run/docs/container-contract#metadata-server), as text. A request without
        `Metadata-Flavor: Google` is denied by the metadata server, in words and a status Google does not
        document, so it is refused by name."""
        if request.headers.get("metadata-flavor") != METADATA_FLAVOR:
            raise NotServed(
                "a metadata server request without the header Metadata-Flavor: Google: the metadata server denies "
                "it, and Google does not document its answer"
            )
        runtime = self._drive.runtime_account()
        if runtime is None:
            raise NotServed(
                "the runtime service account of a world that seeds none: give google_workspace a provider seed "
                "with runtime_service_account"
            )
        named = request.path_params["account"]
        if named not in ("default", runtime.email):
            raise NotServed(f"the metadata server's entries for {named}, which is not the runtime service account")
        return Response(runtime.email, media_type="application/text", headers={"Metadata-Flavor": METADATA_FLAVOR})

    async def metadata_unserved(self, request: Request) -> Response:
        raise NotServed(
            f"the metadata server's {request.url.path} is not served; only the runtime service account's email is"
        )


def _refused(refusal: wire.Refusal) -> Response:
    return Response(wire.error_body(refusal), status_code=refusal.code, media_type=JSON)
