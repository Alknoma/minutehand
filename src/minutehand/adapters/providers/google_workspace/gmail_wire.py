"""Gmail v1's own JSON, and the RFC 2822 messages it carries.

A message is kept as the bytes it was sent as (`StoredMail.raw`, base64url, as Gmail's `raw` format carries it), with
the mailbox it is in, its thread there, its labels and when Gmail took it in. Everything else Gmail serves (the
payload's MIME tree, the headers, the snippet, the size) is read from those bytes when it is asked for, so what the
agent sends is what it reads back.
"""

from __future__ import annotations

import base64
import binascii
import re
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses
from typing import Literal

from pydantic import Field

from minutehand.adapters.providers.google_workspace import wire
from minutehand.domain.scenario import Model

INBOX = "INBOX"
SENT = "SENT"
UNREAD = "UNREAD"
STARRED = "STARRED"
IMPORTANT = "IMPORTANT"
TRASH = "TRASH"
SPAM = "SPAM"
DRAFT = "DRAFT"
SYSTEM_LABELS = (
    INBOX,
    SENT,
    UNREAD,
    STARRED,
    IMPORTANT,
    TRASH,
    SPAM,
    DRAFT,
    "CHAT",
    "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
)
"""Gmail's system labels, as `users.labels.list` names them."""
HIDDEN_LABELS = frozenset({"CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS"})
"""System labels Gmail lists with `labelListVisibility: labelHide`."""
SNIPPET_LENGTH = 200
MAX_RESULTS = 500


# --------------------------------------------------------------------------- stored


class StoredMail(Model):
    """One message in one mailbox. A message sent between two people in the world is one of these in each mailbox,
    each with its own id and thread, as in Gmail."""

    kind: Literal["mail"] = "mail"
    mailbox: str = Field(description="The address of the mailbox it is in")
    id: str
    thread_id: str
    labels: list[str]
    raw: str = Field(description="The RFC 2822 message, base64url")
    internal_date: int = Field(description="Milliseconds since the epoch Gmail took it in")


class UserLabel(Model):
    """A label a mailbox's owner made, seeded by the scenario."""

    id: str
    name: str


# --------------------------------------------------------------------------- served


class Header(Model):
    name: str
    value: str


class PartBody(Model):
    size: int
    data: str | None = None


class MessagePart(Model):
    partId: str
    mimeType: str
    filename: str
    headers: list[Header]
    body: PartBody
    parts: list[MessagePart] | None = None


class GmailMessage(Model):
    id: str
    threadId: str
    labelIds: list[str] | None = None
    snippet: str | None = None
    historyId: str | None = None
    internalDate: str | None = None
    sizeEstimate: int | None = None
    payload: MessagePart | None = None
    raw: str | None = None


class MessageRef(Model):
    id: str
    threadId: str


class MessageList(Model):
    messages: list[MessageRef] | None = None
    nextPageToken: str | None = None
    resultSizeEstimate: int


class GmailThread(Model):
    id: str
    snippet: str | None = None
    historyId: str
    messages: list[GmailMessage] | None = None


class ThreadList(Model):
    threads: list[GmailThread] | None = None
    nextPageToken: str | None = None
    resultSizeEstimate: int


class Label(Model):
    id: str
    name: str
    type: Literal["system", "user"]
    messageListVisibility: str | None = None
    labelListVisibility: str | None = None


class LabelList(Model):
    labels: list[Label]


class Profile(Model):
    emailAddress: str
    messagesTotal: int
    threadsTotal: int
    historyId: str


class LabelledMessage(Model):
    message: GmailMessage
    labelIds: list[str] | None = None


class HistoryRecord(Model):
    id: str
    messages: list[GmailMessage]
    messagesAdded: list[LabelledMessage] | None = None
    messagesDeleted: list[LabelledMessage] | None = None
    labelsAdded: list[LabelledMessage] | None = None
    labelsRemoved: list[LabelledMessage] | None = None


class HistoryList(Model):
    history: list[HistoryRecord] | None = None
    nextPageToken: str | None = None
    historyId: str


# --------------------------------------------------------------------------- requests


class SendRequest(Model):
    raw: str | None = None
    threadId: str | None = None


class ModifyRequest(Model):
    addLabelIds: list[str] = []
    removeLabelIds: list[str] = []


# --------------------------------------------------------------------------- refusals


_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    410: "FAILED_PRECONDITION",
    429: "RESOURCE_EXHAUSTED",
    501: "UNIMPLEMENTED",
    503: "UNAVAILABLE",
}


def refusal(code: int, reason: str, message: str, *, status: str | None = None) -> wire.Refusal:
    """A Gmail or Calendar error: the classic envelope with one `errors` entry, and Google's status word."""
    item = wire.ErrorItem(domain="global", reason=reason, message=message)
    word = status or (_STATUS[code] if code in _STATUS else None)
    return wire.Refusal(wire.GoogleError(error=wire.ErrorBody(code=code, message=message, errors=[item], status=word)))


def invalid_argument(message: str) -> wire.Refusal:
    return refusal(400, "invalidArgument", message)


def not_found() -> wire.Refusal:
    return refusal(404, "notFound", "Requested entity was not found.")


def not_implemented(message: str) -> wire.Refusal:
    """Something real Gmail or Calendar does that this fake does not. Loud, and never mistaken for Google's own."""
    item = wire.ErrorItem(domain="minutehand", reason="notImplemented", message=message)
    return wire.Refusal(
        wire.GoogleError(error=wire.ErrorBody(code=501, message=message, errors=[item], status="UNIMPLEMENTED"))
    )


def fault(kind: wire.FaultKind) -> wire.Refusal:
    """A fault the scenario declared, in the shape Gmail and Calendar answer it."""
    if kind is wire.FaultKind.RATE_LIMITED:
        return refusal(429, "rateLimitExceeded", "Rate Limit Exceeded")
    if kind is wire.FaultKind.USER_RATE_LIMITED:
        return refusal(429, "userRateLimitExceeded", "User-rate limit exceeded.")
    if kind is wire.FaultKind.FORBIDDEN:
        return refusal(403, "forbidden", "Forbidden")
    if kind is wire.FaultKind.NOT_FOUND:
        return not_found()
    if kind is wire.FaultKind.UNAUTHENTICATED:
        return wire.invalid_credentials()
    if kind is wire.FaultKind.EXPIRED:
        return refusal(410, "fullSyncRequired", "Sync token is no longer valid, a full sync is required.")
    return refusal(503, "backendError", "Backend Error")


# --------------------------------------------------------------------------- RFC 2822


def encode(raw: bytes) -> str:
    """Gmail's `raw`: base64url, unpadded as Gmail answers it."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(text: str) -> bytes:
    """A base64url `raw` as clients send it, padded or not; standard base64 is read too, as Gmail reads it."""
    cleaned = text.strip().replace("+", "-").replace("/", "_")
    try:
        return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except (binascii.Error, ValueError) as error:
        raise invalid_argument("Invalid value for ByteString: " + text[:40]) from error


def parsed(raw: bytes) -> EmailMessage:
    found = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(found, EmailMessage)
    return found


def header(message: Message, name: str) -> str:
    found = message[name]
    return str(found) if found is not None else ""


def addresses(message: Message, *names: str) -> list[str]:
    """Every address the named headers carry, as written, in order and once each."""
    values = [str(v) for name in names for v in message.get_all(name, [])]
    found = [address.strip() for _, address in getaddresses(values) if "@" in address]
    return list(dict.fromkeys(found))


def plain_text(message: EmailMessage) -> str:
    """The text a reader reads: the plain-text body, else the HTML body with its tags dropped."""
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    content = body.get_content()
    text = content if isinstance(content, str) else ""
    if body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return text.strip()


_REPLY_PREFIX = re.compile(r"^\s*((re|fwd?|aw|wg)\s*:\s*)+", re.IGNORECASE)


def thread_subject(subject: str) -> str:
    """A subject as threading compares it: without its `Re:` and `Fwd:` prefixes, in any case."""
    return " ".join(_REPLY_PREFIX.sub("", subject).split()).casefold()


def snippet(text: str) -> str:
    """The start of the text, its whitespace folded and HTML-escaped, as Gmail's `snippet` is."""
    folded = " ".join(text.split())[:SNIPPET_LENGTH]
    return (
        folded.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def payload(message: Message, part_id: str = "", *, with_bodies: bool = True) -> MessagePart:
    """The MIME tree of a message as Gmail serves it in `payload`: each part's headers, its type and file name, and
    its decoded bytes as base64url in `body.data`. A multipart part has an empty body and its parts beneath."""
    headers = [Header(name=name, value=str(value)) for name, value in message.items()]
    children = message.get_payload() if message.is_multipart() else None
    if isinstance(children, list):
        parts = [
            payload(child, f"{part_id}.{n}" if part_id else str(n), with_bodies=with_bodies)
            for n, child in enumerate(children)
            if isinstance(child, Message)
        ]
        return MessagePart(
            partId=part_id,
            mimeType=message.get_content_type(),
            filename=message.get_filename() or "",
            headers=headers,
            body=PartBody(size=0),
            parts=parts,
        )
    decoded = message.get_payload(decode=True)
    content = decoded if isinstance(decoded, bytes) else b""
    return MessagePart(
        partId=part_id,
        mimeType=message.get_content_type(),
        filename=message.get_filename() or "",
        headers=headers,
        body=PartBody(size=len(content), data=encode(content) if with_bodies and content else None),
    )
