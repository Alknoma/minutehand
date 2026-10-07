"""Cloud Tasks v2 as its REST API puts it on the wire: proto3 JSON, field names in camelCase, durations as
`"3.5s"`, timestamps as RFC 3339, bytes as base64, and Google's error envelope.

https://cloud.google.com/tasks/docs/reference/rest
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from pydantic import ConfigDict, Field, JsonValue

from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model

JSON = "application/json; charset=UTF-8"

DEFAULT_MIN_BACKOFF = timedelta(seconds=0.1)
DEFAULT_MAX_BACKOFF = timedelta(hours=1)
DEFAULT_MAX_DOUBLINGS = 16
DEFAULT_MAX_ATTEMPTS = 100
DEFAULT_DISPATCH_DEADLINE = timedelta(minutes=10)
NAME_REUSE_AFTER = timedelta(hours=1)
"""How long a task's name stays taken once the task was deleted or ran: Cloud Tasks refuses it ALREADY_EXISTS."""

_STATUS_WORD = {
    400: "INVALID_ARGUMENT",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ALREADY_EXISTS",
    412: "FAILED_PRECONDITION",
    500: "INTERNAL",
    501: "UNIMPLEMENTED",
}


class Wire(Model):
    """A body as Google sends or reads it: camelCase, and unknown fields refused as Google refuses them."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class RetryConfig(Wire):
    maxAttempts: int | None = None
    maxRetryDuration: str | None = None
    minBackoff: str | None = None
    maxBackoff: str | None = None
    maxDoublings: int | None = None


class RateLimits(Wire):
    maxDispatchesPerSecond: float | None = None
    maxBurstSize: int | None = None
    maxConcurrentDispatches: int | None = None


class QueueIn(Wire):
    name: str
    rateLimits: RateLimits | None = None
    retryConfig: RetryConfig | None = None
    state: str | None = None


class OidcToken(Wire):
    serviceAccountEmail: str
    audience: str | None = None


class OAuthToken(Wire):
    serviceAccountEmail: str
    scope: str | None = None


HTTP_METHODS = ("HTTP_METHOD_UNSPECIFIED", "POST", "GET", "HEAD", "PUT", "DELETE", "PATCH", "OPTIONS")
"""`HttpMethod` by its number: Google's clients ask for enums as numbers (`$alt=json;enum-encoding=int`)."""
VIEWS = ("VIEW_UNSPECIFIED", "BASIC", "FULL")


def enum_name(value: str | int, names: tuple[str, ...], field: str) -> str:
    """An enum as proto3 JSON carries it, by name or by number."""
    if isinstance(value, int):
        if not 0 <= value < len(names):
            raise TasksRefusal(400, f"Invalid value {value} for enum field {field}")
        return names[value]
    if value not in names:
        raise TasksRefusal(400, f"Invalid value {value!r} for enum field {field}")
    return value


class HttpRequestIn(Wire):
    url: str
    httpMethod: str | int = "POST"
    headers: dict[str, str] = {}
    body: str | None = Field(default=None, description="Base64")
    oidcToken: OidcToken | None = None
    oauthToken: OAuthToken | None = None


class TaskIn(Wire):
    name: str | None = None
    scheduleTime: str | None = None
    dispatchDeadline: str | None = None
    httpRequest: HttpRequestIn | None = None
    appEngineHttpRequest: JsonValue = None


class CreateTaskIn(Wire):
    task: TaskIn
    responseView: str | int | None = None


class ErrorBody(Wire):
    code: int
    message: str
    status: str


class GoogleError(Wire):
    error: ErrorBody


class TasksRefusal(ServiceRefusal):
    """Cloud Tasks would refuse this request: its status, Google's status word and its message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def render(self, asked: Asked) -> Rendered:
        return error_answer(self.code, _STATUS_WORD.get(self.code, "UNKNOWN"), self.message)


def error_answer(status: int, word: str, message: str) -> Rendered:
    """Google's error envelope, which `google.api_core` reads its exception type from (`status`)."""
    body = GoogleError(error=ErrorBody(code=status, message=message, status=word))
    return Rendered(status=status, content_type=JSON, body=body.model_dump_json().encode())


def duration(text: str) -> timedelta:
    """`"3.5s"` as proto3 JSON writes a Duration."""
    found = re.fullmatch(r"(-?\d+(?:\.\d+)?)s", text)
    if found is None:
        raise TasksRefusal(400, f"Invalid duration {text!r}: expected seconds with an 's' suffix")
    return timedelta(seconds=float(found.group(1)))


def seconds(span: timedelta) -> str:
    total = span.total_seconds()
    return f"{total:.9f}".rstrip("0").rstrip(".") + "s"


def moment(text: str) -> datetime:
    """An RFC 3339 timestamp, as proto3 JSON writes one."""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise TasksRefusal(400, f"Invalid timestamp {text!r}") from e
    if parsed.tzinfo is None:
        raise TasksRefusal(400, f"Invalid timestamp {text!r}: no offset")
    return parsed.astimezone(UTC)


def stamp(when: datetime) -> str:
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")
