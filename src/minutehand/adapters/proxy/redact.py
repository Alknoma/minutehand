"""Keep credentials out of the record.

An `Exchange` stores no request headers, so `Authorization`, `Cookie` and `X-Api-Key` never
reach it. What remains are the places APIs put credentials besides headers: the
query string (`?token=`), a form body (Slack's `token=xoxb-…`) and a JSON body
(an OAuth token response). Values under a credential-named key are replaced; the
shape around them is kept so the provider's normaliser can still read the call.

Beside the names, the values: every value the agent's declared environment supplied
(`application.environment`, a `files` or `only` source) is a `Secrets` member, and is
replaced wherever it appears in a path, a header or a body, whatever key it is under.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from urllib.parse import parse_qsl, quote, unquote_plus, urlsplit, urlunsplit

from minutehand.domain.world import Header

REDACTED = "[redacted]"

LEAST = 8
"""Characters a declared value needs to be redacted by value. A shorter one (a port, `true`, a log level) is not a
secret anyone keeps, and replacing it wherever it occurs would take ordinary numbers and words out of the record."""


class Secrets:
    """Values the record must never hold, replaced wherever they appear. Holds nothing of less than `LEAST`
    characters, and says nothing of what it holds."""

    __slots__ = ("_values",)

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values = tuple(sorted({v for v in values if len(v) >= LEAST}, key=len, reverse=True))

    def __bool__(self) -> bool:
        return bool(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Secrets({len(self._values)} values)"

    def hide(self, text: str) -> str:
        """`text` with every value replaced, the longest first, so a value that holds another is one redaction."""
        for value in self._values:
            if value in text:
                text = text.replace(value, REDACTED)
        return text

    def hide_bytes(self, raw: bytes) -> bytes:
        """`raw` with every value's UTF-8 bytes replaced: a secret in a body that is not text is still a secret."""
        for value in self._values:
            encoded = value.encode("utf-8")
            if encoded in raw:
                raw = raw.replace(encoded, REDACTED.encode("utf-8"))
        return raw


NONE = Secrets()
"""No declared values: what every reader redacts with until a run declares some."""

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


_HEADER_CREDENTIALS = frozenset({"set_cookie", "cookie", "proxy_authorization", "www_authenticate"})
"""Header names, beside the credential keys, whose values are a credential: a session cookie, a challenge."""


def _is_credential(name: str, also: frozenset[str] = frozenset()) -> bool:
    return name.lower() in CREDENTIAL_KEYS or name.lower().replace("-", "_") in also


def _pairs(text: str, also: frozenset[str] = frozenset()) -> str:
    """Form pairs with each credential's value redacted in place: every other byte as it was sent."""
    kept: list[str] = []
    for pair in text.split("&"):
        name, equals, _ = pair.partition("=")
        kept.append(f"{name}={quote(REDACTED)}" if equals and _is_credential(unquote_plus(name), also) else pair)
    return "&".join(kept)


def headers(pairs: Sequence[tuple[str, str]], *, secrets: Secrets = NONE) -> list[Header]:
    """Headers as they crossed, in order, the value of each whose name is a credential's (`authorization`,
    `set-cookie`, an API key) redacted, and every declared value (`secrets`) in any other."""
    return [
        Header(name=name, value=REDACTED if _is_credential(name, _HEADER_CREDENTIALS) else secrets.hide(value))
        for name, value in pairs
    ]


def path(raw: str, *, also: frozenset[str] = frozenset(), secrets: Secrets = NONE) -> str:
    """A request path with credential query parameters redacted; `also` names more, lower case, `_` for `-`; every
    declared value (`secrets`) redacted wherever it stands."""
    parts = urlsplit(raw)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not parts.query or not any(_is_credential(k, also) for k, _ in pairs):
        return secrets.hide(raw)
    return secrets.hide(urlunsplit(parts._replace(query=_pairs(parts.query, also))))


def _named(name: object, data: frozenset[str]) -> bool:
    """Whether a JSON key names a credential: one of `CREDENTIAL_KEYS` that is not one of the provider's `data` keys
    (`Manifest.data_keys`)."""
    return isinstance(name, str) and _is_credential(name) and name.lower() not in data


def _walk(value: object, data: frozenset[str]) -> object:
    if isinstance(value, dict):
        return {k: REDACTED if _named(k, data) else _walk(v, data) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, data) for v in value]
    return value


def body(
    text: str | None, content_type: str, *, data: frozenset[str] = frozenset(), secrets: Secrets = NONE
) -> str | None:
    """A request or response body with credential fields redacted, in its own format, and every declared value
    (`secrets`) wherever it stands, whatever the format; a JSON key in `data` (lower case) is the provider's data,
    and kept."""
    if not text:
        return text
    return secrets.hide(_by_name(text, content_type, data))


def _by_name(text: str, content_type: str, data: frozenset[str]) -> str:
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind == "application/x-www-form-urlencoded":
        return _pairs(text)
    if kind == "application/json" or kind.endswith("+json"):
        try:
            parsed: object = json.loads(text)
        except json.JSONDecodeError:
            return text
        redacted = _walk(parsed, data)
        if redacted == parsed:
            return text
        return _in_place(text, parsed, data) or json.dumps(redacted, ensure_ascii=False)
    return text


def _credentials(value: object, data: frozenset[str]) -> list[tuple[str, str]]:
    """Every credential field holding a string, anywhere in the JSON: its name and its value."""
    if isinstance(value, dict):
        own = [(k, v) for k, v in value.items() if _named(k, data) and isinstance(k, str) and isinstance(v, str)]
        return own + [f for k, v in value.items() if not _named(k, data) for f in _credentials(v, data)]
    if isinstance(value, list):
        return [f for v in value for f in _credentials(v, data)]
    return []


def _in_place(text: str, parsed: object, data: frozenset[str]) -> str | None:
    """The JSON text with each credential's string value replaced where it stands, every other byte as it was; None
    when a credential is not a string, or its value is not written as `json.dumps` writes it, and cannot be found."""
    found = _credentials(parsed, data)
    if len(found) != len([1 for _ in _every_credential(parsed, data)]):
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


def _every_credential(value: object, data: frozenset[str]) -> list[object]:
    """Every credential field's value, whatever its type."""
    if isinstance(value, dict):
        own = [v for k, v in value.items() if _named(k, data)]
        return own + [f for k, v in value.items() if not _named(k, data) for f in _every_credential(v, data)]
    if isinstance(value, list):
        return [f for v in value for f in _every_credential(v, data)]
    return []


def kept(
    raw: bytes, content_type: str, *, data: frozenset[str] = frozenset(), secrets: Secrets = NONE
) -> tuple[str | None, bytes | None]:
    """A body as the record keeps it: UTF-8 text with its credentials redacted, or, when it is not UTF-8 text (a
    file, an image, JSON in another encoding), exactly its bytes, with every declared value's bytes redacted. No
    body is (None, None). `data` names the JSON keys of the answering provider's own data (`Manifest.data_keys`),
    kept as sent."""
    if not raw:
        return None, None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, secrets.hide_bytes(raw)
    return body(text, content_type, data=data, secrets=secrets), None
