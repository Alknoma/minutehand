"""Drive v3, Docs v1 and Google's OAuth token endpoint, as one ASGI app over the run's store and clock.

**Hosts.** A request whose `Host` is one of the manifest's three hosts is answered only
by that host's routes: `/token` on `www.googleapis.com` is a 404, as it is at Google.
Any other host — a client reaching the provider directly, without the proxy — is
answered by every route, which is safe because the three hosts' paths do not overlap.

**Sign-in is not checked.** `POST /token` answers the service-account JWT-bearer grant
and the refresh-token grant with a bearer token. The assertion's signature, its claims
and the refresh token are never verified: the run holds no Google keys to verify them
against. Any bearer token is then accepted by Drive and Docs; a call with none is 401
in Google's shape. Every call is made as one user, `state.AGENT_EMAIL`.

**Errors** are Google's: Drive's classic envelope (`code`, `message`, one `errors`
entry with `domain` and `reason`), and Docs v1's (`code`, `message`, `status`). A real
Drive behaviour this fake does not reproduce answers 501 with reason
`notImplemented` in domain `minutehand`, never a made-up success.

**Content** is stored inside the file's entity, up to `wire.MAX_CONTENT_BYTES` (5 MiB)
per file. A larger upload is refused with 413 `uploadTooLarge`; that limit is this
fake's, not Drive's.
"""

from __future__ import annotations

import hashlib
import html
from collections.abc import Awaitable, Callable

from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, Router
from starlette.types import Receive, Scope, Send

from minutehand.adapters.providers.google_drive import query as drive_query
from minutehand.adapters.providers.google_drive import state, wire
from minutehand.adapters.providers.google_drive.state import ROOT_ALIAS, ROOT_ID, DriveWorld
from minutehand.domain.scenario import Model
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

DRIVE_HOST = "www.googleapis.com"
OAUTH_HOST = "oauth2.googleapis.com"
DOCS_HOST = "docs.googleapis.com"

JSON = "application/json; charset=UTF-8"
CONVERTS_TO_DOC = frozenset({"text/plain", "text/markdown"})
"""Media this fake converts into a Google Doc's text. Drive converts more (HTML, Word); those are refused loudly."""
DOC_EXPORTS_NOT_BUILT = frozenset({
    "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text", "application/rtf", "text/html", "application/zip",
    "application/epub+zip",
})
"""Formats real Drive exports a Doc to and this fake does not render."""
ORDER_KEYS = frozenset({"name", "createdTime", "modifiedTime", "folder", "starred"})
ORDER_KEYS_NOT_BUILT = frozenset({
    "name_natural", "quotaBytesUsed", "recency", "sharedWithMeTime", "viewedByMeTime", "modifiedByMeTime",
})

Handler = Callable[[Request], Awaitable[Response]]


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


def _guarded(handler: Callable[[Request, wire.CallQuery], Awaitable[Response]], *, docs: bool = False) -> Handler:
    """Read the query, demand a bearer token, and turn a refusal into Google's error body."""

    async def endpoint(request: Request) -> Response:
        try:
            call = wire.read_query(request.url.query)
            if _bearer(request, call) is None:
                raise wire.docs_login_required() if docs else wire.login_required()
            return await handler(request, call)
        except wire.Refusal as refusal:
            return _refused(refusal)

    return endpoint


class DriveApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._drive = DriveWorld(store)
        self._clock = clock

    # ------------------------------------------------------------------ lookups

    def _now(self) -> str:
        return wire.rfc3339(self._clock.now())

    def _file(self, file_id: str) -> wire.StoredFile:
        found = self._drive.file(file_id)
        if found is None:
            raise wire.not_found(file_id)
        return found

    def _not_root(self, stored: wire.StoredFile) -> None:
        if stored.file.id == ROOT_ID:
            raise wire.forbidden(
                "insufficientFilePermissions", "The user does not have sufficient permissions for this file.",
            )

    def _served(self, stored: wire.StoredFile, *, trashed_above: bool | None = None) -> wire.DriveFile:
        above = self._drive.trashed(stored) if trashed_above is None else trashed_above or stored.file.trashed
        return stored.file if above == stored.file.trashed else stored.file.model_copy(update={"trashed": above})

    def _folder(self, folder_id: str) -> wire.StoredFile:
        """A folder a file may be put in: it exists and it is a folder."""
        folder = self._file(folder_id)
        if folder.file.mimeType != wire.FOLDER:
            raise wire.drive_refusal(
                400, "invalid", f"The specified parent is not a folder: {folder_id}.", location="parents",
                location_type="other",
            )
        return folder

    def _parent(self, requested: list[str] | None) -> str:
        """The one parent of a new file: the folder asked for, or My Drive."""
        if not requested:
            root = self._drive.root()
            if root is None:
                raise wire.not_found(ROOT_ALIAS)
            return root.file.id
        if len(requested) > 1:
            raise wire.forbidden("cannotAddParent", "Increasing the number of parents is not allowed.")
        return self._folder(requested[0]).file.id

    # ------------------------------------------------------------------ files.list

    async def files_list(self, request: Request, call: wire.CallQuery) -> Response:
        try:
            parsed = drive_query.parse(call.q)
        except drive_query.QueryError as error:
            raise wire.invalid("q", f"Invalid Value: {error}") from error
        except drive_query.QueryNotSupported as error:
            raise wire.not_implemented(str(error)) from error
        parsed = drive_query.resolved(parsed, ROOT_ALIAS, ROOT_ID)
        size = wire.page_size(call.pageSize, default=100, most=1000)
        offset = wire.decode_page(call.pageToken)
        mask = wire.selection(call.fields, wire.FileList, wire.LIST_DEFAULT)
        order = _order(call.orderBy)
        matched: list[wire.DriveFile] = []
        for stored, trashed_above in self._drive.walk():
            served = self._served(stored, trashed_above=trashed_above)
            if drive_query.matches(parsed, _candidate(stored, served)):
                matched.append(served)
        for key, descending in reversed(order):
            matched.sort(key=_sort_key(key), reverse=descending)
        page = matched[offset:offset + size]
        more = offset + size < len(matched)
        self._drive.saw(state.file_ref(ROOT_ID), Operation.SEARCH)
        return _json(wire.FileList(files=page, nextPageToken=wire.encode_page(offset + size) if more else None), mask)

    # ------------------------------------------------------------------ files.get, export

    async def files_get(self, request: Request, call: wire.CallQuery) -> Response:
        stored = self._file(request.path_params["file_id"])
        if call.alt == "media":
            if stored.file.mimeType.startswith(wire.GOOGLE_APPS):
                raise wire.forbidden(
                    "fileNotDownloadable",
                    "Only files with binary content can be downloaded. Use Export with Docs Editors files.",
                )
            content = wire.blob_bytes(stored.content) if isinstance(stored.content, wire.Blob) else b""
            self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
            return Response(content, media_type=stored.file.mimeType)
        mask = wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT)
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return _json(self._served(stored), mask)

    async def files_export(self, request: Request, call: wire.CallQuery) -> Response:
        if not call.mimeType:
            raise wire.required("mimeType")
        stored = self._file(request.path_params["file_id"])
        source = stored.file.mimeType
        if source == wire.FOLDER or not source.startswith(wire.GOOGLE_APPS):
            raise wire.forbidden("fileNotExportable", "Export only supports Docs Editors files.")
        if source != wire.DOCUMENT:
            raise wire.not_implemented(f"this simulation exports Google Docs only, not {source}")
        text = state.readable_text(stored)
        if call.mimeType == "text/plain":
            exported = wire.plain_text_export(text)
        elif call.mimeType == "text/markdown":
            exported = text.encode("utf-8")
        elif call.mimeType in DOC_EXPORTS_NOT_BUILT:
            raise wire.not_implemented(f"this simulation does not render a Google Doc as {call.mimeType}")
        else:
            raise wire.drive_refusal(
                400, "badRequest", "The requested conversion is not supported.", location="convertTo",
                location_type="parameter",
            )
        if len(exported) > wire.EXPORT_LIMIT_BYTES:
            raise wire.forbidden("exportSizeLimitExceeded", "This file is too large to be exported.")
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return Response(exported, media_type=call.mimeType)

    # ------------------------------------------------------------------ files.create, upload

    async def files_create(self, request: Request, call: wire.CallQuery) -> Response:
        found = wire.read_object(await request.body())
        stored = self._created(found, media=None, media_type=None)
        return _json(stored.file, wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    async def upload_create(self, request: Request, call: wire.CallQuery) -> Response:
        found, media, media_type = await self._upload(request, call)
        stored = self._created(found, media=media, media_type=media_type)
        return _json(stored.file, wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    async def _upload(self, request: Request, call: wire.CallQuery) -> tuple[dict[str, JsonValue], bytes, str]:
        raw = await request.body()
        content_type = request.headers["content-type"] if "content-type" in request.headers else wire.OCTET_STREAM
        if call.uploadType == "multipart":
            upload = wire.read_multipart(content_type, raw)
            found, media, media_type = upload.metadata, upload.media, upload.media_type
        elif call.uploadType == "media":
            found, media, media_type = {}, raw, wire.header_value(content_type)[0]
        elif call.uploadType == "resumable":
            raise wire.not_implemented("resumable uploads; use uploadType=multipart or uploadType=media")
        else:
            raise wire.invalid("uploadType")
        if len(media) > wire.MAX_CONTENT_BYTES:
            raise wire.drive_refusal(
                413, "uploadTooLarge",
                f"Media is {len(media)} bytes; this simulation stores at most {wire.MAX_CONTENT_BYTES} per file.",
            )
        return found, media, media_type

    def _created(self, found: dict[str, JsonValue], *, media: bytes | None, media_type: str | None) -> wire.StoredFile:
        if (refused := wire.unwritable(found)) is not None:
            raise _not_writable(refused)
        meta = wire.read_body(wire.FileWrite, found)
        if meta.id is not None:
            raise wire.drive_refusal(400, "fileIdNotUsable", "The provided file ID is not usable.", location="file.id")
        mime = meta.mimeType or media_type or wire.OCTET_STREAM
        content = _content(mime, media, media_type) if media is not None else _empty(mime)
        parent = self._parent(meta.parents)
        seq = self._drive.next_seq()
        new_id = state.file_id(seq)
        now = self._now()
        trashed = bool(meta.trashed)
        size, checksum = _measured(content)
        stored = wire.StoredFile(
            file=wire.DriveFile(
                id=new_id, name=meta.name or "Untitled", mimeType=mime, description=meta.description,
                starred=bool(meta.starred), trashed=trashed, explicitlyTrashed=trashed, parents=[parent],
                owners=[state.agent()], createdTime=now, modifiedTime=now, version=str(seq),
                webViewLink=wire.web_view_link(new_id, mime), size=size, md5Checksum=checksum,
            ),
            content=content,
        )
        self._drive.write_file(stored, operation=Operation.CREATE, actor=Actor.AGENT)
        return stored

    # ------------------------------------------------------------------ files.update, upload update

    async def files_update(self, request: Request, call: wire.CallQuery) -> Response:
        stored = self._file(request.path_params["file_id"])
        found = wire.read_object(await request.body())
        updated = self._updated(stored, found, call, content=None)
        return _json(self._served(updated), wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    async def upload_update(self, request: Request, call: wire.CallQuery) -> Response:
        stored = self._file(request.path_params["file_id"])
        found, media, media_type = await self._upload(request, call)
        content = _content(stored.file.mimeType, media, media_type)
        updated = self._updated(stored, found, call, content=content)
        return _json(self._served(updated), wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    def _updated(
        self, stored: wire.StoredFile, found: dict[str, JsonValue], call: wire.CallQuery,
        *, content: wire.DocText | wire.Blob | None,
    ) -> wire.StoredFile:
        self._not_root(stored)
        if (refused := wire.unwritable(found, also=frozenset({"parents", "id"}))) is not None:
            raise _not_writable(refused)
        meta = wire.read_body(wire.FileWrite, found)
        mime = stored.file.mimeType
        if meta.mimeType is not None and meta.mimeType != mime:
            if mime.startswith(wire.GOOGLE_APPS) or meta.mimeType.startswith(wire.GOOGLE_APPS):
                raise wire.bad_request("A Docs Editors file's type cannot be changed, nor a file made one.")
            mime = meta.mimeType
        seq = self._drive.next_seq()
        changes: dict[str, object] = {
            "mimeType": mime, "parents": self._moved(stored, call), "modifiedTime": self._now(), "version": str(seq),
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

    def _moved(self, stored: wire.StoredFile, call: wire.CallQuery) -> list[str]:
        """The file's parents after `addParents` and `removeParents`: still exactly one folder."""
        current = list(stored.file.parents or [])
        added = wire.id_list(call.addParents)
        removed = {DriveWorld.resolve(p) for p in wire.id_list(call.removeParents)}
        if not added and not removed:
            return current
        parents = [p for p in current if p not in removed]
        for folder_id in added:
            folder = self._folder(folder_id)
            if folder.file.id == stored.file.id or any(a.file.id == stored.file.id for a in self._drive.ancestors(folder)):
                raise wire.drive_refusal(
                    400, "invalid", "A folder cannot be moved into itself or a folder inside it.",
                    location="addParents", location_type="parameter",
                )
            if folder.file.id not in parents:
                parents.append(folder.file.id)
        if len(parents) > 1:
            raise wire.forbidden("cannotAddParent", "Increasing the number of parents is not allowed.")
        if not parents:
            raise wire.not_implemented("a file left with no parent; move it with addParents and removeParents together")
        return parents

    # ------------------------------------------------------------------ files.delete, files.copy

    async def files_delete(self, request: Request, call: wire.CallQuery) -> Response:
        stored = self._file(request.path_params["file_id"])
        self._not_root(stored)
        self._drive.delete_file(stored, actor=Actor.AGENT)
        return Response(status_code=204)

    async def files_copy(self, request: Request, call: wire.CallQuery) -> Response:
        source = self._file(request.path_params["file_id"])
        if source.file.mimeType == wire.FOLDER:
            raise wire.forbidden("cannotCopyFile", "This file cannot be copied by the user.")
        found = wire.read_object(await request.body())
        if (refused := wire.unwritable(found)) is not None:
            raise _not_writable(refused)
        meta = wire.read_body(wire.FileWrite, found)
        if meta.mimeType is not None and meta.mimeType != source.file.mimeType:
            raise wire.not_implemented("converting a file while copying it")
        parent = self._parent(meta.parents or source.file.parents)
        seq = self._drive.next_seq()
        new_id = state.file_id(seq)
        now = self._now()
        copied = wire.StoredFile(
            file=source.file.model_copy(update={
                "id": new_id, "name": meta.name or f"Copy of {source.file.name}", "parents": [parent],
                "description": meta.description if meta.description is not None else source.file.description,
                "starred": bool(meta.starred), "trashed": False, "explicitlyTrashed": False,
                "owners": [state.agent()], "createdTime": now, "modifiedTime": now, "version": str(seq),
                "webViewLink": wire.web_view_link(new_id, source.file.mimeType),
            }),
            content=source.content,
        )
        self._drive.write_file(copied, operation=Operation.CREATE, actor=Actor.AGENT)
        return _json(copied.file, wire.selection(call.fields, wire.DriveFile, wire.FILE_DEFAULT))

    # ------------------------------------------------------------------ permissions

    def _permissions(self, stored: wire.StoredFile) -> list[wire.Permission]:
        """The owner, then what was granted on the file, then what it inherits from the folders above it."""
        found: dict[str, wire.Permission] = {}
        for owner in stored.file.owners:
            found.setdefault(owner.permissionId, wire.Permission(
                id=owner.permissionId, type="user", role="owner", emailAddress=owner.emailAddress,
                displayName=owner.displayName,
            ))
        for holder in [stored, *self._drive.ancestors(stored)]:
            for permission in self._drive.grants(holder.file.id):
                found.setdefault(permission.id, permission)
        return list(found.values())

    async def permissions_list(self, request: Request, call: wire.CallQuery) -> Response:
        stored = self._file(request.path_params["file_id"])
        size = wire.page_size(call.pageSize, default=100, most=100)
        offset = wire.decode_page(call.pageToken)
        mask = wire.selection(call.fields, wire.PermissionList, wire.PERMISSION_LIST_DEFAULT)
        every = self._permissions(stored)
        more = offset + size < len(every)
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return _json(wire.PermissionList(
            permissions=every[offset:offset + size], nextPageToken=wire.encode_page(offset + size) if more else None,
        ), mask)

    async def permissions_create(self, request: Request, call: wire.CallQuery) -> Response:
        stored = self._file(request.path_params["file_id"])
        asked = wire.read_body(wire.PermissionWrite, wire.read_object(await request.body()))
        permission = self._permission(asked, transfer=call.transferOwnership == "true")
        mask = wire.selection(call.fields, wire.Permission, wire.PERMISSION_DEFAULT)
        existing = any(p.id == permission.id for p in self._drive.grants(stored.file.id))
        self._drive.write_grant(
            stored.file.id, permission, operation=Operation.UPDATE if existing else Operation.CREATE,
            actor=Actor.AGENT,
        )
        return _json(permission, mask)

    def _permission(self, asked: wire.PermissionWrite, *, transfer: bool) -> wire.Permission:
        if asked.type not in wire.PERMISSION_TYPES:
            raise wire.drive_refusal(400, "invalid", f"Invalid value for PermissionType: {asked.type}",
                                     location="permission.type", location_type="other")
        if asked.role not in wire.ROLES:
            raise wire.drive_refusal(400, "invalid", f"Invalid value for Role: {asked.role}",
                                     location="permission.role", location_type="other")
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
                    "forbidden", "The transferOwnership parameter must be enabled when the permission role is 'owner'.",
                )
            raise wire.not_implemented("transferring ownership")
        if asked.emailAddress:
            person = self._drive.user(asked.emailAddress)
            return wire.Permission(
                id=state.permission_id(asked.emailAddress), type=kind, role=role, emailAddress=asked.emailAddress,
                displayName=person.displayName if person is not None else None,
            )
        if kind == "domain" and asked.domain:
            return wire.Permission(
                id=state.domain_permission_id(asked.domain), type=kind, role=role, domain=asked.domain,
                allowFileDiscovery=bool(asked.allowFileDiscovery),
            )
        return wire.Permission(
            id=state.ANYONE_PERMISSION_ID, type=kind, role=role, allowFileDiscovery=bool(asked.allowFileDiscovery),
        )

    # ------------------------------------------------------------------ comments

    async def comments_list(self, request: Request, call: wire.CallQuery) -> Response:
        if call.fields is None:
            raise wire.fields_required()
        stored = self._file(request.path_params["file_id"])
        size = wire.page_size(call.pageSize, default=20, most=100)
        offset = wire.decode_page(call.pageToken)
        mask = wire.selection(call.fields, wire.CommentList, call.fields)
        every = self._drive.comments(stored.file.id)
        more = offset + size < len(every)
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return _json(wire.CommentList(
            comments=every[offset:offset + size], nextPageToken=wire.encode_page(offset + size) if more else None,
        ), mask)

    async def comments_create(self, request: Request, call: wire.CallQuery) -> Response:
        if call.fields is None:
            raise wire.fields_required()
        stored = self._file(request.path_params["file_id"])
        mask = wire.selection(call.fields, wire.Comment, call.fields)
        asked = wire.read_body(wire.CommentWrite, wire.read_object(await request.body()))
        if not asked.content.strip():
            raise wire.required("content", "Required: content")
        me = state.agent()
        now = self._now()
        comment = wire.Comment(
            id=state.comment_id(self._drive.next_seq()), createdTime=now, modifiedTime=now,
            author=wire.DriveUser(displayName=me.displayName, permissionId=me.permissionId, me=True),
            htmlContent=html.escape(asked.content), content=asked.content, anchor=asked.anchor,
            quotedFileContent=asked.quotedFileContent,
        )
        self._drive.write_comment(stored.file.id, comment, actor=Actor.AGENT)
        return _json(comment, mask)

    # ------------------------------------------------------------------ about

    async def about(self, request: Request, call: wire.CallQuery) -> Response:
        if call.fields is None:
            raise wire.fields_required()
        mask = wire.selection(call.fields, wire.About, call.fields)
        me = state.agent()
        self._drive.saw(state.user_ref(me.permissionId), Operation.READ)
        return _json(wire.About(user=me.model_copy(update={"me": True})), mask)

    # ------------------------------------------------------------------ docs v1

    async def documents_get(self, request: Request, call: wire.CallQuery) -> Response:
        document_id = request.path_params["document_id"]
        stored = self._drive.file(document_id)
        if stored is None or stored.file.id == ROOT_ID:
            raise wire.docs_refusal(404, "NOT_FOUND", "Requested entity was not found.")
        if stored.file.mimeType != wire.DOCUMENT:
            raise wire.docs_refusal(400, "FAILED_PRECONDITION", "This operation is not supported for this document")
        try:
            mask = wire.selection(call.fields, wire.Document, "*")
        except wire.Refusal as refusal:
            raise wire.docs_refusal(400, "INVALID_ARGUMENT", refusal.answer.error.message) from refusal
        revision = "rev" + hashlib.sha256(f"{stored.file.id}:{stored.file.version}".encode()).hexdigest()[:24]
        body = wire.document(stored.file.id, stored.file.name, revision, state.readable_text(stored))
        self._drive.saw(state.file_ref(stored.file.id), Operation.READ)
        return _json(body, mask)


# ---------------------------------------------------------------------- the token endpoint


async def token(request: Request) -> Response:
    """Both grants a Google client signs in with. Nothing presented is verified; see the module docstring."""
    asked = wire.read_token_request(wire.read_form(await request.body()))
    if asked.grant_type == wire.JWT_BEARER:
        presented = asked.assertion
        missing = "assertion"
    elif asked.grant_type == wire.REFRESH_TOKEN:
        presented = asked.refresh_token
        missing = "refresh_token"
    else:
        failed = wire.OAuthError(error="unsupported_grant_type", error_description=f"Invalid grant_type: {asked.grant_type}")
        return _json(failed, status=400)
    if not presented:
        failed = wire.OAuthError(error="invalid_request", error_description=f"Missing required parameter: {missing}")
        return _json(failed, status=400)
    access = "ya29.sim-" + hashlib.sha256(f"{asked.grant_type}\x1f{presented}".encode()).hexdigest()[:48]
    return _json(wire.TokenAnswer(access_token=access, scope=asked.scope))


# ---------------------------------------------------------------------- helpers


def _sharing_refused(why: str) -> wire.Refusal:
    return wire.drive_refusal(400, "invalidSharingRequest", f'Bad Request. User message: "{why}"')


def _not_writable(field: str) -> wire.Refusal:
    return wire.forbidden("fieldNotWritable", f"The resource body includes fields which are not directly writable: {field}.")


def _empty(mime: str) -> wire.DocText | wire.Blob | None:
    """What a file made without media holds: an empty Doc, or nothing."""
    return wire.DocText(text="") if mime == wire.DOCUMENT else None


def _content(target: str, media: bytes | None, media_type: str | None) -> wire.DocText | wire.Blob:
    """Uploaded media as the file will hold it: converted into a Doc's text, or kept as bytes."""
    media = media or b""
    if target == wire.DOCUMENT:
        if media_type not in CONVERTS_TO_DOC:
            raise wire.not_implemented(f"converting {media_type} into a Google Doc; upload text/plain or text/markdown")
        try:
            text = media.decode("utf-8")
        except UnicodeDecodeError as error:
            raise wire.bad_request("The media is not UTF-8 text.") from error
        return wire.DocText(text=text.removeprefix(wire.BOM))
    if target == wire.FOLDER:
        raise wire.bad_request("A folder has no content.")
    if target.startswith(wire.GOOGLE_APPS):
        raise wire.not_implemented(f"converting media into {target}")
    return wire.blob(media)


def _measured(content: wire.DocText | wire.Blob | None) -> tuple[str | None, str | None]:
    """`size` and `md5Checksum`, which Drive serves for binary content only."""
    if isinstance(content, wire.Blob):
        raw = wire.blob_bytes(content)
        return str(len(raw)), wire.md5(raw)
    return None, None


def _candidate(stored: wire.StoredFile, served: wire.DriveFile) -> drive_query.Candidate:
    return drive_query.Candidate(
        name=served.name, mime_type=served.mimeType, parents=served.parents or [], trashed=served.trashed,
        starred=served.starred, created=wire.moment(served.createdTime), modified=wire.moment(served.modifiedTime),
        full_text="\n".join([served.name, served.description or "", state.readable_text(stored)]),
    )


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
    """Send each request to its host's routes, or to every route when the host is not one of Google's."""

    def __init__(self, by_host: dict[str, Router], every: Router) -> None:
        self._by_host = by_host
        self._every = every

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers: list[tuple[bytes, bytes]] = scope["headers"] if "headers" in scope else []
        host = next((value for name, value in headers if name == b"host"), b"").decode("latin-1").lower()
        if not host.startswith("["):
            host = host.rsplit(":", 1)[0]
        await (self._by_host[host] if host in self._by_host else self._every)(scope, receive, send)


def build_app(store: Store, clock: Clock) -> HostRouter:
    api = DriveApi(store, clock)
    drive = [
        Route("/drive/v3/files", _guarded(api.files_list), methods=["GET"]),
        Route("/drive/v3/files", _guarded(api.files_create), methods=["POST"]),
        Route("/drive/v3/files/{file_id}", _guarded(api.files_get), methods=["GET"]),
        Route("/drive/v3/files/{file_id}", _guarded(api.files_update), methods=["PATCH"]),
        Route("/drive/v3/files/{file_id}", _guarded(api.files_delete), methods=["DELETE"]),
        Route("/drive/v3/files/{file_id}/export", _guarded(api.files_export), methods=["GET"]),
        Route("/drive/v3/files/{file_id}/copy", _guarded(api.files_copy), methods=["POST"]),
        Route("/drive/v3/files/{file_id}/permissions", _guarded(api.permissions_list), methods=["GET"]),
        Route("/drive/v3/files/{file_id}/permissions", _guarded(api.permissions_create), methods=["POST"]),
        Route("/drive/v3/files/{file_id}/comments", _guarded(api.comments_list), methods=["GET"]),
        Route("/drive/v3/files/{file_id}/comments", _guarded(api.comments_create), methods=["POST"]),
        Route("/drive/v3/about", _guarded(api.about), methods=["GET"]),
        Route("/upload/drive/v3/files", _guarded(api.upload_create), methods=["POST"]),
        Route("/upload/drive/v3/files/{file_id}", _guarded(api.upload_update), methods=["PATCH"]),
    ]
    oauth = [Route("/token", token, methods=["POST"])]
    docs = [Route("/v1/documents/{document_id}", _guarded(api.documents_get, docs=True), methods=["GET"])]
    return HostRouter(
        {DRIVE_HOST: Router(routes=drive), OAUTH_HOST: Router(routes=oauth), DOCS_HOST: Router(routes=docs)},
        Router(routes=[*drive, *oauth, *docs]),
    )
