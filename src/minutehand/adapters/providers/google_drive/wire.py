"""Google's own JSON for Drive v3 and Google's sign-in endpoints. Docs v1 is `docs.py`, Slides v1 `slides.py`.

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

from minutehand.adapters.providers.google_drive.docs import DocBody
from minutehand.adapters.providers.google_drive.slides import Deck
from minutehand.domain.scenario import Model

FOLDER = "application/vnd.google-apps.folder"
DOCUMENT = "application/vnd.google-apps.document"
SPREADSHEET = "application/vnd.google-apps.spreadsheet"
PRESENTATION = "application/vnd.google-apps.presentation"
SHORTCUT = "application/vnd.google-apps.shortcut"
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


def invalid_credentials() -> Refusal:
    """A token Google does not accept: unknown, expired or revoked."""
    message = (
        "Request had invalid authentication credentials. Expected OAuth 2 access token, login cookie or other"
        " valid authentication credential. See https://developers.google.com/identity/sign-in/web/devconsole-project."
    )
    item = ErrorItem(
        domain="global",
        reason="authError",
        message="Invalid Credentials",
        location="Authorization",
        locationType="header",
    )
    return Refusal(
        GoogleError(error=ErrorBody(code=401, message=message, errors=[item], status="UNAUTHENTICATED")),
        headers={"WWW-Authenticate": 'Bearer realm="https://accounts.google.com/", error="invalid_token"'},
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


class ShortcutDetails(Model):
    targetId: str
    targetMimeType: str | None = None


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
    driveId: str | None = Field(default=None, description="The shared drive it is in; None in a My Drive")
    lastModifyingUser: DriveUser | None = None
    shared: bool | None = None
    shortcutDetails: ShortcutDetails | None = None


class BlobRef(Model):
    """A binary file's bytes, kept once as their own entity under their digest: a change to the file's metadata
    does not copy them, and two files with the same bytes share them."""

    kind: Literal["blob"] = "blob"
    digest: str
    size: int
    md5: str


class Blob(Model):
    """The bytes of a binary file, base64-encoded so they survive as JSON text: the body of a `BlobRef`'s entity."""

    base64: str


class Sheet(Model):
    """A Google Sheets file: the cells of its one sheet, row by row."""

    kind: Literal["sheet"] = "sheet"
    rows: list[list[str]] = []


Content = Annotated[DocBody | Deck | Sheet | BlobRef, Field(discriminator="kind")]


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


class SharedDrive(Model):
    kind: Literal["drive#drive"] = "drive#drive"
    id: str
    name: str
    createdTime: str
    hidden: bool = False


class Credential(Model):
    """A refresh token or a service account the run signs in, and whom it signs in as. Kept under its digest;
    the credential itself is never stored."""

    email: str = Field(description="Who a token issued for it acts as")
    service_account: bool = False
    revoked: bool = False


class AccessToken(Model):
    """An access token the run issued, kept under its digest."""

    email: str
    expires: str = Field(description="RFC 3339, simulated time")
    credential: str = Field(description="The digest of the credential it was issued for")
    revoked: bool = False


class Channel(Model):
    """A `changes.watch` subscription: where to tell the agent of changes, and what it has been told."""

    id: str
    resourceId: str
    resourceUri: str
    address: str
    expiration: str = Field(description="RFC 3339, simulated time")
    token: str | None = None
    email: str
    driveId: str | None = None
    told_after: int = Field(description="The last event seq the agent has been told of")
    messages: int = Field(default=1, description="Notifications sent, the sync message first")
    stopped: bool = False
    undelivered: int = Field(default=0, description="Notifications the agent's address did not accept")


class StoredFault(Model):
    operation: str
    kind: str
    after: str = Field(description="RFC 3339, simulated time")
    remaining: int


class SeededFile(Model):
    """The file a seeded document became, found by its title however it is renamed later."""

    file_id: str


class UploadSession(Model):
    """A resumable upload in progress: the metadata it began with and the bytes received so far."""

    metadata: str = Field(description="The JSON body of the first request")
    media_type: str
    total: int | None
    received: str = Field(default="", description="base64")
    file_id: str | None = Field(default=None, description="Set when the upload replaces a file's content")
    email: str
    query: str = Field(description="The first request's query string, which says what to answer")


Stored = TypeVar(
    "Stored",
    StoredFile,
    DriveUser,
    Permission,
    Comment,
    SharedDrive,
    Credential,
    AccessToken,
    Channel,
    StoredFault,
    UploadSession,
    Blob,
    SeededFile,
)


def parse(model: type[Stored], body: str) -> Stored:
    return model.model_validate_json(body)


def dump(entity: Model) -> str:
    return entity.model_dump_json(exclude_none=True)


def blob(content: bytes) -> Blob:
    return Blob(base64=base64.b64encode(content).decode())


def blob_bytes(stored: Blob) -> bytes:
    return base64.b64decode(stored.base64)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


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
        return f"https://docs.google.com/document/d/{file_id}/edit?usp=drivesdk"
    if mime_type == SPREADSHEET:
        return f"https://docs.google.com/spreadsheets/d/{file_id}/edit?usp=drivesdk"
    if mime_type == PRESENTATION:
        return f"https://docs.google.com/presentation/d/{file_id}/edit?usp=drivesdk"
    return f"https://drive.google.com/file/d/{file_id}/view?usp=drivesdk"


def rfc1123(moment: datetime) -> str:
    """The date format of Google's push notification headers."""
    return moment.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")


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
    upload_id: str | None = None
    transferOwnership: str = "false"
    sendNotificationEmail: str = "true"
    emailMessage: str | None = None
    corpora: str | None = None
    driveId: str | None = None
    supportsAllDrives: str = "false"
    supportsTeamDrives: str = "false"
    includeItemsFromAllDrives: str = "false"
    includeTeamDriveItems: str = "false"
    spaces: str = "drive"
    includeRemoved: str = "true"
    restrictToMyDrive: str = "false"
    includeTabsContent: str = "false"
    token: str | None = None

    @property
    def all_drives(self) -> bool:
        """Whether the caller says it supports shared drives (`supportsTeamDrives` is the old spelling)."""
        return self.supportsAllDrives == "true" or self.supportsTeamDrives == "true"

    @property
    def items_from_all_drives(self) -> bool:
        return self.includeItemsFromAllDrives == "true" or self.includeTeamDriveItems == "true"


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
    code: str | None = None
    redirect_uri: str | None = None
    code_verifier: str | None = None


class JwtClaims(Model):
    """The claims of a service account's signed assertion that say who it is, read without the signature: the run
    holds no Google key to check it against."""

    iss: str
    scope: str | None = None
    sub: str | None = None
    aud: str | None = None
    exp: int | None = None
    iat: int | None = None
    target_audience: str | None = None


def jwt_claims(assertion: str) -> JwtClaims | None:
    """The claims of a JWT, or None when it is not one."""
    parts = assertion.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        decoded = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    known = {name: value for name, value in decoded.items() if name in JwtClaims.model_fields}
    try:
        return JwtClaims.model_validate(known)
    except ValidationError:
        return None


JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
REFRESH_TOKEN = "refresh_token"
AUTHORIZATION_CODE = "authorization_code"
TOKEN_LIFETIME = 3599


class TokenAnswer(Model):
    access_token: str
    expires_in: int = TOKEN_LIFETIME
    token_type: Literal["Bearer"] = "Bearer"
    scope: str | None = None


class Userinfo(Model):
    """`oauth2/v2/userinfo`."""

    id: str
    email: str
    verified_email: bool = True
    name: str
    given_name: str | None = None
    family_name: str | None = None
    picture: str | None = None


class AllowedLocations(Model):
    """`iamcredentials`' regional access boundary lookup, which google-auth makes after a service account signs in:
    no boundary."""

    locations: list[str] = []
    encodedLocations: str = "0x0"


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


class DriveList(Model):
    kind: Literal["drive#driveList"] = "drive#driveList"
    nextPageToken: str | None = None
    drives: list[SharedDrive]


class StartPageToken(Model):
    kind: Literal["drive#startPageToken"] = "drive#startPageToken"
    startPageToken: str


class Change(Model):
    kind: Literal["drive#change"] = "drive#change"
    changeType: Literal["file"] = "file"
    time: str
    removed: bool
    fileId: str
    file: DriveFile | None = None
    driveId: str | None = None


class ChangeList(Model):
    kind: Literal["drive#changeList"] = "drive#changeList"
    nextPageToken: str | None = None
    newStartPageToken: str | None = None
    changes: list[Change]


class ChannelWrite(Model):
    id: str = ""
    type: str = ""
    address: str = ""
    expiration: str | None = Field(default=None, description="Milliseconds since the epoch, as a string")
    token: str | None = None
    params: dict[str, str] | None = None


class ChannelAnswer(Model):
    kind: Literal["api#channel"] = "api#channel"
    id: str
    resourceId: str
    resourceUri: str
    expiration: str
    token: str | None = None


class ChannelStop(Model):
    id: str = ""
    resourceId: str = ""


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


# --------------------------------------------------------------------------- exports


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


FILE_DEFAULT = "kind,id,name,mimeType,driveId"
LIST_DEFAULT = "kind,nextPageToken,incompleteSearch,files(kind,id,name,mimeType,driveId)"
CHANGE_LIST_DEFAULT = "kind,nextPageToken,newStartPageToken,changes(kind,changeType,time,removed,fileId,file(kind,id,name,mimeType,driveId),driveId)"
DRIVE_LIST_DEFAULT = "kind,nextPageToken,drives(kind,id,name)"
DRIVE_DEFAULT = "kind,id,name"
PERMISSION_DEFAULT = "kind,id,type,role"
PERMISSION_LIST_DEFAULT = "kind,nextPageToken,permissions(kind,id,type,role)"
