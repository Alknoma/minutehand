"""Google's own JSON for Drive v3, Docs v1 and the OAuth token endpoint: the only module that parses or builds it.

Four families of model live here:

- **Stored** — `StoredFile` (a Drive `File` resource and its content side by side),
  `DriveUser`, `Permission`, `Comment`: the body of each entity in the store, in the
  shape Drive serves it.
- **Requests** — the writable part of a file, a permission and a comment, read from
  a JSON body or the metadata part of a `multipart/related` upload. Drive ignores a
  body field it does not know, so `read_body` keeps only the fields a model declares.
- **Responses** — lists, `about`, the Docs `Document`, the token answer, and Google's
  error envelope.
- **Partial responses** — the `fields=` mask, parsed, checked against the model it
  selects from, and applied to the dumped JSON.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import types
from datetime import UTC, datetime
from email.message import Message
from typing import Annotated, Literal, TypeVar, Union, get_args, get_origin
from urllib.parse import parse_qsl

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

from minutehand.domain.scenario import Model

FOLDER = "application/vnd.google-apps.folder"
DOCUMENT = "application/vnd.google-apps.document"
GOOGLE_APPS = "application/vnd.google-apps."
OCTET_STREAM = "application/octet-stream"

MAX_CONTENT_BYTES = 5 * 1024 * 1024
"""This fake's limit on one file's content, not Drive's: content is stored inside the entity's row."""

EXPORT_LIMIT_BYTES = 10 * 1024 * 1024
"""Drive refuses to export a Docs file whose export would exceed this."""

BOM = "﻿"


# --------------------------------------------------------------------------- errors


class ErrorItem(Model):
    domain: str
    reason: str
    message: str
    location: str | None = None
    locationType: str | None = None


class ErrorBody(Model):
    code: int
    message: str
    errors: list[ErrorItem] | None = Field(default=None, description="Drive's classic detail; Docs v1 sends none")
    status: str | None = None


class GoogleError(Model):
    error: ErrorBody


class Refusal(Exception):
    """Google answered with an error. `answer` is the envelope it sent, `code` its HTTP status."""

    def __init__(self, answer: GoogleError, headers: dict[str, str] | None = None) -> None:
        super().__init__(answer.error.message)
        self.answer = answer
        self.headers = headers or {}

    @property
    def code(self) -> int:
        return self.answer.error.code


def drive_refusal(
    code: int,
    reason: str,
    message: str,
    *,
    location: str | None = None,
    location_type: str | None = None,
    domain: str = "global",
) -> Refusal:
    """A Drive v3 error: the classic envelope with one `errors` entry."""
    item = ErrorItem(domain=domain, reason=reason, message=message, location=location, locationType=location_type)
    return Refusal(GoogleError(error=ErrorBody(code=code, message=message, errors=[item])))


def not_found(file_id: str) -> Refusal:
    return drive_refusal(404, "notFound", f"File not found: {file_id}.", location="fileId", location_type="parameter")


def invalid(parameter: str, message: str | None = None) -> Refusal:
    return drive_refusal(400, "invalid", message or "Invalid Value", location=parameter, location_type="parameter")


def required(parameter: str, message: str | None = None) -> Refusal:
    return drive_refusal(
        400,
        "required",
        message or f"Required parameter: {parameter}",
        location=parameter,
        location_type="parameter",
    )


def fields_required() -> Refusal:
    return required("fields", "The 'fields' parameter is required for this method.")


def bad_request(message: str) -> Refusal:
    return drive_refusal(400, "badRequest", message)


def forbidden(reason: str, message: str) -> Refusal:
    return drive_refusal(403, reason, message)


def not_implemented(message: str) -> Refusal:
    """Something real Drive does that this fake does not. Loud, and never mistaken for Google's own answer."""
    return drive_refusal(501, "notImplemented", message, domain="minutehand")


def login_required() -> Refusal:
    item = ErrorItem(
        domain="global",
        reason="required",
        message="Login Required.",
        location="Authorization",
        locationType="header",
    )
    return Refusal(
        GoogleError(error=ErrorBody(code=401, message="Login Required.", errors=[item], status="UNAUTHENTICATED")),
        headers={"WWW-Authenticate": 'Bearer realm="https://accounts.google.com/"'},
    )


def docs_refusal(code: int, status: str, message: str) -> Refusal:
    """A Docs v1 error: code, message and status, and no `errors` list."""
    return Refusal(GoogleError(error=ErrorBody(code=code, message=message, status=status)))


def docs_login_required() -> Refusal:
    refusal = docs_refusal(
        401,
        "UNAUTHENTICATED",
        "Request is missing required authentication credential. Expected OAuth 2 access token, login cookie or"
        " other valid authentication credential.",
    )
    refusal.headers["WWW-Authenticate"] = 'Bearer realm="https://accounts.google.com/"'
    return refusal


def error_body(refusal: Refusal) -> bytes:
    return refusal.answer.model_dump_json(exclude_none=True).encode()


# --------------------------------------------------------------------------- stored


class DriveUser(Model):
    kind: Literal["drive#user"] = "drive#user"
    displayName: str
    emailAddress: str | None = None
    permissionId: str
    me: bool | None = Field(default=None, description="Set when served to say the caller is this user")


class DriveFile(Model):
    """A Drive v3 `File`, as far as this fake knows one."""

    kind: Literal["drive#file"] = "drive#file"
    id: str
    name: str
    mimeType: str
    description: str | None = None
    starred: bool = False
    trashed: bool = Field(default=False, description="Stored as set on this file; served true under a trashed folder")
    explicitlyTrashed: bool = False
    parents: list[str] | None = None
    owners: list[DriveUser] = []
    createdTime: str
    modifiedTime: str
    version: str
    size: str | None = Field(default=None, description="Bytes of a binary file; Docs editors files have none")
    md5Checksum: str | None = None
    webViewLink: str


class DocText(Model):
    """The text of a Google Docs file. It is the document: export and the Docs API both read it."""

    kind: Literal["doc_text"] = "doc_text"
    text: str


class Blob(Model):
    """The bytes of a binary file, base64-encoded so they survive as JSON text."""

    kind: Literal["blob"] = "blob"
    base64: str


Content = Annotated[DocText | Blob, Field(discriminator="kind")]


class StoredFile(Model):
    file: DriveFile
    content: Content | None = Field(default=None, description="None for a folder and an empty non-Docs file")


PermissionType = Literal["user", "group", "domain", "anyone"]
Role = Literal["owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"]
PERMISSION_TYPES: dict[str, PermissionType] = {"user": "user", "group": "group", "domain": "domain", "anyone": "anyone"}
ROLES: dict[str, Role] = {
    "owner": "owner",
    "organizer": "organizer",
    "fileOrganizer": "fileOrganizer",
    "writer": "writer",
    "commenter": "commenter",
    "reader": "reader",
}
"""Drive's closed vocabularies, keyed by how a request spells them. "editor" is the UI's word, not the API's."""


class Permission(Model):
    kind: Literal["drive#permission"] = "drive#permission"
    id: str
    type: PermissionType
    role: Role
    emailAddress: str | None = None
    domain: str | None = None
    displayName: str | None = None
    allowFileDiscovery: bool | None = None


class QuotedFileContent(Model):
    mimeType: str = "text/html"
    value: str


class Comment(Model):
    kind: Literal["drive#comment"] = "drive#comment"
    id: str
    createdTime: str
    modifiedTime: str
    author: DriveUser
    htmlContent: str
    content: str
    deleted: bool = False
    resolved: bool = False
    quotedFileContent: QuotedFileContent | None = None
    anchor: str | None = None
    replies: list[JsonValue] = []


Stored = TypeVar("Stored", StoredFile, DriveUser, Permission, Comment)


def parse(model: type[Stored], body: str) -> Stored:
    return model.model_validate_json(body)


def dump(entity: Model) -> str:
    return entity.model_dump_json(exclude_none=True)


def blob(content: bytes) -> Blob:
    return Blob(base64=base64.b64encode(content).decode())


def blob_bytes(stored: Blob) -> bytes:
    return base64.b64decode(stored.base64)


def md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def rfc3339(moment: datetime) -> str:
    """Drive's timestamp: UTC, milliseconds, `Z`."""
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{utc.microsecond // 1000:03d}Z"


def moment(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def web_view_link(file_id: str, mime_type: str) -> str:
    if mime_type == FOLDER:
        return f"https://drive.google.com/drive/folders/{file_id}"
    if mime_type == DOCUMENT:
        return f"https://docs.google.com/document/d/{file_id}/edit"
    return f"https://drive.google.com/file/d/{file_id}/view?usp=drivesdk"


# --------------------------------------------------------------------------- requests


class FileWrite(Model):
    """The file fields a create, an update or a copy may set."""

    name: str | None = None
    mimeType: str | None = None
    description: str | None = None
    starred: bool | None = None
    trashed: bool | None = None
    parents: list[str] | None = None
    id: str | None = None


NOT_WRITABLE = frozenset(
    {
        "kind",
        "owners",
        "size",
        "md5Checksum",
        "version",
        "webViewLink",
        "explicitlyTrashed",
        "createdTime",
        "modifiedTime",
    }
)
"""Fields Drive serves and refuses in a request body with 403 `fieldNotWritable`.

`createdTime` and `modifiedTime` ARE writable on real Drive; this fake stamps every time from
the run's clock, so it refuses them rather than ignore them."""


class PermissionWrite(Model):
    type: str = ""
    role: str = ""
    emailAddress: str | None = None
    domain: str | None = None
    allowFileDiscovery: bool | None = None


class CommentWrite(Model):
    content: str = ""
    anchor: str | None = None
    quotedFileContent: QuotedFileContent | None = None


Body = TypeVar("Body", bound=Model)


def read_object(raw: bytes) -> dict[str, JsonValue]:
    """A JSON request body that must be an object; an empty body is an empty object."""
    if not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise drive_refusal(400, "parseError", "Parse Error") from error
    if not isinstance(decoded, dict):
        raise drive_refusal(400, "parseError", "Parse Error")
    return decoded


def read_body(model: type[Body], found: dict[str, JsonValue]) -> Body:
    """The fields `model` declares; anything else is ignored, as Drive ignores it."""
    known = {name: value for name, value in found.items() if name in model.model_fields}
    try:
        return model.model_validate(known)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise drive_refusal(400, "invalid", f"Invalid value for: {where}", location=where) from error


def unwritable(found: dict[str, JsonValue], *, also: frozenset[str] = frozenset()) -> str | None:
    """The first field in a request body that Drive does not let a caller set."""
    return next((name for name in found if name in NOT_WRITABLE or name in also), None)


class Upload(Model):
    """What a `multipart/related` upload carried: the file's metadata and its media."""

    metadata: dict[str, JsonValue]
    media: bytes
    media_type: str


def header_value(content_type: str) -> tuple[str, dict[str, str]]:
    """A Content-Type's media type, lower-cased, and its parameters."""
    message = Message()
    message["content-type"] = content_type
    params = {key.lower(): str(value) for key, value in message.get_params(failobj=[])[1:]}
    return message.get_content_type(), params


def read_multipart(content_type: str, raw: bytes) -> Upload:
    """Split a `multipart/related` body into its JSON metadata part and its media part.

    googleapiclient writes the parts with bare `\\n` line ends; other clients use `\\r\\n`.
    Both are read, and the media's bytes are kept exactly.
    """
    media_type, params = header_value(content_type)
    boundary = params.get("boundary")
    if media_type != "multipart/related" or not boundary:
        raise bad_request("Content-Type of a multipart upload must be multipart/related with a boundary.")
    delimiter = b"--" + boundary.encode()
    pieces = raw.split(delimiter)
    parts: list[tuple[str, bytes]] = []
    for piece in pieces[1:]:
        if piece.startswith(b"--"):
            break
        piece = piece[2:] if piece.startswith(b"\r\n") else piece[1:] if piece.startswith(b"\n") else piece
        for gap in (b"\r\n\r\n", b"\n\n"):
            if gap in piece:
                head, body = piece.split(gap, 1)
                break
        else:
            raise bad_request("A part of the multipart body has no headers.")
        body = body[:-2] if body.endswith(b"\r\n") else body[:-1] if body.endswith(b"\n") else body
        part_type = OCTET_STREAM
        for line in head.decode("latin-1").splitlines():
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-type":
                part_type = header_value(value.strip())[0]
        parts.append((part_type, body))
    if len(parts) != 2:
        raise bad_request(f"A multipart upload has two parts, metadata then media; this one has {len(parts)}.")
    (metadata_type, metadata), (part_type, media) = parts
    if metadata_type != "application/json":
        raise bad_request("The first part of a multipart upload must be application/json metadata.")
    return Upload(metadata=read_object(metadata), media=media, media_type=part_type)


class CallQuery(Model):
    """Every query parameter this fake reads, from any method. Drive ignores the ones a method does not take."""

    fields: str | None = None
    alt: str | None = None
    access_token: str | None = None
    q: str = ""
    pageSize: str | None = None
    pageToken: str | None = None
    orderBy: str | None = None
    mimeType: str | None = None
    addParents: str = ""
    removeParents: str = ""
    uploadType: str | None = None
    transferOwnership: str = "false"


def read_query(query: str) -> CallQuery:
    found = dict(parse_qsl(query, keep_blank_values=True))
    return CallQuery.model_validate({name: value for name, value in found.items() if name in CallQuery.model_fields})


def read_form(raw: bytes) -> dict[str, str]:
    return dict(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True))


def id_list(spelled: str) -> list[str]:
    """`addParents` and `removeParents`: comma-separated ids."""
    return [part.strip() for part in spelled.split(",") if part.strip()]


# --------------------------------------------------------------------------- token endpoint


class TokenRequest(Model):
    grant_type: str = ""
    assertion: str | None = None
    refresh_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None


JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
REFRESH_TOKEN = "refresh_token"


class TokenAnswer(Model):
    access_token: str
    expires_in: int = 3599
    token_type: Literal["Bearer"] = "Bearer"
    scope: str | None = None


class OAuthError(Model):
    """The token endpoint's own error shape (RFC 6749), not the API envelope."""

    error: str
    error_description: str


def read_token_request(form: dict[str, str]) -> TokenRequest:
    known = {name: value for name, value in form.items() if name in TokenRequest.model_fields}
    return TokenRequest.model_validate(known)


# --------------------------------------------------------------------------- responses


class FileList(Model):
    kind: Literal["drive#fileList"] = "drive#fileList"
    nextPageToken: str | None = None
    incompleteSearch: bool = False
    files: list[DriveFile]


class PermissionList(Model):
    kind: Literal["drive#permissionList"] = "drive#permissionList"
    nextPageToken: str | None = None
    permissions: list[Permission]


class CommentList(Model):
    kind: Literal["drive#commentList"] = "drive#commentList"
    nextPageToken: str | None = None
    comments: list[Comment]


class About(Model):
    kind: Literal["drive#about"] = "drive#about"
    user: DriveUser


def encode_page(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode().rstrip("=")


def decode_page(token: str | None) -> int:
    if not token:
        return 0
    try:
        text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError) as error:
        raise invalid("pageToken") from error
    head, _, offset = text.partition(":")
    if head != "offset" or not offset.isdigit():
        raise invalid("pageToken")
    return int(offset)


def page_size(raw: str | None, *, default: int, most: int) -> int:
    if raw is None:
        return default
    if not raw.lstrip("-").isdigit():
        raise invalid("pageSize")
    size = int(raw)
    if size < 1 or size > most:
        raise invalid("pageSize", f"Invalid value '{raw}'. Values must be within the range: [1, {most}]")
    return size


# --------------------------------------------------------------------------- docs v1


class TextStyle(Model):
    pass


class TextRun(Model):
    content: str
    textStyle: TextStyle = TextStyle()


class ParagraphElement(Model):
    startIndex: int
    endIndex: int
    textRun: TextRun


class ParagraphStyle(Model):
    namedStyleType: Literal["NORMAL_TEXT"] = "NORMAL_TEXT"
    direction: Literal["LEFT_TO_RIGHT"] = "LEFT_TO_RIGHT"


class Paragraph(Model):
    elements: list[ParagraphElement]
    paragraphStyle: ParagraphStyle = ParagraphStyle()


class SectionStyle(Model):
    columnSeparatorStyle: Literal["NONE"] = "NONE"
    contentDirection: Literal["LEFT_TO_RIGHT"] = "LEFT_TO_RIGHT"
    sectionType: Literal["CONTINUOUS"] = "CONTINUOUS"


class SectionBreak(Model):
    sectionStyle: SectionStyle = SectionStyle()


class StructuralElement(Model):
    startIndex: int | None = Field(default=None, description="Absent on the opening section break, as Docs sends it")
    endIndex: int
    paragraph: Paragraph | None = None
    sectionBreak: SectionBreak | None = None


class DocumentBody(Model):
    content: list[StructuralElement]


class Document(Model):
    documentId: str
    title: str
    revisionId: str
    body: DocumentBody


def utf16_units(text: str) -> int:
    """A Docs index counts UTF-16 code units: an emoji is two."""
    return len(text.encode("utf-16-le")) // 2


def document(document_id: str, title: str, revision: str, text: str) -> Document:
    """A Docs body for plain text: a section break at 0, then one paragraph per line, each with one text run.

    A document always ends in a newline, so text without one gets one.
    """
    if not text.endswith("\n"):
        text += "\n"
    content = [StructuralElement(endIndex=1, sectionBreak=SectionBreak())]
    index = 1
    for line in text.splitlines(keepends=True):
        end = index + utf16_units(line)
        run = ParagraphElement(startIndex=index, endIndex=end, textRun=TextRun(content=line))
        content.append(StructuralElement(startIndex=index, endIndex=end, paragraph=Paragraph(elements=[run])))
        index = end
    return Document(documentId=document_id, title=title, revisionId=revision, body=DocumentBody(content=content))


def plain_text_export(text: str) -> bytes:
    """A `text/plain` export as Drive sends it: a byte-order mark, then the text with CRLF line ends."""
    if not text.endswith("\n"):
        text += "\n"
    return (BOM + text.replace("\r\n", "\n").replace("\n", "\r\n")).encode("utf-8")


# --------------------------------------------------------------------------- partial responses


type Mask = dict[str, Mask | None]
"""A `fields=` selection: each key selected, with its own sub-selection or None for all of it."""


def parse_mask(text: str) -> Mask | None:
    """`fields=` as Google writes it: `a,b/c,files(id,owners(emailAddress))`. None means `*`, everything."""
    text = text.strip()
    if not text or text == "*":
        return None
    mask, at = _selection(text, 0)
    if at != len(text):
        raise _bad_mask(text)
    return mask


def _bad_mask(selection: str) -> Refusal:
    return drive_refusal(
        400,
        "invalidParameter",
        f"Invalid field selection {selection}",
        location="fields",
        location_type="parameter",
    )


def _selection(text: str, at: int) -> tuple[Mask, int]:
    mask: Mask = {}
    while True:
        path: list[str] = []
        while True:
            start = at
            while at < len(text) and (text[at].isalnum() or text[at] in "_*"):
                at += 1
            if at == start:
                raise _bad_mask(text)
            path.append(text[start:at])
            if at < len(text) and text[at] == "/":
                at += 1
                continue
            break
        sub: Mask | None = None
        if at < len(text) and text[at] == "(":
            sub, at = _selection(text, at + 1)
            if at >= len(text) or text[at] != ")":
                raise _bad_mask(text)
            at += 1
        _merge(mask, path, sub)
        while at < len(text) and text[at] == " ":
            at += 1
        if at < len(text) and text[at] == ",":
            at += 1
            while at < len(text) and text[at] == " ":
                at += 1
            continue
        return mask, at


def _merge(mask: Mask, path: list[str], sub: Mask | None) -> None:
    """Add one selected path; selecting all of a field absorbs any narrower selection of it."""
    head, rest = path[0], path[1:]
    value: Mask | None = sub
    for name in reversed(rest):
        value = {name: value}
    if head not in mask:
        mask[head] = value
        return
    existing = mask[head]
    if existing is None or value is None:
        mask[head] = None
        return
    for name, nested in value.items():
        _merge(existing, [name], nested)


def _nested_model(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin in (list, Union, types.UnionType, Annotated):
        for argument in get_args(annotation):
            found = _nested_model(argument)
            if found is not None:
                return found
    return None


def check_mask(mask: Mask | None, model: type[BaseModel]) -> None:
    """Refuse a selection naming a field the resource does not have, as Google does."""
    if mask is None:
        return
    for name, sub in mask.items():
        if name == "*":
            continue
        if name not in model.model_fields:
            raise _bad_mask(name)
        if sub is not None:
            nested = _nested_model(model.model_fields[name].annotation)
            if nested is None:
                raise _bad_mask(name)
            check_mask(sub, nested)


def _apply(value: JsonValue, mask: Mask | None) -> JsonValue:
    if mask is None or "*" in mask:
        return value
    if isinstance(value, list):
        return [_apply(item, mask) for item in value]
    if isinstance(value, dict):
        return {key: _apply(item, mask[key]) for key, item in value.items() if key in mask}
    return value


_JSON = TypeAdapter(JsonValue)


def respond(answer: Model, mask: Mask | None) -> bytes:
    """The answer as JSON, cut down to the selection."""
    dumped: JsonValue = answer.model_dump(mode="json", exclude_none=True)
    return _JSON.dump_json(_apply(dumped, mask))


def selection(fields: str | None, model: type[BaseModel], default: str) -> Mask | None:
    """The selection a request asked for, or the method's default when it named none."""
    mask = parse_mask(fields if fields is not None else default)
    check_mask(mask, model)
    return mask


FILE_DEFAULT = "kind,id,name,mimeType"
LIST_DEFAULT = "kind,nextPageToken,incompleteSearch,files(kind,id,name,mimeType)"
PERMISSION_DEFAULT = "kind,id,type,role"
PERMISSION_LIST_DEFAULT = "kind,nextPageToken,permissions(kind,id,type,role)"
