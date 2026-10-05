"""Graph for files: SharePoint sites, document libraries and OneDrive, and the pre-authenticated download and
upload URLs on the tenant's `*.sharepoint.com` host.

A drive is addressed four ways, all answered the same: `/drives/{id}`, `/me/drive` (a user's token only),
`/users/{id | upn}/drive`, `/sites/{id}/drive`. An item under it by id (`/items/{id}`), as the root (`/root`), or
by path relative to either (`/root:/a/b.docx:`, `/items/{id}:/b.docx:`), then optionally one of `/children`,
`/content`, `/delta` (root only), `/search(q='…')`, `/createUploadSession`, `/copy`, `/invite`, `/createLink`,
`/permissions[/{id}]`.

What Graph does and this does too:
- `GET …/content` answers **302** to a pre-authenticated URL on the site's host (`tempauth` in its query), as
  Graph does; the bytes are served there. A client that does not follow redirects gets the 302.
- `PUT …/content` replaces an item's bytes, or by path creates the file and any folder missing above it.
  `@microsoft.graph.conflictBehavior` (`fail`, `replace`, `rename`) is honoured on create.
- `createUploadSession` answers an `uploadUrl` on the site's host taking `Content-Range` fragments: 202 with
  `nextExpectedRanges` until the last, then the item.
- `copy` answers 202 with a monitor URL; the copy is made at once and the monitor reports it completed.
- `delta` lists every change since its token, with Graph's `deleted` facet for what was removed, paged by
  `@odata.nextLink` and closed by `@odata.deltaLink`. A token older than `DELTA_TOKEN_LIFETIME` on the run's clock
  is refused 410 `resyncRequired` (this fake's lifetime; Graph does not publish one).
- A file a person holds open (a `MicrosoftSeed.holds` fault, `StoredHold`) refuses every write 423 while held.
- A user's token reaching another user's OneDrive is refused 403 `accessDenied`; an application token reaches all.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from minutehand.adapters.providers.microsoft import docx, subscriptions, tokens, wire
from minutehand.adapters.providers.microsoft.common import (
    GRAPH_JSON,
    GraphRefusal,
    bad_request,
    graph_caller,
    header,
    not_found,
    query,
)
from minutehand.adapters.providers.microsoft.state import (
    COPIES,
    GRAPH,
    PERMISSION_PARENT,
    TOMBSTONES,
    UPLOADS,
    CopyRecord,
    DriveRecord,
    MicrosoftWorld,
    SiteRecord,
    copy_ref,
    drive_ref,
    graph_time,
    item_ref,
    item_text,
    permission_ref,
    site_ref,
    tombstone_ref,
    upload_ref,
)
from minutehand.adapters.providers.microsoft.wire import TokenUse
from minutehand.domain.scenario import AccessRole
from minutehand.domain.world import Actor, EntityKind, GrantSnapshot, Operation
from minutehand.ports.clock import Clock

PAGE_DEFAULT = 200
PAGE_MAX = 999
DELTA_TOKEN_LIFETIME = timedelta(days=30)
MAX_SIMPLE_UPLOAD = 250 * 1024 * 1024
DOWNLOAD_LIFETIME = 3600
DRIVE_ITEM_TYPE = "#Microsoft.Graph.DriveItem"

_SUFFIXES = ("children", "content", "delta", "createUploadSession", "copy", "invite", "createLink", "permissions")


def item_id(drive: str, seq: int) -> str:
    """An item id: Graph's 34-character shape, ordered by creation, the same in every run that reaches it."""
    digest = base64.b32encode(hashlib.sha256(f"{drive}\x1f{seq}".encode()).digest()).decode()[:24]
    return f"01{seq:08d}{digest}"


def seeded_item_id(drive: str, parent: str, name: str) -> str:
    """A seeded item's id: Graph's 34-character shape, named by where it is (its drive, its folder, its name) rather
    than by when seeding reached it, so seeding more into an open world moves no item. Its eight-digit run is zero,
    as no minted item's is, so a seeded item never takes a minted id and lists before every minted one."""
    digest = base64.b32encode(hashlib.sha256(f"seeded\x1f{drive}\x1f{parent}\x1f{name}".encode()).digest()).decode()
    return f"01{0:08d}{digest[:24]}"


def mime_of(name: str) -> str:
    lower = name.lower()
    for ending, mime in (
        (".docx", docx.DOCX),
        (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        (".pdf", "application/pdf"),
        (".txt", "text/plain"),
        (".md", "text/markdown"),
        (".html", "text/html"),
        (".htm", "text/html"),
        (".csv", "text/csv"),
        (".json", "application/json"),
    ):
        if lower.endswith(ending):
            return mime
    return "application/octet-stream"


@dataclass(frozen=True)
class Caller:
    claims: wire.Claims

    @property
    def is_user(self) -> bool:
        return self.claims.oid is not None

    @property
    def identity(self) -> wire.IdentitySet:
        if self.claims.oid is not None:
            return wire.IdentitySet(user=wire.Identity(id=self.claims.oid, displayName=self.claims.name))
        return wire.IdentitySet(application=wire.Identity(id=self.claims.appid))


@dataclass(frozen=True)
class Address:
    """What a files path names: a drive, maybe an item in it, maybe a path below that, maybe an operation."""

    drive: DriveRecord
    item: str | None
    path: str | None
    suffix: str | None
    argument: str | None


class Files:
    def __init__(self, world: MicrosoftWorld, clock: Clock, *, seeding: bool = False) -> None:
        """`seeding`: the items made are a scenario's, named by where they are (`seeded_item_id`), not minted."""
        self._world = world
        self._clock = clock
        self._seeding = seeding

    # ================================================================== addressing

    def _drive_for_user(self, key: str) -> DriveRecord:
        user = self._world.user_by(key)
        if user is None:
            raise not_found(f"users/{key}")
        drive = next((d for d in self._world.drives() if d.owner_id == user.user.id), None)
        if drive is None:
            raise GraphRefusal(404, "itemNotFound", "The user's OneDrive has not been provisioned.")
        return drive

    def address(self, caller: Caller, parts: list[str]) -> Address:
        """`parts` are the path's segments after `/v1.0`, the first naming how the drive is reached."""
        head = parts[0]
        if head == "me" and len(parts) >= 2 and parts[1] == "drive":
            if not caller.is_user:
                raise GraphRefusal(400, "BadRequest", "/me request is only valid with delegated authentication flow.")
            drive, rest = self._drive_for_user(caller.claims.oid or ""), parts[2:]
        elif head == "users" and len(parts) >= 3 and parts[2] == "drive":
            drive, rest = self._drive_for_user(parts[1]), parts[3:]
        elif head == "sites" and len(parts) >= 3 and parts[2] == "drive":
            site = self.site(parts[1])
            found = self._world.drive(site.drive_id)
            if found is None:
                raise not_found(f"sites/{parts[1]}/drive")
            drive, rest = found, parts[3:]
        elif head == "drives" and len(parts) >= 2:
            found = self._world.drive(parts[1])
            if found is None:
                raise GraphRefusal(404, "itemNotFound", "The drive could not be found.")
            drive, rest = found, parts[2:]
        else:
            raise bad_request("Invalid request")
        if caller.is_user and drive.owner_id is not None and drive.owner_id != caller.claims.oid:
            raise GraphRefusal(403, "accessDenied", "Access denied: the drive belongs to another user.")
        return self._below(drive, "/".join(rest))

    @staticmethod
    def _below(drive: DriveRecord, rest: str) -> Address:
        if not rest:
            return Address(drive, None, None, None, None)
        match = re.fullmatch(r"(root|items/([^/:]+))(?::(/[^:]*):?)?(?:/(.*))?", rest)
        if match is None:
            raise bad_request(f"Invalid request: '{rest}' names no item.")
        named = match.group(2)
        # Graph takes `root` as an item id too: `/items/root` is the drive's root.
        item = drive.root_id if match.group(1) == "root" or named == "root" else named
        path = match.group(3)
        tail = match.group(4)
        suffix: str | None = None
        argument: str | None = None
        if tail:
            search = re.fullmatch(r"search\(q='(.*)'\)", tail)
            if search is not None:
                suffix, argument = "search", search.group(1).replace("''", "'")
            else:
                name, _, after = tail.partition("/")
                if name not in _SUFFIXES or (after and name != "permissions"):
                    raise bad_request(f"Unsupported segment '{tail}'.")
                suffix, argument = name, after or None
        return Address(drive, item, path, suffix, argument)

    def site(self, key: str) -> SiteRecord:
        """A site by id, `root`, or `{hostname}:/{server relative path}`."""
        if key == "root":
            found = next(iter(self._world.sites()), None)
        elif ":" in key:
            host, _, path = key.partition(":")
            wanted = path.rstrip(":").rstrip("/").lower()
            found = next(
                (
                    s
                    for s in self._world.sites()
                    if s.site.siteCollection.hostname == host.lower()
                    and s.site.webUrl.lower().endswith(wanted if wanted else s.site.siteCollection.hostname)
                ),
                None,
            )
        else:
            found = self._world.site(key)
        if found is None:
            raise GraphRefusal(404, "itemNotFound", "Requested site could not be found")
        return found

    def _item(self, address: Address) -> wire.StoredItem:
        assert address.item is not None
        stored = self._world.item(address.item)
        if stored is None or stored.item.parentReference.driveId != address.drive.drive.id:
            raise not_found(address.item)
        if address.path is None or address.path in ("", "/"):
            return stored
        for name in [p for p in address.path.split("/") if p]:
            stored = self._child(stored, name)
        return stored

    def _child(self, folder: wire.StoredItem, name: str) -> wire.StoredItem:
        found = self._named(folder, name)
        if found is None:
            raise GraphRefusal(404, "itemNotFound", f"The resource '{name}' could not be found.")
        return found

    def _named(self, folder: wire.StoredItem, name: str) -> wire.StoredItem | None:
        if folder.item.folder is None:
            raise bad_request(f"'{folder.item.name}' is not a folder.")
        return next((c for c in self._world.children(folder.item.id) if c.item.name.lower() == name.lower()), None)

    # ================================================================== serving

    def _path_of(self, stored: wire.StoredItem, drive: DriveRecord) -> str:
        names: list[str] = []
        parent = stored.item.parentReference.id
        while parent is not None and parent != drive.root_id:
            above = self._world.item_any(parent)
            if above is None:
                break
            names.append(above.item.name)
            parent = above.item.parentReference.id
        return f"/drives/{drive.drive.id}/root:" + "".join(f"/{n}" for n in reversed(names))

    def served(self, stored: wire.StoredItem, drive: DriveRecord, *, download: bool) -> wire.DriveItem:
        item = stored.item
        update: dict[str, object] = {}
        if item.folder is not None and item.deleted is None:
            update["folder"] = wire.FolderFacet(childCount=len(self._world.children(item.id)))
        if item.root is None:
            update["parentReference"] = item.parentReference.model_copy(update={"path": self._path_of(stored, drive)})
        if download and item.file is not None and item.deleted is None:
            update["download_url"] = self.download_url(stored, drive)
        return item.model_copy(update=update) if update else item

    def host_of(self, drive: DriveRecord) -> str:
        site = self._world.site(drive.site_id) if drive.site_id is not None else None
        if site is not None:
            return site.site.siteCollection.hostname
        tenant = next(iter(self._world.tenants()), None)
        return (
            tenant.sharepoint_host.replace(".sharepoint.com", "-my.sharepoint.com") if tenant else "my.sharepoint.com"
        )

    def download_url(self, stored: wire.StoredItem, drive: DriveRecord) -> str:
        token, _ = tokens.issued(
            use=TokenUse.ACCESS,
            issuer=tokens.issuer_for(next((t.id for t in self._world.tenants()), "")),
            audience=f"download {stored.item.id}",
            tenant=None,
            app_id=stored.item.id,
            user=None,
            lifetime=DOWNLOAD_LIFETIME,
        )
        return f"https://{self.host_of(drive)}/_layouts/15/download.aspx?UniqueId={stored.item.id}&tempauth={token}"

    def _entity(self, stored: wire.StoredItem, drive: DriveRecord, fields: list[str] | None) -> Response:
        body = wire.dump(self.served(stored, drive, download=True))
        context = f"{GRAPH}/$metadata#drives('{drive.drive.id}')/items/$entity"
        return Response(wire.select(wire.with_context(body, context), fields), media_type=GRAPH_JSON)

    # ================================================================== writes

    def _stamp(self) -> str:
        return graph_time(self._clock.now())

    def _web_url(self, drive: DriveRecord, path: str, name: str) -> str:
        base = drive.drive.webUrl
        inner = path.split("root:", 1)[1] if "root:" in path else ""
        return f"{base}{quote(inner)}/{quote(name)}"

    def new_item(
        self,
        drive: DriveRecord,
        parent: wire.StoredItem,
        name: str,
        *,
        folder: bool,
        content: bytes,
        by: wire.IdentitySet,
        declared: str | None = None,
    ) -> wire.StoredItem:
        """`declared`: the id a scenario's document declares for itself, in place of the one it would be given."""
        made = declared or (
            seeded_item_id(drive.drive.id, parent.item.id, name)
            if self._seeding
            else item_id(drive.drive.id, self._world.next_seq())
        )
        stamp = self._stamp()
        digest = hashlib.sha256(content).hexdigest().upper()
        item = wire.DriveItem(
            id=made,
            name=name,
            eTag=f'"{{{made}}},1"',
            cTag=f'"c:{{{made}}},1"',
            size=len(content),
            createdDateTime=stamp,
            lastModifiedDateTime=stamp,
            webUrl=self._web_url(
                drive,
                self._path_of(parent, drive) + "/" + parent.item.name
                if parent.item.root is None
                else f"/drives/{drive.drive.id}/root:",
                name,
            ),
            createdBy=by,
            lastModifiedBy=by,
            parentReference=wire.ItemReference(
                driveId=drive.drive.id, driveType=drive.drive.driveType, id=parent.item.id
            ),
            file=None if folder else wire.FileFacet(mimeType=mime_of(name), hashes=wire.Hashes(sha256Hash=digest)),
            folder=wire.FolderFacet(childCount=0) if folder else None,
        )
        return wire.StoredItem(item=item, content_b64=None if folder else wire.encoded(content))

    @staticmethod
    def changed(stored: wire.StoredItem, by: wire.IdentitySet, stamp: str, **fields: object) -> wire.StoredItem:
        version = stored.version + 1
        made = stored.item.id
        item = stored.item.model_copy(
            update={
                "eTag": f'"{{{made}}},{version}"',
                "cTag": f'"c:{{{made}}},{version}"' if "size" in fields else stored.item.cTag,
                "lastModifiedDateTime": stamp,
                "lastModifiedBy": by,
                **{k: v for k, v in fields.items() if k != "content"},
            }
        )
        content = fields["content"] if "content" in fields else None
        return stored.model_copy(
            update={
                "item": item,
                "version": version,
                **({"content_b64": wire.encoded(content)} if isinstance(content, bytes) else {}),
            }
        )

    def refuse_locked(self, stored: wire.StoredItem) -> None:
        now = int(self._clock.now().timestamp())
        for hold in self._world.holds():
            if (
                hold.item == stored.item.id
                and hold.from_time <= now
                and (hold.until_time is None or now < hold.until_time)
            ):
                raise GraphRefusal(
                    423,
                    "resourceLocked",
                    f"The resource you are attempting to access is locked by {hold.by}.",
                    deliberate=True,
                )

    def _free_name(self, folder: wire.StoredItem, name: str, behaviour: str) -> tuple[str, wire.StoredItem | None]:
        """The name a new item takes, and the existing item it replaces, by `@microsoft.graph.conflictBehavior`."""
        existing = self._named(folder, name)
        if existing is None:
            return name, None
        if behaviour == "fail":
            raise GraphRefusal(409, "nameAlreadyExists", "The specified item name already exists.")
        if behaviour == "replace":
            return name, existing
        stem, dot, ending = name.rpartition(".")
        stem, ending = (stem, dot + ending) if dot else (name, "")
        n = 1
        while self._named(folder, f"{stem} {n}{ending}") is not None:
            n += 1
        return f"{stem} {n}{ending}", None

    async def notify(self, drive: DriveRecord, stored: wire.StoredItem) -> None:
        await subscriptions.notify(
            self._world,
            self._clock,
            subscriptions.drive_watch(drive.drive.id),
            change="updated",
            odata_type=DRIVE_ITEM_TYPE,
            resource=f"/drives/{drive.drive.id}/root",
            item=stored.item.id,
        )

    def write_content(
        self,
        drive: DriveRecord,
        folder: wire.StoredItem,
        name: str,
        content: bytes,
        *,
        behaviour: str,
        by: wire.IdentitySet,
        actor: Actor,
        declared: str | None = None,
    ) -> tuple[wire.StoredItem, bool]:
        """Create `name` under `folder` with `content`, or replace an existing file's bytes; True when created.
        `declared`: the id a seeded document declares, for one created."""
        if folder.item.folder is None:
            raise bad_request(f"'{folder.item.name}' is not a folder.")
        name, existing = self._free_name(folder, name, behaviour)
        if existing is not None:
            return self.replace_content(existing, content, by=by, actor=actor), False
        made = self.new_item(drive, folder, name, folder=False, content=content, by=by, declared=declared)
        self._world.write_item(made, operation=Operation.CREATE, actor=actor)
        return made, True

    def replace_content(
        self, stored: wire.StoredItem, content: bytes, *, by: wire.IdentitySet, actor: Actor
    ) -> wire.StoredItem:
        if stored.item.folder is not None:
            raise bad_request("A folder has no content.")
        self.refuse_locked(stored)
        file = stored.item.file
        assert file is not None
        updated = self.changed(
            stored,
            by,
            self._stamp(),
            size=len(content),
            content=content,
            file=file.model_copy(
                update={"hashes": wire.Hashes(sha256Hash=hashlib.sha256(content).hexdigest().upper())}
            ),
        )
        self._world.write_item(updated, operation=Operation.UPDATE, actor=actor)
        return updated

    def move(
        self,
        drive: DriveRecord,
        stored: wire.StoredItem,
        *,
        name: str | None,
        parent: str | None,
        by: wire.IdentitySet,
        actor: Actor,
    ) -> wire.StoredItem:
        if stored.item.root is not None:
            raise GraphRefusal(403, "accessDenied", "The root of a drive cannot be renamed or moved.")
        self.refuse_locked(stored)
        fields: dict[str, object] = {}
        target = self._world.item(parent) if parent is not None else None
        if parent is not None:
            if target is None or target.item.folder is None or target.item.parentReference.driveId != drive.drive.id:
                raise GraphRefusal(400, "invalidRequest", "The destination is not a folder in this drive.")
            fields["parentReference"] = stored.item.parentReference.model_copy(update={"id": target.item.id})
        destination = target or self._world.item_any(stored.item.parentReference.id or "")
        if name is not None and name != stored.item.name:
            if destination is not None and self._named(destination, name) is not None:
                raise GraphRefusal(409, "nameAlreadyExists", "The specified item name already exists.")
            fields["name"] = name
        updated = self.changed(stored, by, self._stamp(), **fields)
        self._world.write_item(updated, operation=Operation.UPDATE, actor=actor)
        return updated

    def delete(self, stored: wire.StoredItem, *, by: wire.IdentitySet, actor: Actor) -> None:
        if stored.item.root is not None:
            raise GraphRefusal(403, "accessDenied", "The root of a drive cannot be deleted.")
        self.refuse_locked(stored)
        for child in self._world.children(stored.item.id):
            self.delete(child, by=by, actor=actor)
        self._world.remove(item_ref(stored.item.id), actor=actor, parent=stored.item.parentReference.id)
        drive = stored.item.parentReference.driveId
        tombstone = wire.DriveItem.model_validate(
            {
                **self.changed(stored, by, self._stamp(), deleted=wire.DeletedFacet()).item.model_dump(
                    by_alias=True, exclude_none=True
                ),
                "file": None,
                "folder": None,
            }
        )
        self._world.write(
            tombstone_ref(stored.item.id),
            wire.StoredItem(item=tombstone),
            operation=Operation.CREATE,
            actor=actor,
            parent=TOMBSTONES.format(drive=drive),
        )

    def share(self, stored: wire.StoredItem, email: str, roles: list[str], *, actor: Actor) -> wire.Permission:
        user = self._world.user_by(email)
        permission = wire.Permission(
            id=tokens.derived_trace(f"{stored.item.id} {email.lower()}"),
            roles=roles,
            grantedToV2=wire.SharePointIdentity(
                user=wire.Identity(
                    id=user.user.id if user else email, displayName=user.user.displayName if user else email
                )
            ),
        )
        self._world.write(
            permission_ref(stored.item.id, permission.id),
            permission,
            operation=Operation.CREATE,
            actor=actor,
            parent=PERMISSION_PARENT.format(item=stored.item.id),
            after=GrantSnapshot(
                document=stored.item.name,
                to=(user.reached_as if user is not None else None) or email,
                person=user.person_key if user is not None else None,
                role=_role(roles),
            ),
        )
        return permission

    # ================================================================== the routes

    async def answer(self, request: Request, parts: list[str]) -> Response:
        caller = Caller(graph_caller(request))
        method = request.method
        if parts[0] == "sites":
            return await self._sites(request, caller, parts)
        address = self.address(caller, parts)
        fields = [f for f in (query(request, "$select") or "").split(",") if f] or None
        if address.item is None:
            if method != "GET":
                raise GraphRefusal(405, "methodNotAllowed", "The method is not allowed on a drive.")
            self._world.saw(drive_ref(address.drive.drive.id), Operation.READ)
            body = wire.with_context(wire.dump(address.drive.drive), f"{GRAPH}/$metadata#drives/$entity")
            return Response(wire.select(body, fields), media_type=GRAPH_JSON)
        handlers = self._handlers()
        handler = handlers[(method, address.suffix)] if (method, address.suffix) in handlers else None
        if handler is None:
            raise GraphRefusal(405, "methodNotAllowed", f"{method} is not allowed here.")
        return await handler(request, caller, address, fields)

    Handler = Callable[[Request, Caller, Address, list[str] | None], Awaitable[Response]]

    def _handlers(self) -> dict[tuple[str, str | None], Files.Handler]:
        return {
            ("GET", None): self._get,
            ("PATCH", None): self._patch,
            ("DELETE", None): self._delete,
            ("GET", "children"): self._children,
            ("POST", "children"): self._create_folder,
            ("GET", "content"): self._content,
            ("PUT", "content"): self._put_content,
            ("GET", "delta"): self._delta,
            ("GET", "search"): self._search,
            ("POST", "createUploadSession"): self._upload_session,
            ("POST", "copy"): self._copy,
            ("POST", "invite"): self._invite,
            ("POST", "createLink"): self._create_link,
            ("GET", "permissions"): self._permissions,
            ("DELETE", "permissions"): self._delete_permission,
        }

    async def _get(self, request: Request, caller: Caller, address: Address, fields: list[str] | None) -> Response:
        stored = self._item(address)
        self._world.saw(item_ref(stored.item.id), Operation.READ)
        return self._entity(stored, address.drive, fields)

    async def _patch(self, request: Request, caller: Caller, address: Address, fields: list[str] | None) -> Response:
        stored = self._item(address)
        try:
            asked = wire.read(wire.MoveRequest, await request.body())
        except wire.Unreadable as e:
            raise bad_request(e.message) from e
        parent = asked.parentReference.id if asked.parentReference is not None else None
        updated = self.move(
            address.drive, stored, name=asked.name, parent=parent, by=caller.identity, actor=Actor.AGENT
        )
        await self.notify(address.drive, updated)
        return self._entity(updated, address.drive, None)

    async def _delete(self, request: Request, caller: Caller, address: Address, fields: list[str] | None) -> Response:
        stored = self._item(address)
        self.delete(stored, by=caller.identity, actor=Actor.AGENT)
        await self.notify(address.drive, stored)
        return Response(status_code=204)

    def _page_bounds(self, request: Request) -> tuple[int, int]:
        top = query(request, "$top")
        size = int(top) if top and top.isdigit() and int(top) > 0 else PAGE_DEFAULT
        skip = query(request, "$skiptoken")
        offset = 0
        if skip:
            try:
                offset = int(base64.urlsafe_b64decode(skip + "=" * (-len(skip) % 4)).decode())
            except (binascii.Error, ValueError) as e:
                raise bad_request("The $skiptoken is not valid.") from e
        return min(size, PAGE_MAX), offset

    @staticmethod
    def _skiptoken(offset: int) -> str:
        return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")

    def _paged(
        self, request: Request, items: list[wire.DriveItem], context: str, fields: list[str] | None, link: str
    ) -> Response:
        size, offset = self._page_bounds(request)
        page = items[offset : offset + size]
        more = offset + size < len(items)
        following: str | None = None
        if more:
            kept = [f"$top={size}"] + ([f"$select={','.join(fields)}"] if fields else [])
            following = f"{link}?{'&'.join(kept)}&$skiptoken={self._skiptoken(offset + size)}"
        body = wire.dump(wire.Page[wire.DriveItem](context=context, value=page, next_link=following))
        return Response(wire.select_page(body, fields), media_type=GRAPH_JSON)

    async def _children(self, request: Request, caller: Caller, address: Address, fields: list[str] | None) -> Response:
        folder = self._item(address)
        if folder.item.folder is None:
            raise bad_request(f"'{folder.item.name}' is not a folder.")
        order = query(request, "$orderby")
        children = [self.served(c, address.drive, download=True) for c in self._world.children(folder.item.id)]
        if order:
            key, _, direction = order.partition(" ")
            if key not in ("name", "lastModifiedDateTime", "size"):  # enum-lint: exempt Graph's $orderby property name
                raise bad_request(f"Ordering by '{key}' is not supported.")
            by_name = key == "name"  # enum-lint: exempt Graph's $orderby property name
            children.sort(key=lambda i: str(getattr(i, key)).lower() if by_name else getattr(i, key))
            if direction.lower() == "desc":
                children.reverse()
        self._world.saw(item_ref(folder.item.id), Operation.SEARCH)
        drive = address.drive.drive.id
        link = f"{GRAPH}/drives/{drive}/items/{folder.item.id}/children"
        return self._paged(
            request, children, f"{GRAPH}/$metadata#drives('{drive}')/items('{folder.item.id}')/children", fields, link
        )

    async def _create_folder(
        self, request: Request, caller: Caller, address: Address, fields: list[str] | None
    ) -> Response:
        parent = self._item(address)
        try:
            asked = wire.read(wire.SentItem, await request.body())
        except wire.Unreadable as e:
            raise bad_request(e.message) from e
        if not asked.name:
            raise bad_request("The name of the item is required.")
        if asked.folder is None:
            raise GraphRefusal(400, "invalidRequest", "Only a folder is created this way; upload a file's content.")
        if parent.item.folder is None:
            raise bad_request(f"'{parent.item.name}' is not a folder.")
        name, existing = self._free_name(parent, asked.name, asked.conflict_behavior or "fail")
        if existing is not None:
            self.delete(existing, by=caller.identity, actor=Actor.AGENT)
        made = self.new_item(address.drive, parent, name, folder=True, content=b"", by=caller.identity)
        self._world.write_item(made, operation=Operation.CREATE, actor=Actor.AGENT)
        await self.notify(address.drive, made)
        response = self._entity(made, address.drive, None)
        response.status_code = 201
        return response

    async def _content(self, request: Request, caller: Caller, address: Address, fields: list[str] | None) -> Response:
        stored = self._item(address)
        if stored.item.file is None:
            raise bad_request("A folder has no content to download.")
        return RedirectResponse(self.download_url(stored, address.drive), status_code=302)

    async def _put_content(
        self, request: Request, caller: Caller, address: Address, fields: list[str] | None
    ) -> Response:
        content = await request.body()
        if len(content) > MAX_SIMPLE_UPLOAD:
            raise GraphRefusal(413, "requestTooLarge", "Use an upload session for content over 250 MB.")
        behaviour = query(request, "@microsoft.graph.conflictBehavior") or "replace"
        if address.path:
            base = self._item(Address(address.drive, address.item, None, None, None))
            *folders, name = [p for p in address.path.split("/") if p]
            parent = base
            for folder in folders:
                found = self._named(parent, folder)
                if found is None:
                    found = self.new_item(address.drive, parent, folder, folder=True, content=b"", by=caller.identity)
                    self._world.write_item(found, operation=Operation.CREATE, actor=Actor.AGENT)
                parent = found
            stored, created = self.write_content(
                address.drive, parent, name, content, behaviour=behaviour, by=caller.identity, actor=Actor.AGENT
            )
        else:
            stored, created = (
                self.replace_content(self._item(address), content, by=caller.identity, actor=Actor.AGENT),
                False,
            )
        await self.notify(address.drive, stored)
        response = self._entity(stored, address.drive, None)
        response.status_code = 201 if created else 200
        return response

    # ------------------------------------------------------------------ delta

    def _delta_token(self, seq: int) -> str:
        raw = f"{seq}.{int(self._clock.now().timestamp())}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    def _since(self, token: str) -> int:
        try:
            seq_text, _, stamp_text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode().partition(".")
            seq, stamp = int(seq_text), int(stamp_text)
        except (binascii.Error, ValueError) as e:
            raise bad_request("The delta token is not valid.") from e
        issued = datetime.fromtimestamp(stamp, tz=self._clock.now().tzinfo)
        if self._clock.now() - issued > DELTA_TOKEN_LIFETIME:
            raise GraphRefusal(
                410,
                "resyncRequired",
                "The delta token has expired. Start again without a token and replace what you hold with what is listed.",
            )
        return seq

    async def _delta(self, request: Request, caller: Caller, address: Address, fields: list[str] | None) -> Response:
        if address.item != address.drive.root_id or address.path:
            raise bad_request("Delta is answered for the root of a drive.")
        token = query(request, "token")
        since = self._since(token) if token else 0
        changed: list[tuple[int, wire.DriveItem]] = []
        pending = [address.drive.root_id]
        root = self._world.store.get(item_ref(address.drive.root_id))
        if root is not None and root.seq > since:
            stored_root = wire.parse(wire.StoredItem, root.body)
            changed.append((root.seq, self.served(stored_root, address.drive, download=False)))
        while pending:
            folder = pending.pop()
            for stored in self._world.store.children("microsoft", EntityKind.DOCUMENT, folder, limit=100_000):
                item = wire.parse(wire.StoredItem, stored.body)
                if item.item.folder is not None:
                    pending.append(item.item.id)
                if stored.seq > since:
                    changed.append((stored.seq, self.served(item, address.drive, download=False)))
        if since:
            for stored in self._world.store.children(
                "microsoft", EntityKind.RECORD, TOMBSTONES.format(drive=address.drive.drive.id), limit=100_000
            ):
                if stored.seq > since:
                    changed.append((stored.seq, wire.parse(wire.StoredItem, stored.body).item))
        changed.sort(key=lambda pair: pair[0])
        head = self._world.store.head()
        items = [i for _, i in changed]
        size, offset = self._page_bounds(request)
        page = items[offset : offset + size]
        drive = address.drive.drive.id
        link = f"{GRAPH}/drives/{drive}/root/delta"
        following = deltalink = None
        if offset + size < len(items):
            kept = f"?$top={size}&$skiptoken={self._skiptoken(offset + size)}" + (f"&token={token}" if token else "")
            following = link + kept
        else:
            deltalink = f"{link}?token={self._delta_token(head)}"
        self._world.saw(drive_ref(drive), Operation.SEARCH)
        body = wire.dump(
            wire.Page[wire.DriveItem](
                context=f"{GRAPH}/$metadata#Collection(driveItem)",
                value=page,
                next_link=following,
                delta_link=deltalink,
            )
        )
        return Response(wire.select_page(body, fields), media_type=GRAPH_JSON)

    # ------------------------------------------------------------------ search

    async def _search(self, request: Request, caller: Caller, address: Address, fields: list[str] | None) -> Response:
        base = self._item(address)
        wanted = (address.argument or "").lower()
        found: list[wire.DriveItem] = []
        pending = [base]
        while pending:
            folder = pending.pop()
            for child in self._world.children(folder.item.id):
                if child.item.folder is not None:
                    pending.append(child)
                if wanted and (wanted in child.item.name.lower() or wanted in item_text(child).lower()):
                    found.append(self.served(child, address.drive, download=False))
        found.sort(key=lambda i: i.id)
        self._world.saw(drive_ref(address.drive.drive.id), Operation.SEARCH)
        return self._paged(
            request, found, f"{GRAPH}/$metadata#Collection(driveItem)", fields, str(request.url).split("?")[0]
        )

    # ------------------------------------------------------------------ uploads

    async def _upload_session(
        self, request: Request, caller: Caller, address: Address, fields: list[str] | None
    ) -> Response:
        try:
            asked = wire.read(wire.UploadSessionRequest, await request.body())
        except wire.Unreadable as e:
            raise bad_request(e.message) from e
        behaviour = (asked.item.conflict_behavior if asked.item else None) or "replace"
        if address.path:
            parent = self._item(Address(address.drive, address.item, None, None, None))
            *folders, name = [p for p in address.path.split("/") if p]
            for folder in folders:
                parent = self._child(parent, folder)
            existing = self._named(parent, name)
            refuse = behaviour == "fail"  # enum-lint: exempt Graph's @microsoft.graph.conflictBehavior value
            if existing is not None and refuse:
                raise GraphRefusal(409, "nameAlreadyExists", "The specified item name already exists.")
            if existing is not None:
                self.refuse_locked(existing)
        else:
            existing = self._item(address)
            self.refuse_locked(existing)
            parent_id = existing.item.parentReference.id
            assert parent_id is not None
            found_parent = self._world.item(parent_id)
            assert found_parent is not None
            parent, name = found_parent, existing.item.name
        expires = graph_time(self._clock.now() + timedelta(days=1))
        session = tokens.derived_trace(f"upload {self._world.next_seq()}")
        record = wire.StoredUploadSession(
            session=session,
            drive=address.drive.drive.id,
            parent=parent.item.id,
            name=name,
            existing=existing.item.id if existing is not None and behaviour == "replace" else None,
            conflict=behaviour,
            expires=expires,
            by=caller.identity,
        )
        self._world.write(upload_ref(session), record, operation=Operation.CREATE, actor=Actor.AGENT, parent=UPLOADS)
        token, _ = tokens.issued(
            use=TokenUse.ACCESS,
            issuer=tokens.issuer_for(caller.claims.tid or ""),
            audience=f"upload {session}",
            tenant=caller.claims.tid,
            app_id=caller.claims.appid,
            user=caller.claims.oid,
            lifetime=24 * 3600,
        )
        url = (
            f"https://{self.host_of(address.drive)}/_api/v2.0/drives/{address.drive.drive.id}/items/{parent.item.id}"
            f"/uploadSession?guid='{session}'&tempauth={token}"
        )
        answer = wire.UploadSession(uploadUrl=url, expirationDateTime=expires, nextExpectedRanges=["0-"])
        return Response(wire.dump(answer), media_type=GRAPH_JSON)

    async def upload_fragment(self, request: Request, session: str) -> Response:
        """`PUT` on an upload URL: one `Content-Range` fragment; the last one makes the file."""
        record = self._world.upload(session)
        if record is None:
            raise GraphRefusal(404, "itemNotFound", "The upload session was not found or has expired.")
        if request.method == "DELETE":  # enum-lint: exempt HTTP's method name
            self._world.remove(upload_ref(session), actor=Actor.AGENT, parent=UPLOADS)
            return Response(status_code=204)
        if request.method == "GET":
            return self._session_status(record)
        ranged = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", header(request, "content-range") or "")
        if ranged is None:
            raise GraphRefusal(400, "invalidRange", "A Content-Range header of the form 'bytes a-b/total' is required.")
        first, last, total_text = int(ranged.group(1)), int(ranged.group(2)), ranged.group(3)
        body = await request.body()
        received = wire.b64_bytes(record.received_b64)
        if first != len(received):
            raise GraphRefusal(
                416, "invalidRange", f"The fragment starts at {first}; the next expected byte is {len(received)}."
            )
        if last - first + 1 != len(body):
            raise GraphRefusal(400, "invalidRange", "The Content-Range does not match the length of the fragment.")
        total = int(total_text) if total_text != "*" else None
        received += body
        if total is None or len(received) < total:
            updated = record.model_copy(update={"received_b64": wire.encoded(received), "expected": total})
            self._world.write(
                upload_ref(session), updated, operation=Operation.UPDATE, actor=Actor.AGENT, parent=UPLOADS
            )
            return self._session_status(updated, status=202)
        drive = self._world.drive(record.drive)
        parent = self._world.item(record.parent)
        if drive is None or parent is None:
            raise GraphRefusal(404, "itemNotFound", "The folder the upload was going to no longer exists.")
        if record.existing is not None and (existing := self._world.item(record.existing)) is not None:
            stored, created = self.replace_content(existing, received, by=record.by, actor=Actor.AGENT), False
        else:
            stored, created = self.write_content(
                drive, parent, record.name, received, behaviour=record.conflict, by=record.by, actor=Actor.AGENT
            )
        self._world.remove(upload_ref(session), actor=Actor.AGENT, parent=UPLOADS)
        await self.notify(drive, stored)
        response = self._entity(stored, drive, None)
        response.status_code = 201 if created else 200
        return response

    def _session_status(self, record: wire.StoredUploadSession, status: int = 200) -> Response:
        got = len(wire.b64_bytes(record.received_b64))
        answer = wire.UploadSession(uploadUrl="", expirationDateTime=record.expires, nextExpectedRanges=[f"{got}-"])
        body = wire.dump(answer)
        return Response(body, status_code=status, media_type=GRAPH_JSON)

    # ------------------------------------------------------------------ copy

    async def _copy(self, request: Request, caller: Caller, address: Address, fields: list[str] | None) -> Response:
        source = self._item(address)
        try:
            asked = wire.read(wire.CopyRequest, await request.body())
        except wire.Unreadable as e:
            raise bad_request(e.message) from e
        target_id = (
            asked.parentReference.id
            if asked.parentReference and asked.parentReference.id
            else source.item.parentReference.id
        )
        target_drive = address.drive
        if asked.parentReference and asked.parentReference.driveId:
            found_drive = self._world.drive(asked.parentReference.driveId)
            if found_drive is None:
                raise GraphRefusal(404, "itemNotFound", "The destination drive could not be found.")
            target_drive = found_drive
        target = self._world.item(target_id or "")
        if target is None or target.item.folder is None:
            raise GraphRefusal(400, "invalidRequest", "The destination is not a folder.")
        name = asked.name or source.item.name
        if self._named(target, name) is not None:
            raise GraphRefusal(409, "nameAlreadyExists", "The specified item name already exists.")
        made = self._copy_tree(target_drive, source, target, name, caller.identity)
        operation = tokens.derived_trace(f"copy {made.item.id}")
        self._world.write(
            copy_ref(operation),
            CopyRecord(
                id=operation,
                status=wire.AsyncOperationStatus(status="completed", percentageComplete=100, resourceId=made.item.id),
            ),
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=COPIES,
        )
        await self.notify(target_drive, made)
        monitor = f"https://{self.host_of(address.drive)}/_api/v2.0/monitor/{operation}"
        return Response(status_code=202, headers={"Location": monitor})

    def _copy_tree(
        self, drive: DriveRecord, source: wire.StoredItem, target: wire.StoredItem, name: str, by: wire.IdentitySet
    ) -> wire.StoredItem:
        made = self.new_item(
            drive, target, name, folder=source.item.folder is not None, content=wire.content_of(source), by=by
        )
        self._world.write_item(made, operation=Operation.CREATE, actor=Actor.AGENT)
        for child in self._world.children(source.item.id):
            self._copy_tree(drive, child, made, child.item.name, by)
        return made

    def download(self, item: str) -> Response:
        """The bytes behind a pre-authenticated download URL."""
        stored = self._world.item(item)
        if stored is None or stored.item.file is None:
            raise GraphRefusal(404, "itemNotFound", "The file could not be found.")
        self._world.saw(item_ref(stored.item.id), Operation.READ)
        return Response(
            wire.content_of(stored),
            media_type=stored.item.file.mimeType,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(stored.item.name)}"},
        )

    def monitor(self, operation: str) -> Response:
        record = self._world.copy(operation)
        if record is None:
            raise GraphRefusal(404, "itemNotFound", "The operation could not be found.")
        return Response(wire.dump(record.status), media_type=GRAPH_JSON)

    # ------------------------------------------------------------------ sharing

    async def _invite(self, request: Request, caller: Caller, address: Address, fields: list[str] | None) -> Response:
        stored = self._item(address)
        try:
            asked = wire.read(wire.InviteRequest, await request.body())
        except wire.Unreadable as e:
            raise bad_request(e.message) from e
        if not asked.recipients or not asked.roles:
            raise bad_request("At least one recipient and one role are required.")
        if not set(asked.roles) <= {"read", "write", "owner"}:
            raise bad_request(f"'{','.join(asked.roles)}' is not a valid set of roles.")
        granted = [self.share(stored, r.email, asked.roles, actor=Actor.AGENT) for r in asked.recipients]
        page = wire.Page[wire.Permission](context=f"{GRAPH}/$metadata#Collection(permission)", value=granted)
        return Response(wire.dump(page), media_type=GRAPH_JSON)

    async def _create_link(
        self, request: Request, caller: Caller, address: Address, fields: list[str] | None
    ) -> Response:
        stored = self._item(address)
        try:
            asked = wire.read(wire.CreateLinkRequest, await request.body())
        except wire.Unreadable as e:
            raise bad_request(e.message) from e
        if asked.type not in ("view", "edit", "embed"):  # enum-lint: exempt Graph's sharing link type
            raise bad_request(f"'{asked.type}' is not a valid link type.")
        scope = asked.scope or "organization"
        if scope not in ("anonymous", "organization", "users"):
            raise bad_request(f"'{scope}' is not a valid link scope.")
        share = tokens.derived_trace(f"link {stored.item.id} {asked.type} {scope}")
        permission = wire.Permission(
            id=share,
            roles=["write" if asked.type == "edit" else "read"],  # enum-lint: exempt Graph's sharing link type
            link=wire.SharingLink(
                type=asked.type, scope=scope, webUrl=f"https://{self.host_of(address.drive)}/:w:/s/shared/{share}"
            ),
            shareId=f"u!{share}",
        )
        self._world.write(
            permission_ref(stored.item.id, share),
            permission,
            operation=Operation.CREATE,
            actor=Actor.AGENT,
            parent=PERMISSION_PARENT.format(item=stored.item.id),
            after=GrantSnapshot(
                document=stored.item.name,
                to="anyone" if scope == "anonymous" else scope,  # enum-lint: exempt Graph's link scope
                role=_role(permission.roles),
            ),
        )
        response = Response(wire.dump(permission), status_code=201, media_type=GRAPH_JSON)
        return response

    async def _permissions(
        self, request: Request, caller: Caller, address: Address, fields: list[str] | None
    ) -> Response:
        stored = self._item(address)
        found = self._world.permissions(stored.item.id)
        if address.argument is not None:
            one = next((p for p in found if p.id == address.argument), None)
            if one is None:
                raise not_found(address.argument)
            return Response(wire.dump(one), media_type=GRAPH_JSON)
        page = wire.Page[wire.Permission](context=f"{GRAPH}/$metadata#Collection(permission)", value=found)
        return Response(wire.dump(page), media_type=GRAPH_JSON)

    async def _delete_permission(
        self, request: Request, caller: Caller, address: Address, fields: list[str] | None
    ) -> Response:
        stored = self._item(address)
        if address.argument is None or not any(
            p.id == address.argument for p in self._world.permissions(stored.item.id)
        ):
            raise not_found(address.argument or "permission")
        self._world.remove(
            permission_ref(stored.item.id, address.argument),
            actor=Actor.AGENT,
            parent=PERMISSION_PARENT.format(item=stored.item.id),
        )
        return Response(status_code=204)

    # ------------------------------------------------------------------ sites

    async def _sites(self, request: Request, caller: Caller, parts: list[str]) -> Response:
        fields = [f for f in (query(request, "$select") or "").split(",") if f] or None
        if len(parts) == 1:
            search = query(request, "search")
            if search is None:
                raise bad_request("Listing sites needs a search: /sites?search=")
            wanted = search.lower().strip("*")
            found = [s.site for s in self._world.sites() if not wanted or wanted in s.site.displayName.lower()]
            body = wire.dump(wire.Page[wire.Site](context=f"{GRAPH}/$metadata#sites", value=found))
            return Response(wire.select_page(body, fields), media_type=GRAPH_JSON)
        key = parts[1]
        rest = parts[2:]
        addressed = re.fullmatch(r"([^:/]+):(/[^:]*)?(?::(/.*)?)?", "/".join(parts[1:]))
        if addressed is not None:
            key = f"{addressed.group(1)}:{addressed.group(2) or ''}"
            rest = [p for p in (addressed.group(3) or "").split("/") if p]
        site = self.site(key)
        if rest[:1] == ["drive"]:
            return await self.answer(request, ["drives", site.drive_id, *rest[1:]])
        if not rest and request.method == "GET":
            self._world.saw(site_ref(site.site.id), Operation.READ)
            body = wire.with_context(wire.dump(site.site), f"{GRAPH}/$metadata#sites/$entity")
            return Response(wire.select(body, fields), media_type=GRAPH_JSON)
        if request.method != "GET":
            raise NotImplementedError(f"{request.method} on a site: sites are read only here")
        if rest == ["drives"]:
            drives = [d.drive for d in self._world.drives() if d.site_id == site.site.id]
            body = wire.dump(wire.Page[wire.Drive](context=f"{GRAPH}/$metadata#drives", value=drives))
            return Response(wire.select_page(body, fields), media_type=GRAPH_JSON)
        raise bad_request(f"Unsupported segment '{'/'.join(rest)}'.")


GRAPH_ROLES = {"read": AccessRole.READER, "write": AccessRole.WRITER, "owner": AccessRole.ORGANIZER}
"""What each of Graph's permission roles gives, as every document provider names access."""


def _role(roles: list[str]) -> AccessRole:
    """The most a set of Graph roles gives."""
    order = list(AccessRole)
    return max((GRAPH_ROLES[r] for r in roles if r in GRAPH_ROLES), key=order.index, default=AccessRole.READER)
