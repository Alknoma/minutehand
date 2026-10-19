"""The FCM HTTP v1 API as it puts it on the wire: proto3 JSON, either spelling of a field's name on the way in (the
Admin SDKs send `validate_only`, Google's discovery clients `validateOnly`), and Google's error envelope with the
`details` FCM documents.

https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages
"""

from __future__ import annotations

import re

from pydantic import AliasChoices, ConfigDict, Field, JsonValue

from minutehand.domain.errors import Asked, Rendered, ServiceRefusal
from minutehand.domain.scenario import Model

JSON = "application/json; charset=UTF-8"

PAYLOAD_LIMIT = 4096
""""Check that the total size of the payload data included in a message does not exceed FCM limits: 4096 bytes for
most messages ... This includes both the keys and the values" (https://firebase.google.com/docs/reference/fcm/rest/v1/ErrorCode)."""

RESERVED_KEYS = ("from", "message_type")
RESERVED_PREFIXES = ("google.", "gcm.notification.")
"""Message.data: "The key should not be a reserved word ("from", "message_type", or any word starting with "google."
or "gcm.notification.")" (https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages)."""

INVALID_TOKEN = "The registration token is not a valid FCM registration token"
"""The message FCM answers a token it never issued (https://firebase.google.com/docs/cloud-messaging/error-codes,
Example 2)."""
UNREGISTERED = "Requested entity was not found."
"""The message FCM answers an unregistered token with, beside `UNREGISTERED` (observed:
https://community.privacyidea.org/t/firebase-access-suddenly-broken-partially/2018)."""
INVALID_ARGUMENT = "The client specified an invalid argument."
"""google.rpc.Code INVALID_ARGUMENT (https://github.com/googleapis/googleapis/blob/master/google/rpc/code.proto)."""
TARGET_RULE = (
    "Required. Input only. Target to send a message to. The following is a list of mutually exclusive fields: "
    "token, fid, topic, condition."
)
"""Message's union field `target` (projects.messages)."""
TOO_BIG = (
    "Check that the total size of the payload data included in a message does not exceed FCM limits: 4096 bytes "
    "for most messages. This includes both the keys and the values."
)
"""ErrorCode, INVALID_ARGUMENT, "Message too big"."""
RESERVED_RULE = (
    'The key should not be a reserved word ("from", "message_type", or any word starting with "google." or '
    '"gcm.notification.").'
)
"""Message.data (projects.messages)."""

FCM_ERROR = "type.googleapis.com/google.firebase.fcm.v1.FcmError"
BAD_REQUEST = "type.googleapis.com/google.rpc.BadRequest"


class Wire(Model):
    """A body as Google reads it: unknown fields refused, either spelling of a field's name accepted, as proto3 JSON
    parsers accept "both the lowerCamelCase name and the proto field name" (https://protobuf.dev/programming-guides/json/)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class NotificationIn(Wire):
    """`Notification`: the basic notification template across all platforms."""

    title: str | None = None
    body: str | None = None
    image: str | None = None


class WebpushIn(Wire):
    """`WebpushConfig`: its headers are kept as sent; the rest is kept verbatim with the message."""

    headers: dict[str, str] = {}
    data: JsonValue = None
    notification: JsonValue = None
    fcmOptions: JsonValue = Field(default=None, validation_alias=AliasChoices("fcmOptions", "fcm_options"))


class MessageIn(Wire):
    """`Message`, as `messages.send` reads it. `data` is read as sent, so a value that is not a string is refused as
    FCM refuses it, naming its place."""

    name: str | None = Field(default=None, description="Output only: ignored on input")
    data: JsonValue = None
    notification: NotificationIn | None = None
    android: JsonValue = None
    webpush: WebpushIn | None = None
    apns: JsonValue = None
    fcmOptions: JsonValue = Field(default=None, validation_alias=AliasChoices("fcmOptions", "fcm_options"))
    token: str | None = None
    fid: str | None = None
    topic: str | None = None
    condition: str | None = None

    def targets(self) -> list[str]:
        """The members of the union field `target` this message sets."""
        given = {"token": self.token, "fid": self.fid, "topic": self.topic, "condition": self.condition}
        return [name for name, value in given.items() if value is not None]


class SendIn(Wire):
    """`SendMessageRequest`."""

    message: MessageIn
    validateOnly: bool = Field(default=False, validation_alias=AliasChoices("validateOnly", "validate_only"))


class FieldViolation(Model):
    field: str
    description: str


class Extension(Model):
    """One entry of an error's `details`: FCM's `FcmError`, or a `google.rpc.BadRequest` naming the field."""

    type_url: str = Field(serialization_alias="@type")
    errorCode: str | None = None
    fieldViolations: list[FieldViolation] | None = None


class ErrorBody(Model):
    code: int
    message: str
    status: str
    extensions: list[Extension] = Field(default=[], serialization_alias="details")


class GoogleError(Model):
    error: ErrorBody


def fcm_error(code: str) -> Extension:
    return Extension(type_url=FCM_ERROR, errorCode=code)


def bad_request(field: str, description: str) -> Extension:
    return Extension(type_url=BAD_REQUEST, fieldViolations=[FieldViolation(field=field, description=description)])


class FcmRefusal(ServiceRefusal):
    """FCM would refuse this request: its status, Google's status word, its message and its details."""

    def __init__(self, code: int, word: str, message: str, extension: Extension) -> None:
        super().__init__(message)
        self.code = code
        self.word = word
        self.message = message
        self.extension = extension

    def render(self, asked: Asked) -> Rendered:
        return error_answer(self.code, self.word, self.message, [self.extension])


def error_answer(status: int, word: str, message: str, extensions: list[Extension]) -> Rendered:
    """Google's error envelope as FCM answers it (https://firebase.google.com/docs/cloud-messaging/error-codes),
    which `firebase-admin` reads its error type from: `status`, then an `FcmError`'s `errorCode`."""
    body = GoogleError(error=ErrorBody(code=status, message=message, status=word, extensions=extensions))
    return Rendered(
        status=status,
        content_type=JSON,
        body=body.model_dump_json(by_alias=True, exclude_none=True).encode(),
    )


def invalid_value(at: str, shown: str) -> FcmRefusal:
    """A value of the wrong type, as FCM words it: "Invalid value at 'message.data[0].value' (TYPE_STRING), 12"
    (https://firebase.google.com/docs/cloud-messaging/error-codes, Example 1)."""
    said = f"Invalid value at '{at}' (TYPE_STRING), {shown}"
    return FcmRefusal(400, "INVALID_ARGUMENT", said, bad_request(at, said))


# --- the API's surface ------------------------------------------------------------------------------------------

_R = "/v1/projects/[^/:]+/registrations/[^/:]+/topicSubscriptions"
REST_METHODS: dict[str, tuple[str, re.Pattern[str]]] = {
    "fcm.projects.messages.send": ("POST", re.compile("/v1/projects/[^/:]+/messages:send")),
    "fcm.projects.registrations.topicSubscriptions.create": ("POST", re.compile(_R)),
    "fcm.projects.registrations.topicSubscriptions.delete": ("DELETE", re.compile(_R + "/[^/:]+")),
    "fcm.projects.registrations.topicSubscriptions.get": ("GET", re.compile(_R + "/[^/:]+")),
    "fcm.projects.registrations.topicSubscriptions.list": ("GET", re.compile(_R)),
    "fcm.projects.registrations.topicSubscriptions.patch": ("PATCH", re.compile(_R + "/[^/:]+")),
}
"""Every method of FCM's discovery document (v1), by its `httpMethod` and `flatPath`."""

REST_SERVED = frozenset({"fcm.projects.messages.send"})

_TOPICS = "topics are not served: a world's messages go to its seeded registration tokens"
REST_REFUSED: dict[str, str] = {
    "fcm.projects.registrations.topicSubscriptions.create": _TOPICS,
    "fcm.projects.registrations.topicSubscriptions.delete": _TOPICS,
    "fcm.projects.registrations.topicSubscriptions.get": _TOPICS,
    "fcm.projects.registrations.topicSubscriptions.list": _TOPICS,
    "fcm.projects.registrations.topicSubscriptions.patch": _TOPICS,
}
"""Why each REST method not served is refused."""


def rest_method(method: str, path: str) -> str | None:
    """The discovery method id a REST request calls, or None when no method has that route."""
    return next((m for m, (verb, route) in REST_METHODS.items() if verb == method and route.fullmatch(path)), None)
