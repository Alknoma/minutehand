"""Keep credentials out of the record.

An `Exchange` stores no headers, so `Authorization`, `Cookie` and `X-Api-Key` never
reach it. What remains are the places APIs put credentials besides headers: the
query string (`?token=`), a form body (Slack's `token=xoxb-…`) and a JSON body
(an OAuth token response). Values under a credential-named key are replaced; the
shape around them is kept so the provider's normaliser can still read the call.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, quote, unquote_plus, urlsplit, urlunsplit

REDACTED = "[redacted]"

CREDENTIAL_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "password",
        "api_key",
        "apikey",
        "x-api-key",
        "authorization",
        "cookie",
    }
)
"""Exact names only: a tracker's issue `key` or a document's `secret` flag is data, not a credential."""

CAPTURED_QUERY_KEYS = frozenset(
    {
        "key",
        "api_key",
        "access_token",
        "auth",
        "auth_token",
        "secret",
        "signature",
        "sig",
        "x_amz_signature",
        "x_amz_credential",
        "x_amz_security_token",
        "x_goog_api_key",
        "private_token",
    }
)
"""Query parameters that carry a credential on the hosts no provider claims (`adapters.proxy.capture`): there is
no provider to read them, so a name that is data on a tracker (`?key=`) is a key on a search or maps API, and
is redacted. Compared with `-` read as `_`, in any case."""


def _is_credential(name: str, also: frozenset[str] = frozenset()) -> bool:
    return name.lower() in CREDENTIAL_KEYS or name.lower().replace("-", "_") in also


def _pairs(text: str, also: frozenset[str] = frozenset()) -> str:
    """Form pairs with each credential's value redacted in place: every other byte as it was sent."""
    kept: list[str] = []
    for pair in text.split("&"):
        name, equals, _ = pair.partition("=")
        kept.append(f"{name}={quote(REDACTED)}" if equals and _is_credential(unquote_plus(name), also) else pair)
    return "&".join(kept)


def path(raw: str, *, also: frozenset[str] = frozenset()) -> str:
    """A request path with credential query parameters redacted; `also` names more, lower case, `_` for `-`."""
    parts = urlsplit(raw)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not parts.query or not any(_is_credential(k, also) for k, _ in pairs):
        return raw
    return urlunsplit(parts._replace(query=_pairs(parts.query, also)))


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
        if redacted == parsed:
            return text
        return _in_place(text, parsed) or json.dumps(redacted, ensure_ascii=False)
    return text


def _credentials(value: object) -> list[tuple[str, str]]:
    """Every credential field holding a string, anywhere in the JSON: its name and its value."""
    if isinstance(value, dict):
        own = [(k, v) for k, v in value.items() if isinstance(k, str) and _is_credential(k) and isinstance(v, str)]
        return own + [
            f for k, v in value.items() if not (isinstance(k, str) and _is_credential(k)) for f in _credentials(v)
        ]
    if isinstance(value, list):
        return [f for v in value for f in _credentials(v)]
    return []


def _in_place(text: str, parsed: object) -> str | None:
    """The JSON text with each credential's string value replaced where it stands, every other byte as it was; None
    when a credential is not a string, or its value is not written as `json.dumps` writes it, and cannot be found."""
    found = _credentials(parsed)
    if len(found) != len([1 for _ in _every_credential(parsed)]):
        return None
    for name, value in found:
        key = re.escape(json.dumps(name))
        count = 0
        for written in dict.fromkeys([json.dumps(value, ensure_ascii=False), json.dumps(value)]):
            text, count = re.subn(
                rf"({key}\s*:\s*){re.escape(written)}", lambda m: m.group(1) + json.dumps(REDACTED), text
            )
            if count:
                break
        if count == 0:
            return None
    return text


def _every_credential(value: object) -> list[object]:
    """Every credential field's value, whatever its type."""
    if isinstance(value, dict):
        own = [v for k, v in value.items() if isinstance(k, str) and _is_credential(k)]
        return own + [
            f for k, v in value.items() if not (isinstance(k, str) and _is_credential(k)) for f in _every_credential(v)
        ]
    if isinstance(value, list):
        return [f for v in value for f in _every_credential(v)]
    return []


def kept(raw: bytes, content_type: str) -> tuple[str | None, bytes | None]:
    """A body as the record keeps it: UTF-8 text with its credentials redacted, or, when it is not UTF-8 text (a
    file, an image, JSON in another encoding), exactly its bytes. No body is (None, None)."""
    if not raw:
        return None, None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, raw
    return body(text, content_type), None
