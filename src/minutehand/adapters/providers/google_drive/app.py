"""Drive v3, Docs v1, Slides v1 and Google's sign-in endpoints, as one ASGI app over the run's store and clock.

**Hosts.** A request whose `Host` is one of the manifest's hosts is answered only by that host's routes:
`/token` on `www.googleapis.com` is a 404, as it is at Google. Any other host (a client reaching the
provider directly, without the proxy) is answered by every route, which is safe because the hosts' paths do
not overlap.

**Sign-in.** `POST /token` answers the refresh-token grant and the service account's JWT-bearer grant for the
credentials the scenario names (`SignIn`), or, when it names none, for any credential, signing in as the
scenario's owner. A refresh token is matched as given; a service account by the `iss` (and, impersonating,
`sub`) of its assertion, whose signature is not checked: the run holds no Google key. The access token
issued identifies its user for an hour of simulated time, so a token used after a clock jump gets Google's
401 and the client's refresh path runs. `POST /revoke` revokes a token and the grant it came from.

**Errors** are Google's: Drive's classic envelope (`code`, `message`, one `errors` entry with `domain` and
`reason`), and Docs' and Slides' (`code`, `message`, `status`). A real behaviour this fake does not
reproduce answers 501 (`notImplemented` in domain `minutehand` for Drive, `UNIMPLEMENTED` for Docs and
Slides), never a made-up success. A fault Drive's own seed declares answers its refusal in the same shapes.

**Content.** A binary file's bytes are kept once as their own entity, up to `wire.MAX_CONTENT_BYTES`
(5 MiB) per file; a larger upload is refused with 413 `uploadTooLarge`, a limit of this fake's, not Drive's.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import html
import io
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import JsonValue, ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, Router
from starlette.types import Receive, Scope, Send

from minutehand.adapters.providers.google_drive import docs, slides, state, wire
from minutehand.adapters.providers.google_drive import query as drive_query
from minutehand.adapters.providers.google_drive.state import ROLE_RANK, ROOT_ALIAS, DriveWorld
from minutehand.domain.scenario import DocumentHappening, Edited, Model, Moved, Renamed, Shared
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

DRIVE_HOST = "www.googleapis.com"
OAUTH_HOST = "oauth2.googleapis.com"
DOCS_HOST = "docs.googleapis.com"
SLIDES_HOST = "slides.googleapis.com"
IAM_HOST = "iamcredentials.googleapis.com"

JSON = "application/json; charset=UTF-8"
CONVERTS_TO_DOC = frozenset({"text/plain", "text/markdown"})
"""Media this fake converts into a Google Doc. Drive converts more (HTML, Word); those are refused loudly."""
EXPORTS_NOT_BUILT = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "application/rtf",
        "text/html",
        "application/zip",
        "application/epub+zip",
    }
)
"""Formats real Drive exports Docs editors files to and this fake does not render."""
EXPORTS: dict[str, frozenset[str]] = {
    wire.DOCUMENT: frozenset({"text/plain", "text/markdown"}),
    wire.SPREADSHEET: frozenset({"text/csv", "text/tab-separated-values"}),
    wire.PRESENTATION: frozenset({"text/plain"}),
}
ORDER_KEYS = frozenset({"name", "createdTime", "modifiedTime", "folder", "starred"})
ORDER_KEYS_NOT_BUILT = frozenset(
    {"name_natural", "quotaBytesUsed", "recency", "sharedWithMeTime", "viewedByMeTime", "modifiedByMeTime"}
)
CHANNEL_DEFAULT = timedelta(hours=1)
CHANNEL_LONGEST = timedelta(days=7)
"""A `changes.watch` channel lives an hour unless it asks for longer, and a week at most. An expiration already
past on the run's clock (an agent that reads the machine's clock in a run set earlier or later) is taken as
none asked for, and a later one is cut to the week."""


class Api(StrEnum):
    """Which of Google's error shapes a route answers in."""

    DRIVE = "drive"
    DOCS = "docs"
    SLIDES = "slides"
    OAUTH = "oauth"
    USERINFO = "userinfo"


Handler = Callable[[Request], Awaitable[Response]]


class Caller(Model):
    email: str
    user: wire.DriveUser


def _json(answer: Model, mask: wire.Mask | None = None, status: int = 200) -> Response:
    return Response(wire.respond(answer, mask), status_code=status, media_type=JSON)


def _refused(refusal: wire.Refusal) -> Response:
    return Response(wire.error_body(refusal), status_code=refusal.code, media_type=JSON, headers=refusal.headers)


def _bearer(request: Request, call: wire.CallQuery) -> str | None:
    authorization = request.headers["authorization"] if "authorization" in request.headers else ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return call.access_token or None


def _status_refusal(code: int, status: str, message: str) -> wire.Refusal:
    return wire.docs_refusal(code, status, message)


def _fault_refusal(api: Api, kind: wire.FaultKind, file_id: str | None) -> wire.Refusal:
    """A declared fault, in the shape the route's API answers it."""
    if api is Api.DRIVE:
        if kind is wire.FaultKind.RATE_LIMITED:
            return wire.drive_refusal(403, "rateLimitExceeded", "Rate Limit Exceeded", domain="usageLimits")
        if kind is wire.FaultKind.USER_RATE_LIMITED:
            return wire.drive_refusal(403, "userRateLimitExceeded", "User Rate Limit Exceeded", domain="usageLimits")
        if kind is wire.FaultKind.FORBIDDEN:
            return wire.forbidden(
                "insufficientFilePermissions", "The user does not have sufficient permissions for this file."
            )
        if kind is wire.FaultKind.NOT_FOUND:
            return wire.not_found(file_id or "")
        if kind is wire.FaultKind.UNAUTHENTICATED:
            return wire.invalid_credentials()
        if kind is wire.FaultKind.EXPIRED:
            return wire.drive_refusal(410, "expired", "The page token is no longer valid.", location="pageToken")
        return wire.drive_refusal(503, "backendError", "Backend Error")
    if kind in (wire.FaultKind.RATE_LIMITED, wire.FaultKind.USER_RATE_LIMITED):
        return _status_refusal(
            429,
            "RESOURCE_EXHAUSTED",
            "Quota exceeded for quota metric 'Read requests' and limit 'Read requests per minute per user'.",
        )
    if kind is wire.FaultKind.FORBIDDEN:
        return _status_refusal(403, "PERMISSION_DENIED", "The caller does not have permission")
    if kind is wire.FaultKind.NOT_FOUND:
        return _status_refusal(404, "NOT_FOUND", "Requested entity was not found.")
    if kind is wire.FaultKind.UNAUTHENTICATED:
        return _status_refusal(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
    if kind is wire.FaultKind.EXPIRED:
        return _status_refusal(400, "FAILED_PRECONDITION", "The requested revision is no longer available.")
    return _status_refusal(503, "UNAVAILABLE", "The service is currently unavailable.")


OPERATIONS = frozenset(
    {
        "files.list",
        "files.get",
        "files.create",
        "files.update",
        "files.delete",
        "files.copy",
        "files.export",
        "permissions.list",
        "permissions.create",
        "permissions.delete",
        "comments.list",
        "comments.create",
        "about.get",
        "drives.list",
        "drives.get",
        "changes.getStartPageToken",
        "changes.list",
        "changes.watch",
        "channels.stop",
        "documents.get",
        "documents.create",
        "documents.batchUpdate",
        "presentations.get",
        "presentations.create",
        "presentations.batchUpdate",
        "userinfo.get",
        "token",
        "revoke",
    }
)
"""Every call a `Fault` may name: Google's own method names, without the API's prefix."""


class DriveApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._drive = DriveWorld(store)
        self._clock = clock
        self._background: set[asyncio.Task[bool]] = set()

    @property
    def drive(self) -> DriveWorld:
        return self._drive

    # ------------------------------------------------------------------ the gate every call passes

    def guarded(
        self,
        handler: Callable[[Request, wire.CallQuery, Caller], Awaitable[Response]],
        api: Api,
        operation: str,
    ) -> Handler:
        """Read the query, know the caller by their token, play any fault due, and answer refusals in Google's
        shape for `api`."""

        async def endpoint(request: Request) -> Response:
            try:
                try:
                    call = wire.read_query(request.url.query)
                    caller = self._caller(_bearer(request, call), api)
                    self._fault(operation, api, request)
                    return await handler(request, call, caller)
                except docs.Refused as refused:
                    raise _status_refusal(refused.code, refused.status, refused.message) from refused
                except slides.Refused as refused:
                    raise _status_refusal(refused.code, refused.status, refused.message) from refused
            except wire.Refusal as refusal:
                if api in (Api.DOCS, Api.SLIDES, Api.USERINFO) and refusal.answer.error.status is None:
                    refusal = _status_refusal(refusal.code, _STATUS[refusal.code], refusal.answer.error.message)
                return _refused(refusal)

        return endpoint

    def _caller(self, token: str | None, api: Api) -> Caller:
        if token is None:
            raise wire.login_required() if api is Api.DRIVE else wire.docs_login_required()
        issued = self._drive.token(token)
        if issued is None or issued.revoked or wire.moment(issued.expires) <= self._clock.now():
            raise wire.invalid_credentials()
        user = self._drive.user(issued.email)
        if user is None:
            raise wire.invalid_credentials()
        return Caller(email=issued.email, user=user)

    def _fault(self, operation: str, api: Api, request: Request) -> None:
        now = self._clock.now()
        for key, fault in self._drive.faults():
            if fault.operation != operation or fault.remaining < 1 or wire.moment(fault.after) > now:
                continue
            self._drive.keep_fault(key, fault.model_copy(update={"remaining": fault.remaining - 1}))
            ids = [v for k, v in request.path_params.items() if isinstance(v, str) and k.endswith("_id")]
            raise _fault_refusal(api, fault.kind, ids[0] if ids else None)

    # ------------------------------------------------------------------ lookups

    def _now(self) -> str:
        return wire.rfc3339(self._clock.now())

    def _root(self, caller: Caller) -> str:
        return state.root_id(caller.email)

    def _resolve(self, file_id: str, caller: Caller) -> str:
        return self._root(caller) if file_id == ROOT_ALIAS else file_id

    def _file(
        self,
        file_id: str,
        caller: Caller,
        call: wire.CallQuery,
        *,
        need: str = "reader",
        all_drives: bool | None = None,
    ) -> wire.StoredFile:
        """A file the caller may see, refused as Drive refuses: 404 when it is not theirs to see, or is in a
        shared drive and the call does not say it supports them; 403 when they may see it and not do this."""
        resolved = self._resolve(file_id, caller)
        stored = self._drive.file(resolved)
        if stored is None:
            raise wire.not_found(file_id)
        supports = call.all_drives if all_drives is None else all_drives
        if stored.file.driveId is not None and not supports:
            raise wire.not_found(file_id)
        role = self._drive.role(caller.email, stored)
        if role is None:
            raise wire.not_found(file_id)
        if ROLE_RANK[role] < ROLE_RANK[need]:
            raise wire.forbidden(
                "insufficientFilePermissions", "The user does not have sufficient permissions for this file."
            )
        return stored

    def _is_root(self, stored: wire.StoredFile) -> bool:
        return stored.file.parents is None

    def _served(self, stored: wire.StoredFile, *, trashed_above: bool | None = None) -> wire.DriveFile:
        above = self._drive.trashed(stored) if trashed_above is None else trashed_above or stored.file.trashed
        return stored.file if above == stored.file.trashed else stored.file.model_copy(update={"trashed": above})

    def _folder(self, folder_id: str, caller: Caller, call: wire.CallQuery) -> wire.StoredFile:
        """A folder the caller may put a file in."""
        folder = self._file(folder_id, caller, call, need="writer")
        if folder.file.mimeType != wire.FOLDER:
            raise wire.drive_refusal(
                400,
                "invalid",
                f"The specified parent is not a folder: {folder_id}.",
                location="parents",
                location_type="other",
            )
        return folder

    def _parent(self, requested: list[str] | None, caller: Caller, call: wire.CallQuery) -> wire.StoredFile:
        """The one parent of a new file: the folder asked for, or the caller's My Drive."""
        if not requested:
            root = self._drive.file(self._root(caller))
            if root is None:
                raise wire.not_found(ROOT_ALIAS)
            return root
        if len(requested) > 1:
            raise wire.forbidden("cannotAddParent", "Increasing the number of parents is not allowed.")
        return self._folder(requested[0], caller, call)

    # ------------------------------------------------------------------ files.list

    def _roots_for(self, caller: Caller, call: wire.CallQuery) -> list[str]:
        corpora = call.corpora or ("drive" if call.driveId else "user")
        if corpora == "domain":
            raise wire.not_implemented("searching a whole domain (corpora=domain)")
        if corpora not in ("user", "drive", "allDrives"):
            raise wire.invalid("corpora")
        if (corpora == "drive") != (call.driveId is not None):
            raise wire.drive_refusal(
                400,
                "invalid",
                "The driveId parameter must be specified if and only if corpora is set to drive.",
                location="driveId",
                location_type="parameter",
            )
        if corpora != "user" and not call.items_from_all_drives:
            raise wire.drive_refusal(
                403,
                "teamDriveIncludeItemsRequired",
                f"The includeItemsFromAllDrives parameter must be set to true when corpora is set to {corpora}.",
                location="includeItemsFromAllDrives",
                location_type="parameter",
            )
        if call.driveId is not None:
            self._member(call.driveId, caller)
            return [call.driveId]
        mine = [state.root_id(u.emailAddress) for u in self._drive.users() if u.emailAddress]
        shared = [d.id for d in self._drive.drives()] if call.items_from_all_drives else []
        return [r for r in [*mine, *shared] if self._drive.file(r) is not None]

    def _member(self, drive_id: str, caller: Caller) -> wire.SharedDrive:
        drive = self._drive.drive(drive_id)
        root = self._drive.file(drive_id)
        if drive is None or root is None or self._drive.role(caller.email, root) is None:
            raise wire.drive_refusal(
                404, "notFound", f"Shared drive not found: {drive_id}", location="driveId", location_type="parameter"
            )
        return drive

    async def files_list(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        if call.spaces != "drive":
            if "appDataFolder" in call.spaces.split(","):
                raise wire.not_implemented("the appDataFolder space")
            raise wire.invalid("spaces")
        try:
            parsed = drive_query.parse(call.q)
        except drive_query.QueryError as error:
            raise wire.invalid("q", f"Invalid Value: {error}") from error
        except drive_query.QueryNotSupported as error:
            raise wire.not_implemented(str(error)) from error
        parsed = drive_query.resolved(parsed, ROOT_ALIAS, self._root(caller))
        size = wire.page_size(call.pageSize, default=100, most=1000)
        offset = wire.decode_page(call.pageToken)
        mask = wire.selection(call.fields, wire.FileList, wire.LIST_DEFAULT)
        order = _order(call.orderBy)
        matched: list[wire.DriveFile] = []
        for root in self._roots_for(caller, call):
            for stored, trashed_above in self._drive.walk(root):
                if self._drive.role(caller.email, stored, searching=True) is None:
                    continue
                served = self._served(stored, trashed_above=trashed_above)
                if drive_query.matches(parsed, self._candidate(stored, served)):
                    matched.append(served)
        for key, descending in reversed(order):
            matched.sort(key=_sort_key(key), reverse=descending)
        page = matched[offset : offset + size]
        more = offset + size < len(matched)
        self._drive.saw(state.file_ref(self._root(caller)), Operation.SEARCH)
        return _json(wire.FileList(files=page, nextPageToken=wire.encode_page(offset + size) if more else None), mask)

    def _candidate(self, stored: wire.StoredFile, served: wire.DriveFile) -> drive_query.Candidate:
        return drive_query.Candidate(
            name=served.name,
            mime_type=served.mimeType,
            parents=served.parents or [],
            trashed=served.trashed,
            starred=served.starred,
            created=wire.moment(served.createdTime),
            modified=wire.moment(served.modifiedTime),
            full_text="\n".join([served.name, served.description or "", self._text(stored)]),
        )

    def _text(self, stored: wire.StoredFile) -> str:
        content = stored.content
        blob = self._drive.blob(content) if isinstance(content, wire.BlobRef) else None
        return state.readable_text(stored, blob)

    # ------------------------------------------------------------------ files.get, export

    async def files_get(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._file(request.path_params["file_id"], caller, call)
        if call.alt == "media":
            if stored.file.mimeType.startswith(wire.GOOGLE_APPS):
                raise wire.forbidden(
                    "fileNotDownloadable",
                    "Only files with binary content can be downloaded. Use Export with Docs Editors files.",
                )
            content = self._drive.blob(stored.content) if isinstance(stored.content, wire.BlobRef) else b""
            self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
            return Response(content, media_type=stored.file.mimeType)
        mask = wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT)
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return _json(self._served(stored), mask)

    async def files_export(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        if not call.mimeType:
            raise wire.required("mimeType")
        stored = self._file(request.path_params["file_id"], caller, call, all_drives=True)
        source = stored.file.mimeType
        if source not in EXPORTS:
            if source == wire.FOLDER or not source.startswith(wire.GOOGLE_APPS):
                raise wire.forbidden("fileNotExportable", "Export only supports Docs Editors files.")
            raise wire.not_implemented(f"exporting {source}")
        if call.mimeType not in EXPORTS[source]:
            if call.mimeType in EXPORTS_NOT_BUILT:
                raise wire.not_implemented(f"this simulation does not render {source} as {call.mimeType}")
            raise wire.drive_refusal(
                400,
                "badRequest",
                "The requested conversion is not supported.",
                location="convertTo",
                location_type="parameter",
            )
        exported = _exported(stored.content, call.mimeType)
        if len(exported) > wire.EXPORT_LIMIT_BYTES:
            raise wire.forbidden("exportSizeLimitExceeded", "This file is too large to be exported.")
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return Response(exported, media_type=call.mimeType)

    # ------------------------------------------------------------------ files.create, uploads

    async def files_create(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        found = wire.read_object(await request.body())
        stored = self._created(found, caller, call, media=None, media_type=None)
        return _json(stored.file, wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    async def upload_create(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        if call.uploadType == "resumable":
            return await self._begin_resumable(request, call, caller, None)
        found, media, media_type = await self._upload(request, call)
        stored = self._created(found, caller, call, media=media, media_type=media_type)
        return _json(stored.file, wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    async def _upload(self, request: Request, call: wire.CallQuery) -> tuple[dict[str, JsonValue], bytes, str]:
        raw = await request.body()
        content_type = request.headers["content-type"] if "content-type" in request.headers else wire.OCTET_STREAM
        if call.uploadType == "multipart":
            upload = wire.read_multipart(content_type, raw)
            found, media, media_type = upload.metadata, upload.media, upload.media_type
        elif call.uploadType == "media":
            found, media, media_type = {}, raw, wire.header_value(content_type)[0]
        else:
            raise wire.invalid("uploadType")
        _check_size(len(media))
        return found, media, media_type

    async def _begin_resumable(
        self, request: Request, call: wire.CallQuery, caller: Caller, file_id: str | None
    ) -> Response:
        """The first request of a resumable upload: the metadata, and where to send the bytes."""
        raw = await request.body()
        wire.read_object(raw)
        media_type = request.headers["x-upload-content-type"] if "x-upload-content-type" in request.headers else None
        length = request.headers["x-upload-content-length"] if "x-upload-content-length" in request.headers else None
        if length is not None and (not length.isdigit()):
            raise wire.invalid("X-Upload-Content-Length")
        total = int(length) if length is not None else None
        if total is not None:
            _check_size(total)
        if file_id is not None:
            self._file(file_id, caller, call, need="writer")
        upload_id = "AU" + hashlib.sha256(f"upload\x1f{self._drive.next_seq()}".encode()).hexdigest()[:40]
        query = "&".join(f"{k}={v}" for k, v in request.query_params.items() if k not in ("uploadType", "upload_id"))
        session = wire.UploadSession(
            metadata=raw.decode("utf-8") or "{}",
            media_type=media_type or wire.OCTET_STREAM,
            total=total,
            file_id=file_id,
            email=caller.email,
            query=query,
        )
        self._drive.keep_upload(upload_id, session, operation=Operation.CREATE)
        path = request.url.path
        location = f"{request.base_url.scheme}://{request.url.netloc}{path}?uploadType=resumable&upload_id={upload_id}"
        return Response(b"", status_code=200, headers={"Location": location, "X-GUploader-UploadID": upload_id})

    async def upload_put(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        """A chunk of a resumable upload, or a question about how much has arrived (`bytes */total`)."""
        upload_id = call.upload_id
        session = self._drive.upload(upload_id) if upload_id else None
        if upload_id is None or session is None or session.email != caller.email:
            raise wire.drive_refusal(404, "notFound", "The upload session was not found.", location="upload_id")
        received = wire.blob_bytes(wire.Blob(base64=session.received))
        raw = await request.body()
        spelled = request.headers["content-range"] if "content-range" in request.headers else None
        first, total = _content_range(spelled, len(raw), session.total)
        if first is not None and first != len(received):
            raise wire.drive_refusal(
                400, "badContent", f"The chunk starts at {first}; {len(received)} bytes have arrived."
            )
        received += raw
        _check_size(len(received))
        known_total = total if total is not None else session.total
        if known_total is None or len(received) < known_total:
            self._drive.keep_upload(
                upload_id, session.model_copy(update={"received": wire.blob(received).base64, "total": known_total})
            )
            headers = {"Range": f"bytes=0-{len(received) - 1}"} if received else {}
            return Response(b"", status_code=308, headers=headers)
        if len(received) > known_total:
            raise wire.drive_refusal(400, "badContent", "More bytes arrived than the upload said it would carry.")
        self._drive.end_upload(upload_id)
        first_call = wire.read_query(session.query)
        found = wire.read_object(session.metadata.encode())
        if session.file_id is None:
            stored = self._created(found, caller, first_call, media=received, media_type=session.media_type)
        else:
            target = self._file(session.file_id, caller, first_call, need="writer")
            content = self._content(target.file.mimeType, received, session.media_type, caller)
            stored = self._updated(target, found, first_call, caller, content=content)
        return _json(self._served(stored), wire.selection(first_call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    def _created(
        self,
        found: dict[str, JsonValue],
        caller: Caller,
        call: wire.CallQuery,
        *,
        media: bytes | None,
        media_type: str | None,
    ) -> wire.StoredFile:
        if (refused := wire.unwritable(found)) is not None:
            raise _not_writable(refused)
        meta = wire.read_body(wire.FileWrite, found)
        if meta.id is not None:
            raise wire.drive_refusal(400, "fileIdNotUsable", "The provided file ID is not usable.", location="file.id")
        mime = meta.mimeType or media_type or wire.OCTET_STREAM
        parent = self._parent(meta.parents, caller, call)
        seq = self._drive.next_seq()
        new_id = state.file_id(seq)
        content = (
            self._content(mime, media, media_type, caller, document_id=new_id)
            if media is not None
            else _empty(mime, new_id)
        )
        now = self._now()
        trashed = bool(meta.trashed)
        size, checksum = _measured(content)
        drive_id = parent.file.driveId
        stored = wire.StoredFile(
            file=wire.DriveFile(
                id=new_id,
                name=meta.name or "Untitled",
                mimeType=mime,
                description=meta.description,
                starred=bool(meta.starred),
                trashed=trashed,
                explicitlyTrashed=trashed,
                parents=[parent.file.id],
                owners=None if drive_id else [caller.user],
                createdTime=now,
                modifiedTime=now,
                version=str(seq),
                webViewLink=wire.web_view_link(new_id, mime),
                size=size,
                md5Checksum=checksum,
                driveId=drive_id,
                lastModifyingUser=caller.user,
            ),
            content=content,
        )
        self._drive.write_file(stored, operation=Operation.CREATE, actor=Actor.AGENT)
        return stored

    def _content(
        self, target: str, media: bytes | None, media_type: str | None, caller: Caller, *, document_id: str = ""
    ) -> wire.Content:
        """Uploaded media as the file will hold it: converted into a Doc or a sheet, or kept as bytes."""
        media = media or b""
        if target in (wire.DOCUMENT, wire.SPREADSHEET):
            try:
                text = media.decode("utf-8").removeprefix(wire.BOM)
            except UnicodeDecodeError as error:
                raise wire.bad_request("The media is not UTF-8 text.") from error
            if target == wire.SPREADSHEET:
                if media_type not in ("text/csv", "text/tab-separated-values"):
                    raise wire.not_implemented(f"converting {media_type} into a Google Sheet; upload text/csv")
                delimiter = "," if media_type == "text/csv" else "\t"
                return wire.Sheet(rows=[list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)])
            if media_type not in CONVERTS_TO_DOC:
                raise wire.not_implemented(
                    f"converting {media_type} into a Google Doc; upload text/plain or text/markdown"
                )
            return docs.from_markdown(document_id, text) if media_type == "text/markdown" else docs.from_text(text)
        if target == wire.FOLDER:
            raise wire.bad_request("A folder has no content.")
        if target.startswith(wire.GOOGLE_APPS):
            raise wire.not_implemented(f"converting media into {target}")
        return self._drive.keep_blob(media, actor=Actor.AGENT)

    # ------------------------------------------------------------------ files.update

    async def files_update(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._file(request.path_params["file_id"], caller, call, need="writer")
        found = wire.read_object(await request.body())
        updated = self._updated(stored, found, call, caller, content=None)
        return _json(self._served(updated), wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    async def upload_update(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        if call.uploadType == "resumable":
            return await self._begin_resumable(request, call, caller, request.path_params["file_id"])
        stored = self._file(request.path_params["file_id"], caller, call, need="writer")
        found, media, media_type = await self._upload(request, call)
        content = self._content(stored.file.mimeType, media, media_type, caller, document_id=stored.file.id)
        updated = self._updated(stored, found, call, caller, content=content)
        return _json(self._served(updated), wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    def _updated(
        self,
        stored: wire.StoredFile,
        found: dict[str, JsonValue],
        call: wire.CallQuery,
        caller: Caller,
        *,
        content: wire.Content | None,
    ) -> wire.StoredFile:
        if self._is_root(stored):
            raise wire.forbidden(
                "insufficientFilePermissions", "The user does not have sufficient permissions for this file."
            )
        if (refused := wire.unwritable(found, also=frozenset({"parents", "id"}))) is not None:
            raise _not_writable(refused)
        meta = wire.read_body(wire.FileWrite, found)
        mime = stored.file.mimeType
        if meta.mimeType is not None and meta.mimeType != mime:
            if mime.startswith(wire.GOOGLE_APPS) or meta.mimeType.startswith(wire.GOOGLE_APPS):
                raise wire.bad_request("A Docs Editors file's type cannot be changed, nor a file made one.")
            mime = meta.mimeType
        if meta.trashed is not None and meta.trashed != stored.file.trashed:
            self._may_remove(stored, caller, trash=True)
        seq = self._drive.next_seq()
        changes: dict[str, object] = {
            "mimeType": mime,
            "parents": self._moved(stored, call, caller),
            "modifiedTime": self._now(),
            "version": str(seq),
            "lastModifyingUser": caller.user,
        }
        if meta.name is not None:
            changes["name"] = meta.name
        if meta.description is not None:
            changes["description"] = meta.description
        if meta.starred is not None:
            changes["starred"] = meta.starred
        if meta.trashed is not None:
            changes["trashed"] = meta.trashed
            changes["explicitlyTrashed"] = meta.trashed
        kept = stored.content if content is None else content
        if content is not None:
            changes["size"], changes["md5Checksum"] = _measured(content)
        updated = wire.StoredFile(file=stored.file.model_copy(update=changes), content=kept)
        self._drive.write_file(updated, operation=Operation.UPDATE, actor=Actor.AGENT)
        return updated

    def _may_remove(self, stored: wire.StoredFile, caller: Caller, *, trash: bool) -> None:
        """Only a My Drive file's owner trashes or deletes it; in a shared drive, a file organizer trashes and an
        organizer deletes."""
        role = self._drive.role(caller.email, stored)
        if stored.file.driveId is None:
            allowed = role == "owner"
        else:
            allowed = role is not None and ROLE_RANK[role] >= ROLE_RANK["fileOrganizer" if trash else "organizer"]
        if not allowed:
            raise wire.forbidden(
                "insufficientFilePermissions", "The user does not have sufficient permissions for this file."
            )

    def _moved(self, stored: wire.StoredFile, call: wire.CallQuery, caller: Caller) -> list[str]:
        """The file's parents after `addParents` and `removeParents`: still exactly one folder."""
        current = list(stored.file.parents or [])
        added = wire.id_list(call.addParents)
        removed = {self._resolve(p, caller) for p in wire.id_list(call.removeParents)}
        if not added and not removed:
            return current
        parents = [p for p in current if p not in removed]
        for folder_id in added:
            folder = self._folder(folder_id, caller, call)
            if folder.file.id == stored.file.id or any(
                a.file.id == stored.file.id for a in self._drive.ancestors(folder)
            ):
                raise wire.drive_refusal(
                    400,
                    "invalid",
                    "A folder cannot be moved into itself or a folder inside it.",
                    location="addParents",
                    location_type="parameter",
                )
            if folder.file.driveId != stored.file.driveId:
                raise wire.not_implemented(
                    "moving a file between My Drive and a shared drive, or between shared drives"
                )
            if folder.file.id not in parents:
                parents.append(folder.file.id)
        if len(parents) > 1:
            if stored.file.driveId is not None:
                raise wire.forbidden("teamDrivesParentLimit", "A shared drive item must have exactly one parent.")
            raise wire.forbidden("cannotAddParent", "Increasing the number of parents is not allowed.")
        if not parents:
            raise wire.not_implemented("a file left with no parent; move it with addParents and removeParents together")
        return parents

    # ------------------------------------------------------------------ files.delete, files.copy

    async def files_delete(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._file(request.path_params["file_id"], caller, call, need="writer")
        if self._is_root(stored):
            raise wire.forbidden(
                "insufficientFilePermissions", "The user does not have sufficient permissions for this file."
            )
        self._may_remove(stored, caller, trash=False)
        self._drive.delete_file(stored, actor=Actor.AGENT)
        return Response(status_code=204)

    async def files_copy(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        source = self._file(request.path_params["file_id"], caller, call)
        if source.file.mimeType == wire.FOLDER:
            raise wire.forbidden("cannotCopyFile", "This file cannot be copied by the user.")
        found = wire.read_object(await request.body())
        if (refused := wire.unwritable(found)) is not None:
            raise _not_writable(refused)
        meta = wire.read_body(wire.FileWrite, found)
        if meta.mimeType is not None and meta.mimeType != source.file.mimeType:
            raise wire.not_implemented("converting a file while copying it")
        requested = meta.parents
        if not requested and source.file.parents:
            above = self._drive.file(source.file.parents[0])
            writable = above is not None and self._drive.role(caller.email, above) in (
                "writer",
                "owner",
                "organizer",
                "fileOrganizer",
            )
            requested = source.file.parents if writable else None
        parent = self._parent(requested, caller, call)
        seq = self._drive.next_seq()
        new_id = state.file_id(seq)
        now = self._now()
        copied = wire.StoredFile(
            file=source.file.model_copy(
                update={
                    "id": new_id,
                    "name": meta.name or f"Copy of {source.file.name}",
                    "parents": [parent.file.id],
                    "description": meta.description if meta.description is not None else source.file.description,
                    "starred": bool(meta.starred),
                    "trashed": False,
                    "explicitlyTrashed": False,
                    "owners": None if parent.file.driveId else [caller.user],
                    "driveId": parent.file.driveId,
                    "createdTime": now,
                    "modifiedTime": now,
                    "version": str(seq),
                    "webViewLink": wire.web_view_link(new_id, source.file.mimeType),
                    "lastModifyingUser": caller.user,
                    "shared": None,
                }
            ),
            content=source.content,
        )
        self._drive.write_file(copied, operation=Operation.CREATE, actor=Actor.AGENT)
        return _json(copied.file, wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    # ------------------------------------------------------------------ permissions

    def _permissions(self, stored: wire.StoredFile) -> list[wire.Permission]:
        """The owner, then what was granted on the file, then what it inherits from the folders above it."""
        found: dict[str, wire.Permission] = {}
        for owner in stored.file.owners or []:
            found.setdefault(
                owner.permissionId,
                wire.Permission(
                    id=owner.permissionId,
                    type="user",
                    role="owner",
                    emailAddress=owner.emailAddress,
                    displayName=owner.displayName,
                ),
            )
        for holder in [stored, *self._drive.ancestors(stored)]:
            for permission in self._drive.grants(holder.file.id):
                found.setdefault(permission.id, permission)
        return list(found.values())

    async def permissions_list(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._file(request.path_params["file_id"], caller, call)
        size = wire.page_size(call.pageSize, default=100, most=100)
        offset = wire.decode_page(call.pageToken)
        mask = wire.selection(call.fields, wire.PermissionList, wire.PERMISSION_LIST_DEFAULT)
        every = self._permissions(stored)
        more = offset + size < len(every)
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return _json(
            wire.PermissionList(
                permissions=every[offset : offset + size],
                nextPageToken=wire.encode_page(offset + size) if more else None,
            ),
            mask,
        )

    async def permissions_create(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._file(request.path_params["file_id"], caller, call, need="writer")
        asked = wire.read_body(wire.PermissionWrite, wire.read_object(await request.body()))
        permission = self._permission(asked, transfer=call.transferOwnership == "true")
        mask = wire.selection(call.fields, wire.Permission, wire.PERMISSION_DEFAULT)
        self.grant(stored, permission, actor=Actor.AGENT)
        return _json(permission, mask)

    def grant(self, stored: wire.StoredFile, permission: wire.Permission, *, actor: Actor) -> None:
        """Record a grant, and mark the file shared: a new version of it, which is a change in the feed."""
        existing = any(p.id == permission.id for p in self._drive.grants(stored.file.id))
        self._drive.write_grant(
            stored.file.id,
            permission,
            operation=Operation.UPDATE if existing else Operation.CREATE,
            actor=actor,
        )
        seq = self._drive.next_seq()
        shared = stored.file.model_copy(update={"shared": True, "version": str(seq)})
        self._drive.write_file(
            wire.StoredFile(file=shared, content=stored.content), operation=Operation.UPDATE, actor=actor
        )

    async def permissions_delete(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._file(request.path_params["file_id"], caller, call, need="writer")
        permission_id = request.path_params["permission_id"]
        if any(owner.permissionId == permission_id for owner in stored.file.owners or []):
            raise wire.forbidden("cannotRemoveOwner", "The owner of a file cannot be removed.")
        if not any(p.id == permission_id for p in self._drive.grants(stored.file.id)):
            raise wire.drive_refusal(
                404,
                "notFound",
                f"Permission not found: {permission_id}.",
                location="permissionId",
                location_type="parameter",
            )
        self._drive.delete_grant(stored.file.id, permission_id, actor=Actor.AGENT)
        return Response(status_code=204)

    def _permission(self, asked: wire.PermissionWrite, *, transfer: bool) -> wire.Permission:
        if asked.type not in wire.PERMISSION_TYPES:
            raise wire.drive_refusal(
                400,
                "invalid",
                f"Invalid value for PermissionType: {asked.type}",
                location="permission.type",
                location_type="other",
            )
        if asked.role not in wire.ROLES:
            raise wire.drive_refusal(
                400,
                "invalid",
                f"Invalid value for Role: {asked.role}",
                location="permission.role",
                location_type="other",
            )
        kind, role = wire.PERMISSION_TYPES[asked.type], wire.ROLES[asked.role]
        if kind in ("user", "group") and not asked.emailAddress:
            raise _sharing_refused("A permission of type user or group needs an emailAddress.")
        if kind in ("anyone", "domain") and asked.emailAddress:
            raise _sharing_refused(f"A permission of type {kind} has no emailAddress.")
        if kind == "domain" and not asked.domain:
            raise _sharing_refused("A permission of type domain needs a domain.")
        if role == "owner":
            if not transfer:
                raise wire.forbidden(
                    "forbidden",
                    "The transferOwnership parameter must be enabled when the permission role is 'owner'.",
                )
            raise wire.not_implemented("transferring ownership")
        if asked.emailAddress:
            person = self._drive.user(asked.emailAddress)
            return wire.Permission(
                id=state.permission_id(asked.emailAddress),
                type=kind,
                role=role,
                emailAddress=asked.emailAddress,
                displayName=person.displayName if person is not None else None,
            )
        if kind == "domain" and asked.domain:
            return wire.Permission(
                id=state.domain_permission_id(asked.domain),
                type=kind,
                role=role,
                domain=asked.domain,
                allowFileDiscovery=bool(asked.allowFileDiscovery),
            )
        return wire.Permission(
            id=state.ANYONE_PERMISSION_ID,
            type=kind,
            role=role,
            allowFileDiscovery=bool(asked.allowFileDiscovery),
        )

    # ------------------------------------------------------------------ comments

    async def comments_list(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        if call.fields is None:
            raise wire.fields_required()
        stored = self._file(request.path_params["file_id"], caller, call)
        size = wire.page_size(call.pageSize, default=20, most=100)
        offset = wire.decode_page(call.pageToken)
        mask = wire.selection(call.fields, wire.CommentList, call.fields)
        every = self._drive.comments(stored.file.id)
        more = offset + size < len(every)
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return _json(
            wire.CommentList(
                comments=every[offset : offset + size],
                nextPageToken=wire.encode_page(offset + size) if more else None,
            ),
            mask,
        )

    async def comments_create(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        if call.fields is None:
            raise wire.fields_required()
        stored = self._file(request.path_params["file_id"], caller, call, need="commenter")
        mask = wire.selection(call.fields, wire.Comment, call.fields)
        asked = wire.read_body(wire.CommentWrite, wire.read_object(await request.body()))
        if not asked.content.strip():
            raise wire.required("content", "Required: content")
        now = self._now()
        comment = wire.Comment(
            id=state.comment_id(self._drive.next_seq()),
            createdTime=now,
            modifiedTime=now,
            author=wire.DriveUser(displayName=caller.user.displayName, permissionId=caller.user.permissionId, me=True),
            htmlContent=html.escape(asked.content),
            content=asked.content,
            anchor=asked.anchor,
            quotedFileContent=asked.quotedFileContent,
        )
        self._drive.write_comment(stored.file.id, comment, actor=Actor.AGENT)
        return _json(comment, mask)

    # ------------------------------------------------------------------ about, drives, userinfo

    async def about(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        if call.fields is None:
            raise wire.fields_required()
        mask = wire.selection(call.fields, wire.About, call.fields)
        self._drive.saw(state.user_ref(caller.user.permissionId), Operation.READ)
        return _json(wire.About(user=caller.user.model_copy(update={"me": True})), mask)

    async def drives_list(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        if call.q:
            raise wire.not_implemented("searching shared drives with q")
        size = wire.page_size(call.pageSize, default=10, most=100)
        offset = wire.decode_page(call.pageToken)
        mask = wire.selection(call.fields, wire.DriveList, wire.DRIVE_LIST_DEFAULT)
        mine = []
        for drive in self._drive.drives():
            root = self._drive.file(drive.id)
            if root is not None and self._drive.role(caller.email, root) is not None:
                mine.append(drive)
        more = offset + size < len(mine)
        self._drive.saw(state.file_ref(self._root(caller)), Operation.SEARCH)
        page = mine[offset : offset + size]
        return _json(wire.DriveList(drives=page, nextPageToken=wire.encode_page(offset + size) if more else None), mask)

    async def drives_get(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        drive = self._member(request.path_params["drive_id"], caller)
        self._drive.saw(state.file_ref(drive.id), Operation.READ)
        return _json(drive, wire.selection(call.fields, wire.SharedDrive, wire.DRIVE_DEFAULT))

    async def userinfo(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        given, _, family = caller.user.displayName.partition(" ")
        answer = wire.Userinfo(
            id=str(int(caller.user.permissionId)),
            email=caller.email,
            name=caller.user.displayName,
            given_name=given or None,
            family_name=family or None,
        )
        return _json(answer)

    # ------------------------------------------------------------------ changes and channels

    def feed(
        self, email: str, drive_id: str | None, *, after: int, include_all_drives: bool, include_removed: bool = True
    ) -> list[tuple[int, wire.Change]]:
        """Every file that changed after event `after`, seen by `email`, once each at its latest change, oldest
        first. A file deleted, or no longer theirs to see, is `removed` with no file."""
        latest: dict[str, int] = {}
        roots = set(self._drive.roots())
        for event in self._drive.store.events(since=after):
            entity = event.entity
            if entity.provider != state.MANIFEST.key or entity.kind is not state.file_ref("").kind:
                continue
            if event.operation not in (Operation.CREATE, Operation.UPDATE, Operation.DELETE):
                continue
            if entity.external_id in roots:
                continue
            latest[entity.external_id] = event.seq
        found: list[tuple[int, wire.Change]] = []
        for file_id, seq in sorted(latest.items(), key=lambda item: item[1]):
            current = self._drive.file(file_id)
            earlier = self._drive.file_as_of(file_id, seq)
            subject = current or earlier
            if subject is None:
                continue
            in_drive = subject.file.driveId
            if drive_id is not None and in_drive != drive_id:
                continue
            if drive_id is None and in_drive is not None and not include_all_drives:
                continue
            sees_now = current is not None and self._drive.role(email, current) is not None
            saw_before = earlier is not None and self._drive.role(email, earlier) is not None
            if not sees_now and not saw_before:
                continue
            stamp = (current or subject).file.modifiedTime
            if sees_now and current is not None:
                change = wire.Change(
                    time=stamp, removed=False, fileId=file_id, file=self._served(current), driveId=in_drive
                )
            elif include_removed:
                change = wire.Change(time=stamp, removed=True, fileId=file_id, driveId=in_drive)
            else:
                continue
            found.append((seq, change))
        return found

    def _start_token(self) -> str:
        return str(self._drive.store.head() + 1)

    def _page_token(self, spelled: str | None) -> int:
        if not spelled:
            raise wire.required("pageToken")
        if not spelled.isdigit() or not 1 <= int(spelled) <= self._drive.store.head() + 1:
            raise wire.invalid("pageToken")
        return int(spelled)

    async def changes_start(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        if call.driveId is not None:
            self._member(call.driveId, caller)
        token = self._start_token()
        self._drive.saw(state.file_ref(call.driveId or self._root(caller)), Operation.READ)
        return _json(wire.StartPageToken(startPageToken=token))

    async def changes_list(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        token = self._page_token(call.pageToken)
        if call.driveId is not None:
            self._member(call.driveId, caller)
        size = wire.page_size(call.pageSize, default=100, most=1000)
        mask = wire.selection(call.fields, wire.ChangeList, wire.CHANGE_LIST_DEFAULT)
        every = self.feed(
            caller.email,
            call.driveId,
            after=token - 1,
            include_all_drives=call.items_from_all_drives,
            include_removed=call.includeRemoved != "false",
        )
        start = self._start_token()
        page = every[:size]
        rest = every[size:]
        answer = wire.ChangeList(
            changes=[change for _, change in page],
            nextPageToken=str(rest[0][0]) if rest else None,
            newStartPageToken=None if rest else start,
        )
        self._drive.saw(state.file_ref(call.driveId or self._root(caller)), Operation.SEARCH)
        return _json(answer, mask)

    async def changes_watch(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        token = self._page_token(call.pageToken)
        if call.driveId is not None:
            self._member(call.driveId, caller)
        asked = wire.read_body(wire.ChannelWrite, wire.read_object(await request.body()))
        if not asked.id:
            raise wire.required("channel.id", "Required: channel.id")
        if asked.type not in ("web_hook", "webhook"):
            raise wire.drive_refusal(
                400, "push.channelTypeNotSupported", f"Channel type '{asked.type}' is not supported.", domain="push"
            )
        if not asked.address.startswith(("https://", "http://")):
            raise wire.drive_refusal(
                400,
                "push.webhookUrlUnauthorized",
                f"Unauthorized WebHook callback channel: {asked.address}",
                domain="push",
            )
        if self._drive.channel(asked.id) is not None:
            raise wire.drive_refusal(400, "channelIdNotUnique", f"Channel id {asked.id} not unique", domain="push")
        now = self._clock.now()
        if asked.expiration is not None and not asked.expiration.isdigit():
            raise wire.invalid("channel.expiration")
        wanted = (
            datetime.fromtimestamp(int(asked.expiration) / 1000, tz=now.tzinfo)
            if asked.expiration is not None
            else now + CHANNEL_DEFAULT
        )
        if wanted <= now:
            wanted = now + CHANNEL_DEFAULT
        expires = min(wanted, now + CHANNEL_LONGEST)
        resource_id = hashlib.sha256(f"resource\x1f{asked.id}\x1f{token}".encode()).hexdigest()[:27]
        uri = f"https://{DRIVE_HOST}/drive/v3/changes?alt=json&pageToken={token}"
        if call.driveId is not None:
            uri += f"&driveId={call.driveId}&includeItemsFromAllDrives=true&supportsAllDrives=true"
        channel = wire.Channel(
            id=asked.id,
            resourceId=resource_id,
            resourceUri=uri,
            address=asked.address,
            expiration=wire.rfc3339(expires),
            token=asked.token,
            email=caller.email,
            driveId=call.driveId,
            told_after=token - 1,
        )
        self._drive.keep_channel(channel, operation=Operation.CREATE)
        sync = asyncio.create_task(self._push(channel, "sync", 1))
        self._background.add(sync)
        sync.add_done_callback(self._background.discard)
        answer = wire.ChannelAnswer(
            id=channel.id,
            resourceId=resource_id,
            resourceUri=uri,
            expiration=str(int(expires.timestamp() * 1000)),
            token=asked.token,
        )
        return _json(answer)

    async def channels_stop(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        asked = wire.read_body(wire.ChannelStop, wire.read_object(await request.body()))
        channel = self._drive.channel(asked.id)
        if channel is None or channel.resourceId != asked.resourceId or channel.stopped:
            raise wire.drive_refusal(
                404, "notFound", f"Channel '{asked.id}' not found for project 'minutehand'", domain="global"
            )
        self._drive.keep_channel(channel.model_copy(update={"stopped": True}))
        return Response(status_code=204)

    def watching(self) -> list[wire.Channel]:
        now = self._clock.now()
        return [c for c in self._drive.channels() if not c.stopped and wire.moment(c.expiration) > now]

    async def notify(self) -> None:
        """Tell every live channel's address of the changes its user has not been told of, as Drive does: an
        empty POST whose `X-Goog-*` headers name the channel; the agent reads the changes itself."""
        head = self._drive.store.head()
        for channel in self.watching():
            changed = self.feed(channel.email, channel.driveId, after=channel.told_after, include_all_drives=True)
            if not changed:
                continue
            number = channel.messages + 1
            delivered = await self._push(channel, "change", number)
            self._drive.keep_channel(
                channel.model_copy(
                    update={
                        "told_after": head,
                        "messages": number,
                        "undelivered": channel.undelivered + (0 if delivered else 1),
                    }
                )
            )

    async def _push(self, channel: wire.Channel, resource_state: str, number: int) -> bool:
        headers = {
            "X-Goog-Channel-ID": channel.id,
            "X-Goog-Channel-Expiration": wire.rfc1123(wire.moment(channel.expiration)),
            "X-Goog-Resource-State": resource_state,
            "X-Goog-Message-Number": str(number),
            "X-Goog-Resource-ID": channel.resourceId,
            "X-Goog-Resource-URI": channel.resourceUri,
            "Content-Length": "0",
        }
        if channel.token is not None:
            headers["X-Goog-Channel-Token"] = channel.token
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
                answered = await client.post(channel.address, headers=headers)
        except httpx.HTTPError:
            return False
        return answered.is_success

    # ------------------------------------------------------------------ what people do without the agent

    def person_change(self, change: DocumentHappening) -> None:
        """A person changes a seeded document, as actor PERSON. A document the agent has deleted is not there
        to change, and the change does not land."""
        person = self._drive.person(change.person)
        file_id = self._drive.seeded(change.document)
        stored = self._drive.file(file_id) if file_id is not None else None
        if person is None or stored is None:
            return
        action = change.action
        if isinstance(action, Shared):
            target = self._drive.person(action.access.person)
            assert target is not None and target.emailAddress is not None
            permission = wire.Permission(
                id=target.permissionId,
                type="user",
                role=state.ROLES[action.access.role],
                emailAddress=target.emailAddress,
                displayName=target.displayName,
            )
            self.grant(stored, permission, actor=Actor.PERSON)
            return
        update: dict[str, object] = {}
        content = stored.content
        if isinstance(action, Edited):
            content = self._appended(stored, action.append)
            update["size"], update["md5Checksum"] = _measured(content)
        elif isinstance(action, Renamed):
            update["name"] = action.to
        elif isinstance(action, Moved):
            root = ([stored, *self._drive.ancestors(stored)])[-1]
            folder = state.ensure_folder(self._drive, root, action.folder, person, self._now(), Actor.PERSON)
            update["parents"] = [folder.file.id]
        else:
            update["trashed"] = True
            update["explicitlyTrashed"] = True
        seq = self._drive.next_seq()
        update |= {"modifiedTime": self._now(), "version": str(seq), "lastModifyingUser": person}
        changed = wire.StoredFile(file=stored.file.model_copy(update=update), content=content)
        self._drive.write_file(changed, operation=Operation.UPDATE, actor=Actor.PERSON)

    def _appended(self, stored: wire.StoredFile, text: str) -> wire.Content | None:
        content = stored.content
        if isinstance(content, docs.DocBody):
            return docs.append_paragraph(content, text)
        if isinstance(content, slides.Deck):
            return slides.append_slide(stored.file.id, content, text)
        if isinstance(content, wire.Sheet):
            return content.model_copy(update={"rows": [*content.rows, [text]]})
        before = self._drive.blob(content) if isinstance(content, wire.BlobRef) else b""
        joined = before + (b"\n" if before and not before.endswith(b"\n") else b"") + text.encode("utf-8")
        return self._drive.keep_blob(joined, actor=Actor.PERSON)

    # ------------------------------------------------------------------ docs v1

    def _doc(self, document_id: str, caller: Caller, *, need: str) -> wire.StoredFile:
        try:
            stored = self._file(document_id, caller, wire.CallQuery(), need=need, all_drives=True)
        except wire.Refusal as refusal:
            if refusal.code == 403:
                raise _status_refusal(403, "PERMISSION_DENIED", "The caller does not have permission") from refusal
            raise _status_refusal(404, "NOT_FOUND", "Requested entity was not found.") from refusal
        if stored.file.mimeType != wire.DOCUMENT or not isinstance(stored.content, docs.DocBody):
            raise _status_refusal(400, "FAILED_PRECONDITION", "This operation is not supported for this document")
        return stored

    async def documents_get(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._doc(request.path_params["document_id"], caller, need="reader")
        assert isinstance(stored.content, docs.DocBody)
        try:
            mask = wire.selection(call.fields, docs.Document, "*")
        except wire.Refusal as refusal:
            raise _status_refusal(400, "INVALID_ARGUMENT", refusal.answer.error.message) from refusal
        revision = docs.revision(stored.file.id, stored.file.version)
        body = docs.render(
            stored.file.id, stored.file.name, revision, stored.content, tabs=call.includeTabsContent == "true"
        )
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return _json(body, mask)

    async def documents_create(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        try:
            asked = docs.CreateDocument.model_validate_json(await request.body() or b"{}")
        except ValidationError as error:
            raise _status_refusal(400, "INVALID_ARGUMENT", "Invalid JSON payload received.") from error
        stored = self._created(
            {"name": asked.title or "Untitled document", "mimeType": wire.DOCUMENT},
            caller,
            call,
            media=None,
            media_type=None,
        )
        assert isinstance(stored.content, docs.DocBody)
        revision = docs.revision(stored.file.id, stored.file.version)
        return _json(docs.render(stored.file.id, stored.file.name, revision, stored.content, tabs=False))

    async def documents_batch_update(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._doc(request.path_params["document_id"], caller, need="writer")
        content = stored.content
        assert isinstance(content, docs.DocBody)
        asked = docs.read_batch(await request.body())
        current = docs.revision(stored.file.id, stored.file.version)
        changed, replies = docs.update(stored.file.id, content, current, asked)
        revision = current
        if changed != content:
            changed = changed.model_copy(update={"revisions": [*changed.revisions, current]})
            updated = self._rewritten(stored, changed, caller)
            revision = docs.revision(updated.file.id, updated.file.version)
        answer = docs.BatchUpdateAnswer(
            documentId=stored.file.id,
            replies=replies,
            writeControl=docs.WriteControlAnswer(requiredRevisionId=revision),
        )
        return _json(answer)

    def _rewritten(self, stored: wire.StoredFile, content: wire.Content, caller: Caller) -> wire.StoredFile:
        seq = self._drive.next_seq()
        file = stored.file.model_copy(
            update={"modifiedTime": self._now(), "version": str(seq), "lastModifyingUser": caller.user}
        )
        updated = wire.StoredFile(file=file, content=content)
        self._drive.write_file(updated, operation=Operation.UPDATE, actor=Actor.AGENT)
        return updated

    # ------------------------------------------------------------------ slides v1

    def _deck(self, presentation_id: str, caller: Caller, *, need: str) -> wire.StoredFile:
        try:
            stored = self._file(presentation_id, caller, wire.CallQuery(), need=need, all_drives=True)
        except wire.Refusal as refusal:
            if refusal.code == 403:
                raise _status_refusal(403, "PERMISSION_DENIED", "The caller does not have permission") from refusal
            raise _status_refusal(404, "NOT_FOUND", "Requested entity was not found.") from refusal
        if stored.file.mimeType != wire.PRESENTATION or not isinstance(stored.content, slides.Deck):
            raise _status_refusal(400, "FAILED_PRECONDITION", "This operation is not supported for this document")
        return stored

    async def presentations_get(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._deck(request.path_params["presentation_id"], caller, need="reader")
        assert isinstance(stored.content, slides.Deck)
        try:
            mask = wire.selection(call.fields, slides.Presentation, "*")
        except wire.Refusal as refusal:
            raise _status_refusal(400, "INVALID_ARGUMENT", refusal.answer.error.message) from refusal
        revision = docs.revision(stored.file.id, stored.file.version)
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return _json(slides.render(stored.file.id, stored.file.name, revision, stored.content), mask)

    async def presentations_create(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        try:
            asked = docs.CreateDocument.model_validate_json(await request.body() or b"{}")
        except ValidationError as error:
            raise _status_refusal(400, "INVALID_ARGUMENT", "Invalid JSON payload received.") from error
        stored = self._created(
            {"name": asked.title or "Untitled presentation", "mimeType": wire.PRESENTATION},
            caller,
            call,
            media=None,
            media_type=None,
        )
        assert isinstance(stored.content, slides.Deck)
        revision = docs.revision(stored.file.id, stored.file.version)
        return _json(slides.render(stored.file.id, stored.file.name, revision, stored.content))

    async def presentations_batch_update(self, request: Request, call: wire.CallQuery, caller: Caller) -> Response:
        stored = self._deck(request.path_params["presentation_id"], caller, need="writer")
        content = stored.content
        assert isinstance(content, slides.Deck)
        asked = slides.read_batch(await request.body())
        changed, replies = slides.update(stored.file.id, content, asked, image_readable=self._public_image)
        updated = self._rewritten(stored, changed, caller) if changed != content else stored
        answer = slides.BatchUpdateAnswer(
            presentationId=stored.file.id,
            replies=replies,
            writeControl=slides.WriteControlAnswer(
                requiredRevisionId=docs.revision(updated.file.id, updated.file.version)
            ),
        )
        return _json(answer)

    def _public_image(self, url: str) -> bool:
        """Whether Google could fetch an image at `url`: a Drive file must exist and be open to anyone with the
        link; anything else is taken on trust, since the run fetches nothing."""
        parsed = urlparse(url)
        if parsed.hostname != "drive.google.com":
            return True
        ids = parse_qs(parsed.query).get("id") or []
        stored = self._drive.file(ids[0]) if ids else None
        if stored is None:
            return False
        return any(g.type == "anyone" for g in self._drive.grants(stored.file.id))

    # ------------------------------------------------------------------ sign-in

    async def token(self, request: Request) -> Response:
        """The grants a Google client signs in with; see the module docstring for what is and is not checked."""
        asked = wire.read_token_request(wire.read_form(await request.body()))
        try:
            self._fault("token", Api.OAUTH, request)
        except wire.Refusal as refusal:
            return _oauth_failed(
                "invalid_grant" if refusal.code == 401 else "temporarily_unavailable",
                "Token has been expired or revoked." if refusal.code == 401 else refusal.answer.error.message,
                401 if refusal.code == 401 else 503,
            )
        if asked.grant_type == wire.JWT_BEARER:
            if not asked.assertion:
                return _oauth_failed("invalid_request", "Missing required parameter: assertion")
            claims = wire.jwt_claims(asked.assertion)
            if claims is None:
                return _oauth_failed(
                    "invalid_grant", "Invalid JWT: Token must be a short-lived token and in a reasonable timeframe"
                )
            secret, acting, scope = claims.iss, claims.sub, None
        elif asked.grant_type == wire.REFRESH_TOKEN:
            if not asked.refresh_token:
                return _oauth_failed("invalid_request", "Missing required parameter: refresh_token")
            secret, acting, scope = asked.refresh_token, None, asked.scope
        elif asked.grant_type == wire.AUTHORIZATION_CODE:
            return _oauth_failed("invalid_grant", "Malformed auth code.")
        else:
            return _oauth_failed("unsupported_grant_type", f"Invalid grant_type: {asked.grant_type}")
        credential = self._drive.credential(secret)
        if credential is None:
            if asked.grant_type == wire.JWT_BEARER:
                return _oauth_failed("invalid_grant", "Invalid grant: account not found")
            return _oauth_failed("invalid_grant", "Bad Request")
        if credential.revoked:
            return _oauth_failed("invalid_grant", "Token has been expired or revoked.")
        email = credential.email
        if acting is not None and acting != email:
            if self._drive.user(acting) is None:
                return _oauth_failed("invalid_grant", "Invalid email or User ID")
            email = acting
        key = self._drive.credential_key(secret)
        if key == state.ANY_CREDENTIAL:
            key = state.secret_digest(secret)
            self._drive.keep_credential(key, credential, operation=Operation.CREATE)
        seq = self._drive.next_seq()
        access = "ya29.a0" + hashlib.sha256(f"access\x1f{secret}\x1f{seq}".encode()).hexdigest()
        expires = self._clock.now() + timedelta(seconds=wire.TOKEN_LIFETIME)
        self._drive.keep_token(
            access,
            wire.AccessToken(email=email, expires=wire.rfc3339(expires), credential=key),
        )
        return _json(wire.TokenAnswer(access_token=access, scope=scope))

    async def revoke(self, request: Request) -> Response:
        """Revoke an access token or a refresh token, and with either the whole grant: the credential and every
        access token issued for it. A service account's credential is not revoked, only its tokens."""
        form = wire.read_form(await request.body())
        query = wire.read_query(request.url.query)
        spelled = query.token or (form["token"] if "token" in form else None)
        if not spelled:
            return _oauth_failed("invalid_request", "Missing required parameter: token")
        issued = self._drive.token(spelled)
        if issued is not None:
            if issued.revoked:
                return _oauth_failed("invalid_token", "Token expired or revoked")
            key = issued.credential
        else:
            key = state.secret_digest(spelled)
            known = self._drive.credential_by_key(key)
            if known is None or known.revoked or known.service_account:
                return _oauth_failed("invalid_token", "Token expired or revoked")
        credential = self._drive.credential_by_key(key)
        if credential is not None and not credential.service_account and key != state.ANY_CREDENTIAL:
            self._drive.keep_credential(key, credential.model_copy(update={"revoked": True}))
        for token_key, token in self._drive.tokens():
            if token.credential == key and not token.revoked:
                self._drive.revoke_token(token_key, token)
        return Response(b"{}", media_type=JSON)

    async def allowed_locations(self, request: Request) -> Response:
        return _json(wire.AllowedLocations())


_STATUS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ABORTED",
    410: "FAILED_PRECONDITION",
    413: "INVALID_ARGUMENT",
    429: "RESOURCE_EXHAUSTED",
    501: "UNIMPLEMENTED",
    503: "UNAVAILABLE",
}


# ---------------------------------------------------------------------- helpers


def _oauth_failed(error: str, description: str, status: int = 400) -> Response:
    return _json(wire.OAuthError(error=error, error_description=description), status=status)


def _check_size(size: int) -> None:
    if size > wire.MAX_CONTENT_BYTES:
        raise wire.drive_refusal(
            413,
            "uploadTooLarge",
            f"Media is {size} bytes; this simulation stores at most {wire.MAX_CONTENT_BYTES} per file.",
        )


def _content_range(spelled: str | None, length: int, total: int | None) -> tuple[int | None, int | None]:
    """`Content-Range: bytes first-last/total`, `bytes */total`, or none (the whole body is the upload)."""
    if spelled is None:
        return 0, total if total is not None else length
    unit, _, rest = spelled.partition(" ")
    span, _, size = rest.partition("/")
    if unit != "bytes" or not size:
        raise wire.invalid("Content-Range")
    whole = None if size == "*" else int(size) if size.isdigit() else -1
    if whole == -1:
        raise wire.invalid("Content-Range")
    if span == "*":
        return None, whole
    first, _, last = span.partition("-")
    if not first.isdigit() or not last.isdigit() or int(last) - int(first) + 1 != length:
        raise wire.invalid("Content-Range")
    return int(first), whole


def _exported(content: wire.Content | None, mime_type: str) -> bytes:
    if isinstance(content, docs.DocBody):
        if mime_type == "text/markdown":
            return docs.text_of(content, markdown=True).encode("utf-8")
        return wire.plain_text_export(docs.text_of(content) + "\n")
    if isinstance(content, slides.Deck):
        return wire.plain_text_export(slides.text_of(content) + "\n")
    if isinstance(content, wire.Sheet):
        out = io.StringIO()
        writer = csv.writer(out, delimiter="," if mime_type == "text/csv" else "\t", lineterminator="\r\n")
        writer.writerows(content.rows)
        return out.getvalue().encode("utf-8")
    raise wire.not_implemented(f"exporting this file as {mime_type}")


def _sharing_refused(why: str) -> wire.Refusal:
    return wire.drive_refusal(400, "invalidSharingRequest", f'Bad Request. User message: "{why}"')


def _not_writable(field: str) -> wire.Refusal:
    return wire.forbidden(
        "fieldNotWritable", f"The resource body includes fields which are not directly writable: {field}."
    )


def _empty(mime: str, file_id: str) -> wire.Content | None:
    """What a file made without media holds: an empty Doc, deck or sheet, or nothing."""
    if mime == wire.DOCUMENT:
        return docs.empty()
    if mime == wire.PRESENTATION:
        return slides.new_deck(file_id)
    if mime == wire.SPREADSHEET:
        return wire.Sheet()
    if mime.startswith(wire.GOOGLE_APPS) and mime != wire.FOLDER:
        raise wire.not_implemented(f"creating a {mime}")
    return None


def _measured(content: wire.Content | None) -> tuple[str | None, str | None]:
    """`size` and `md5Checksum`, which Drive serves for binary content only."""
    if isinstance(content, wire.BlobRef):
        return str(content.size), content.md5
    return None, None


def _order(spelled: str | None) -> list[tuple[str, bool]]:
    """`orderBy`: comma-separated keys, each optionally followed by `desc`."""
    if not spelled:
        return []
    order: list[tuple[str, bool]] = []
    for part in spelled.split(","):
        words = part.split()
        if not words or len(words) > 2 or (len(words) == 2 and words[1] != "desc"):
            raise wire.invalid("orderBy")
        if words[0] in ORDER_KEYS_NOT_BUILT:
            raise wire.not_implemented(f"ordering by {words[0]}")
        if words[0] not in ORDER_KEYS:
            raise wire.invalid("orderBy", f"Invalid Value: sorting is not supported for '{words[0]}'")
        order.append((words[0], len(words) == 2))
    return order


def _sort_key(key: str) -> Callable[[wire.DriveFile], str | bool]:
    if key == "name":
        return lambda f: f.name.lower()
    if key == "createdTime":
        return lambda f: f.createdTime
    if key == "modifiedTime":
        return lambda f: f.modifiedTime
    if key == "folder":
        return lambda f: f.mimeType != wire.FOLDER
    return lambda f: not f.starred


class HostRouter:
    """Send each request to its host's routes, or to every route when the host is not one of Google's.

    The path is matched decoded, as ASGI says it arrives: mitmproxy hands the app a percent-encoded one, which
    turns Docs' `/v1/documents/{id}:batchUpdate` into `{id}%3AbatchUpdate`."""

    def __init__(self, by_host: dict[str, Router], every: Router) -> None:
        self._by_host = by_host
        self._every = every

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers: list[tuple[bytes, bytes]] = scope["headers"] if "headers" in scope else []
        host = next((value for name, value in headers if name == b"host"), b"").decode("latin-1").lower()
        if not host.startswith("["):
            host = host.rsplit(":", 1)[0]
        path = scope["path"] if "path" in scope else ""
        if isinstance(path, str) and "%" in path:
            scope = {**scope, "path": unquote(path)}
        await (self._by_host[host] if host in self._by_host else self._every)(scope, receive, send)


def build_app(api: DriveApi) -> HostRouter:
    def drive(handler: Callable[[Request, wire.CallQuery, Caller], Awaitable[Response]], operation: str) -> Handler:
        return api.guarded(handler, Api.DRIVE, operation)

    routes = [
        Route("/drive/v3/files", drive(api.files_list, "files.list"), methods=["GET"]),
        Route("/drive/v3/files", drive(api.files_create, "files.create"), methods=["POST"]),
        Route("/drive/v3/files/{file_id}", drive(api.files_get, "files.get"), methods=["GET"]),
        Route("/drive/v3/files/{file_id}", drive(api.files_update, "files.update"), methods=["PATCH"]),
        Route("/drive/v3/files/{file_id}", drive(api.files_delete, "files.delete"), methods=["DELETE"]),
        Route("/drive/v3/files/{file_id}/export", drive(api.files_export, "files.export"), methods=["GET"]),
        Route("/drive/v3/files/{file_id}/copy", drive(api.files_copy, "files.copy"), methods=["POST"]),
        Route(
            "/drive/v3/files/{file_id}/permissions", drive(api.permissions_list, "permissions.list"), methods=["GET"]
        ),
        Route(
            "/drive/v3/files/{file_id}/permissions",
            drive(api.permissions_create, "permissions.create"),
            methods=["POST"],
        ),
        Route(
            "/drive/v3/files/{file_id}/permissions/{permission_id}",
            drive(api.permissions_delete, "permissions.delete"),
            methods=["DELETE"],
        ),
        Route("/drive/v3/files/{file_id}/comments", drive(api.comments_list, "comments.list"), methods=["GET"]),
        Route("/drive/v3/files/{file_id}/comments", drive(api.comments_create, "comments.create"), methods=["POST"]),
        Route("/drive/v3/about", drive(api.about, "about.get"), methods=["GET"]),
        Route("/drive/v3/drives", drive(api.drives_list, "drives.list"), methods=["GET"]),
        Route("/drive/v3/drives/{drive_id}", drive(api.drives_get, "drives.get"), methods=["GET"]),
        Route(
            "/drive/v3/changes/startPageToken", drive(api.changes_start, "changes.getStartPageToken"), methods=["GET"]
        ),
        Route("/drive/v3/changes", drive(api.changes_list, "changes.list"), methods=["GET"]),
        Route("/drive/v3/changes/watch", drive(api.changes_watch, "changes.watch"), methods=["POST"]),
        Route("/drive/v3/channels/stop", drive(api.channels_stop, "channels.stop"), methods=["POST"]),
        Route("/upload/drive/v3/files", drive(api.upload_create, "files.create"), methods=["POST"]),
        Route("/upload/drive/v3/files", drive(api.upload_put, "files.create"), methods=["PUT"]),
        Route("/upload/drive/v3/files/{file_id}", drive(api.upload_update, "files.update"), methods=["PATCH"]),
        Route("/upload/drive/v3/files/{file_id}", drive(api.upload_put, "files.update"), methods=["PUT"]),
        Route("/oauth2/v2/userinfo", api.guarded(api.userinfo, Api.USERINFO, "userinfo.get"), methods=["GET"]),
    ]
    oauth = [
        Route("/token", api.token, methods=["POST"]),
        Route("/revoke", api.revoke, methods=["POST"]),
    ]
    documents = [
        Route("/v1/documents", api.guarded(api.documents_create, Api.DOCS, "documents.create"), methods=["POST"]),
        Route(
            "/v1/documents/{document_id}:batchUpdate",
            api.guarded(api.documents_batch_update, Api.DOCS, "documents.batchUpdate"),
            methods=["POST"],
        ),
        Route(
            "/v1/documents/{document_id}", api.guarded(api.documents_get, Api.DOCS, "documents.get"), methods=["GET"]
        ),
    ]
    presentations = [
        Route(
            "/v1/presentations",
            api.guarded(api.presentations_create, Api.SLIDES, "presentations.create"),
            methods=["POST"],
        ),
        Route(
            "/v1/presentations/{presentation_id}:batchUpdate",
            api.guarded(api.presentations_batch_update, Api.SLIDES, "presentations.batchUpdate"),
            methods=["POST"],
        ),
        Route(
            "/v1/presentations/{presentation_id}",
            api.guarded(api.presentations_get, Api.SLIDES, "presentations.get"),
            methods=["GET"],
        ),
    ]
    iam = [
        Route(
            "/v1/projects/{project}/serviceAccounts/{account}/allowedLocations", api.allowed_locations, methods=["GET"]
        )
    ]
    return HostRouter(
        {
            DRIVE_HOST: Router(routes=routes),
            OAUTH_HOST: Router(routes=oauth),
            DOCS_HOST: Router(routes=documents),
            SLIDES_HOST: Router(routes=presentations),
            IAM_HOST: Router(routes=iam),
        },
        Router(routes=[*routes, *oauth, *documents, *presentations, *iam]),
    )
