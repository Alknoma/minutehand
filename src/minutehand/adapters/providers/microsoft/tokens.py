"""The JSON Web Tokens this provider issues and reads: Microsoft identity platform access and refresh tokens, and
the Bot Framework's token on every activity pushed to a bot.

Every token is a real RS256 JWT signed with `keys.private_key()`, so a caller that validates one against the
published key set (`/discovery/v2.0/keys`, `/v1/.well-known/keys`) accepts it, and Graph and the connector can
tell from a bearer token which tenant, app and user a call is for.

**Times are real time.** `iat`, `nbf` and `exp` are the machine's clock, not the run's: a caller checks a token's
lifetime against its own clock (PyJWT reads `time.time()`), and MSAL decides when to refresh from `expires_in`
on its own clock. A token stamped in simulated time days away from the real one would be refused by the caller,
or held by MSAL after the fake had stopped accepting it. Graph and the connector check `exp` the same way.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import time

from pydantic import ValidationError

from minutehand.adapters.providers.microsoft import keys, wire
from minutehand.adapters.providers.microsoft.wire import TokenUse

LIFETIME_SECONDS = 3599
REFRESH_LIFETIME_SECONDS = 90 * 24 * 3600
SKEW_SECONDS = 300

AAD = "https://login.microsoftonline.com"
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
BOT_FRAMEWORK_TENANT = "d6d49420-f39b-4df7-a1dc-d59a935871db"
"""The tenant the Bot Framework's own channel service signs in from; the connector's tokens name it."""
BOT_FRAMEWORK_AUDIENCE = "https://api.botframework.com"
GRAPH_AUDIENCE = "https://graph.microsoft.com"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"


def now_seconds() -> int:
    return int(time.time())  # clock-lint: exempt a token's lifetime is checked on the caller's machine clock


def issuer_for(tenant: str) -> str:
    return f"{AAD}/{tenant}/v2.0"


def encode(claims: wire.Claims) -> str:
    header = keys.b64url(json.dumps({"alg": "RS256", "kid": keys.key_id(), "typ": "JWT"}).encode())
    payload = keys.b64url(wire.dump_claims(claims).encode())
    signing_input = f"{header}.{payload}"
    return f"{signing_input}.{keys.sign(signing_input.encode())}"


class TokenRefused(Exception):
    """A presented token is not one this provider issued, or is no longer good. `reason` says which."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unpadded(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def decode(token: str, *, use: TokenUse) -> wire.Claims:
    """The claims of a token this provider issued for `use`, its signature and lifetime checked; else refused."""
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenRefused("the token is not a JWT")
    try:
        header = wire.JoseHeader.model_validate_json(_unpadded(parts[0]))
        claims = wire.Claims.model_validate_json(_unpadded(parts[1]))
    except (binascii.Error, ValueError, ValidationError) as e:
        raise TokenRefused("the token's header or claims are not readable") from e
    if header.alg != "RS256" or header.kid != keys.key_id():
        raise TokenRefused("the token was not signed with a key this service publishes")
    if keys.sign(f"{parts[0]}.{parts[1]}".encode()) != parts[2]:
        raise TokenRefused("the token's signature does not verify")
    if claims.minutehand_use is not use:
        raise TokenRefused(f"the token is a {claims.minutehand_use.value} token, not a {use.value} token")
    now = now_seconds()
    if claims.exp + SKEW_SECONDS < now:
        raise TokenRefused("the token has expired")
    if claims.nbf - SKEW_SECONDS > now:
        raise TokenRefused("the token is not valid yet")
    return claims


def issued(
    *,
    use: TokenUse,
    issuer: str,
    audience: str,
    tenant: str | None,
    app_id: str,
    user: str | None,
    roles: list[str] | None = None,
    scopes: str | None = None,
    service_url: str | None = None,
    lifetime: int = LIFETIME_SECONDS,
    nonce: str | None = None,
    redirect_uri: str | None = None,
    identity: wire.GraphUser | None = None,
) -> tuple[str, wire.Claims]:
    now = now_seconds()
    claims = wire.Claims(
        iss=issuer,
        aud=audience,
        iat=now,
        nbf=now,
        exp=now + lifetime,
        tid=tenant,
        appid=app_id,
        azp=app_id,
        oid=user,
        sub=user or app_id,
        roles=roles,
        scp=scopes,
        serviceurl=service_url,
        ver="2.0" if use is not TokenUse.ACTIVITY else "1.0",
        nonce=nonce,
        uti=secrets.token_urlsafe(16),
        redirect_uri=redirect_uri,
        preferred_username=identity.userPrincipalName if identity is not None else None,
        name=identity.displayName if identity is not None else None,
        email=identity.mail if identity is not None else None,
        minutehand_use=use,
    )
    return encode(claims), claims


def derived_trace(text: str) -> str:
    """A trace or correlation id for an error answer: a GUID's shape, derived from what it is about."""
    digest = hashlib.sha256(text.encode()).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
