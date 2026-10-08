"""The JSON Web Tokens this provider issues and reads: Microsoft identity platform access and refresh tokens, and
the Bot Framework's token on every activity pushed to a bot.

Every token is a real RS256 JWT signed with `keys.private_key()`, so a caller that validates one against the
published key set (`/discovery/v2.0/keys`, `/v1/.well-known/keys`) accepts it, and Graph and the connector can
tell from a bearer token which tenant, app and user a call is for.

**Times are real time.** `iat`, `nbf` and `exp` are the machine's clock, not the run's: a caller checks a token's
lifetime against its own clock (PyJWT reads `time.time()`), and MSAL decides when to refresh from `expires_in`
on its own clock. A token stamped in simulated time days away from the real one would be refused by the caller.

**Nothing presented is checked.** Graph, the connector and the identity platform read a presented token only for
the tenant, app and user it names (`presented`); its signature, lifetime, audience and use are never checked, and
a token that cannot be read at all is the tenant's application calling.
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


def presented(token: str | None) -> wire.Claims | None:
    """The claims a presented token carries, read without checking its signature, lifetime, audience or use: None
    when it is not a JWT this provider can read. Minutehand does not enforce credentials (CLAIMS.md), so nothing a
    caller presents is refused; a token is read only for whom it names."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        return wire.Claims.model_validate_json(_unpadded(parts[1]))
    except (binascii.Error, ValueError, ValidationError):
        return None


def _unpadded(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


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
