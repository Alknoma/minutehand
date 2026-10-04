"""Keep credentials out of the record.

An `Exchange` stores no headers, so `Authorization`, `Cookie` and `X-Api-Key` never
reach it. What remains are the places APIs put credentials besides headers: the
query string (`?token=`), a form body (Slack's `token=xoxb-…`) and a JSON body
(an OAuth token response). Values under a credential-named key are replaced; the
shape around them is kept so the provider's normaliser can still read the call.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[redacted]"

CREDENTIAL_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "client_secret", "password",
    "api_key", "apikey", "x-api-key", "authorization", "cookie",
})
"""Exact names only: a tracker's issue `key` or a document's `secret` flag is data, not a credential."""


def _is_credential(name: str) -> bool:
    return name.lower() in CREDENTIAL_KEYS


def _pairs(text: str) -> str:
    return urlencode(
        [(k, REDACTED if _is_credential(k) else v) for k, v in parse_qsl(text, keep_blank_values=True)]
    )


def path(raw: str) -> str:
    """A request path with credential query parameters redacted."""
    parts = urlsplit(raw)
    if not parts.query or not any(_is_credential(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return raw
    return urlunsplit(parts._replace(query=_pairs(parts.query)))


def _walk(value: object) -> object:
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _is_credential(k) else _walk(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v) for v in value]
    return value


def body(text: str | None, content_type: str) -> str | None:
    """A request or response body with credential fields redacted, in its own format."""
    if not text:
        return text
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind == "application/x-www-form-urlencoded":
        return _pairs(text)
    if kind == "application/json" or kind.endswith("+json"):
        try:
            parsed: object = json.loads(text)
        except json.JSONDecodeError:
            return text
        redacted = _walk(parsed)
        return text if redacted == parsed else json.dumps(redacted, ensure_ascii=False)
    return text
