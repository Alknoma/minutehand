"""What an external emulator's answer was (`CallOutcome`), and what its call asked for (the operation).

An emulator's errors are of three kinds that must not read alike: one the real API would give (a 4xx, or a 5xx or
in-body error its declaration lists as faithful), one that says it has no implementation of the call (a 501, or a
declared marker), and its own failure (any other 5xx). Only the last is the emulator's fault; none is the agent's.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from minutehand.adapters.proxy.capture import structured, values_at
from minutehand.domain.emulator import ErrorMarker, ExternalEmulator
from minutehand.domain.world import CallOutcome

_NAMED = re.compile(r"^\s*(?:query|mutation|subscription)\s+([_A-Za-z][_0-9A-Za-z]*)")


def _marks(marker: ErrorMarker, status: int, parsed: object | None) -> bool:
    if marker.status is not None and marker.status != status:
        return False
    if marker.at is None:
        return True
    found = values_at(parsed, marker.at) if parsed is not None else []
    if marker.equals is None:
        return bool(found)
    return any(str(value) == marker.equals for value in found)


def outcome(declaration: ExternalEmulator, status: int, body: str | None, content_type: str | None) -> CallOutcome:
    """Not implemented first, then a faithful error, then by status: 5xx its own failure, 4xx a refusal."""
    parsed = structured(body, content_type) if body is not None else None
    if any(_marks(m, status, parsed) for m in declaration.not_implemented):
        return CallOutcome.NOT_IMPLEMENTED
    if any(_marks(m, status, parsed) for m in declaration.faithful):
        return CallOutcome.REFUSED
    if status >= 500:
        return CallOutcome.INTERNAL_ERROR
    if status >= 400:
        return CallOutcome.REFUSED
    return CallOutcome.ANSWERED


def operation(method: str, path: str, body: str | None, content_type: str | None) -> str:
    """A GraphQL call's operation name (`operationName`, else the name its document gives it), else the method and
    the path without its query."""
    parsed = structured(body, content_type) if body is not None else None
    if isinstance(parsed, dict):
        name = parsed["operationName"] if "operationName" in parsed else None
        if isinstance(name, str) and name:
            return name
        document = parsed["query"] if "query" in parsed else None
        if isinstance(document, str):
            named = _NAMED.match(document)
            if named is not None:
                return named.group(1)
    return f"{method.upper()} {urlsplit(path).path}"
