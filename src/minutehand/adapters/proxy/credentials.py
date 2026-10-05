"""The credentials a call carries and the ones an answer hands out, read the way the OAuth standards put them,
never a provider's own format. `minutehand serve` routes a call to the world that claims one of them.

What a call carries (RFC 6750, RFC 6749, RFC 7523):
  - `Authorization: Bearer <token>`; the password of `Authorization: Basic`
  - `access_token` in the query string or a form body
  - a token request's `refresh_token`, and its JWT `assertion`'s `iss` and `sub` (a service account's email)

What an answer hands out (RFC 6749 §5.1): `access_token` and `refresh_token` in a JSON body.
"""

from __future__ import annotations

import base64
import binascii
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

FORM = "application/x-www-form-urlencoded"
JSON = "application/json"


class _TokenAnswer(BaseModel):
    """The fields of RFC 6749's token answer that later calls present; the rest is the provider's."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    access_token: str
    refresh_token: str | None = None


class _Assertion(BaseModel):
    """The claims of a JWT-bearer grant that name who signs in."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    iss: str | None = None
    sub: str | None = None


def _media(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _single(found: dict[str, list[str]], name: str) -> list[str]:
    return [v for v in found[name] if v] if name in found else []


def _claims(assertion: str) -> list[str]:
    parts = assertion.split(".")
    if len(parts) != 3:
        return []
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = _Assertion.model_validate_json(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, ValidationError):
        return []
    return [c for c in (claims.iss, claims.sub) if c]


def presented(*, authorization: str | None, path: str, content_type: str, body: bytes) -> list[str]:
    """Every credential the call presents, the strongest first: its Authorization header, then the query, then
    the form body."""
    found: list[str] = []
    if authorization:
        scheme, _, value = authorization.strip().partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            found.append(value.strip())
        elif scheme.lower() == "basic" and value.strip():
            try:
                user, _, password = base64.b64decode(value.strip()).decode("utf-8").partition(":")
            except (binascii.Error, UnicodeDecodeError):
                user, password = "", ""
            found += [c for c in (password, user) if c]
    query = parse_qs(urlsplit(path).query)
    found += _single(query, "access_token")
    if _media(content_type) == FORM and body:
        form = parse_qs(body.decode("utf-8", errors="replace"))
        found += _single(form, "access_token") + _single(form, "refresh_token")
        for assertion in _single(form, "assertion"):
            found += _claims(assertion)
    return list(dict.fromkeys(found))


def minted(*, content_type: str, body: bytes) -> list[str]:
    """The credentials an answer hands out for later calls: an OAuth token answer's access and refresh tokens."""
    if _media(content_type) != JSON or not body:
        return []
    try:
        answer = _TokenAnswer.model_validate_json(body)
    except ValidationError:
        return []
    return [t for t in (answer.access_token, answer.refresh_token) if t]
