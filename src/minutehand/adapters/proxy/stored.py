"""A host declared `store` (`domain.outbound.DeclaredStore`): each call to one of its collections answered from, and
written to, the run's world, so what the agent writes it reads back unchanged.

An item is the JSON object the agent sent, kept with every field and value as sent; Minutehand writes into it only
what its collection declares the API assigns (the id, and stamps from the run's clock), and replaces credential
fields and the declaration's `redact` paths with `[redacted]` before the store sees it, as it does for every kept
body. It lives in the world's log as a `STORED` entity under the declaration's name, listed under the collection's
path as called, so a fork sees the items as they stood at its checkpoint and an assessment counts them
(`stored:`). Nothing here reads a credential: every call is let in.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, unquote, urlsplit

from minutehand.adapters.proxy import capture
from minutehand.adapters.proxy.capture import JSON, Canned
from minutehand.domain.outbound import (
    PLACEHOLDER,
    Collection,
    DeclaredStore,
    IdFormat,
    Stamp,
    StampFormat,
    StampOn,
)
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, Stored, StoredSnapshot
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

_PAGE = 500
"""Items read from the store at a time while listing a collection."""

_IDS = uuid.UUID("6f1c3e2a-58b4-4d0e-9a7d-2c1b0e8f4a35")
"""The namespace a `uuid` id is made in, from the host, the collection's path and the creating event."""


@dataclass(frozen=True)
class Kept:
    """What a call to a collection comes to: the answer, the change it makes to the world (none for a read or a
    refusal), and why it was refused, when it was."""

    answer: Canned
    change: Change | None = None
    refused: str | None = None


@dataclass(frozen=True)
class _Found:
    """The collection a path is under, the path of that collection as called, and the item's id when the path
    names one."""

    collection: Collection
    path: str
    id: str | None


def _pattern(path: str) -> str:
    return "".join("[^/]+" if PLACEHOLDER.fullmatch(p) else re.escape(p) for p in re.split(r"(\{[^}]*\})", path))


def find(declaration: DeclaredStore, path: str) -> _Found | None:
    """The collection `path` (without its query) lists, else the one whose item it names: every collection is
    tried as a listing before any as an item, so `/v1/contacts/search` declared as a collection is not an item of
    `/v1/contacts`."""
    bare = urlsplit(path).path
    called = bare.rstrip("/") or "/"
    for collection in declaration.collections:
        if re.fullmatch(_pattern(collection.path), called):
            return _Found(collection, called, None)
    for collection in declaration.collections:
        matched = re.fullmatch(f"({_pattern(collection.path)})/([^/]+)", called)
        if matched is not None:
            return _Found(collection, matched.group(1), unquote(matched.group(2)))
    return None


def answer(
    declaration: DeclaredStore,
    found: _Found,
    method: str,
    path: str,
    body: str | None,
    *,
    store: Store,
    clock: Clock,
    seq: int,
) -> Kept | None:
    """The answer a collection gives the call, and what it writes, with `seq` the event the write will be; None
    when the collection does not take the method (a DELETE of the whole collection, a POST to an item), which the
    host's own answer then gives."""
    verb: str = method.upper()
    if found.id is None and verb == "GET":
        return _list(declaration, found, path, store)
    if found.id is None and verb == "POST":
        return _create(declaration, found, body, store=store, clock=clock, seq=seq)
    if found.id is None or verb not in ("GET", "PUT", "PATCH", "DELETE"):
        return None
    ref = _ref(declaration, found.path, found.id)
    held = store.get(ref)
    if held is None:
        return _refused(404, f"no {found.collection.key} item {found.id} is stored at {found.path}", declaration)
    if verb == "GET":
        return Kept(_json(200, held.body))
    if verb == "DELETE":
        gone = Change(
            entity=ref,
            operation=Operation.DELETE,
            actor=Actor.AGENT,
            parent=found.path,
            after=_snapshot(declaration, found, found.id, None),
        )
        return Kept(Canned(status=found.collection.deleted_status, headers={}, body=b""), gone)
    sent = _object(declaration, found.collection, body)
    if isinstance(sent, Kept):
        return sent
    before: dict[str, object] = json.loads(held.body)
    item = sent if verb == "PUT" else {**before, **sent}
    _keep_assigned(item, before, found.collection)
    _stamp(item, found.collection.stamps, clock, created=False)
    text = json.dumps(item, ensure_ascii=False)
    changed = Change(
        entity=ref,
        operation=Operation.UPDATE,
        actor=Actor.AGENT,
        body=text,
        parent=found.path,
        after=_snapshot(declaration, found, found.id, text),
    )
    return Kept(_json(200, text), changed)


def _create(
    declaration: DeclaredStore, found: _Found, body: str | None, *, store: Store, clock: Clock, seq: int
) -> Kept:
    collection = found.collection
    sent = _object(declaration, collection, body)
    if isinstance(sent, Kept):
        return sent
    made = collection.id.format
    value: object
    if made is IdFormat.SENT:
        given = [v for v in capture.values_at(sent, collection.id.at) if isinstance(v, str | int)]
        if not given or isinstance(given[0], bool) or given[0] == "":
            return _refused(
                400, f"a {collection.key} item is created with its own id at {collection.id.at}", declaration
            )
        value = given[0]
    elif made is IdFormat.INTEGER:
        value = seq
    elif made is IdFormat.PREFIXED:
        value = f"{collection.id.prefix}{seq}"
    else:
        value = str(uuid.uuid5(_IDS, f"{declaration.host}{found.path}#{seq}"))
    item_id = str(value)
    ref = _ref(declaration, found.path, item_id)
    if store.get(ref) is not None:
        return _refused(409, f"a {collection.key} item {item_id} is already stored at {found.path}", declaration)
    if made is not IdFormat.SENT:
        _put(sent, collection.id.at, value)
    _stamp(sent, collection.stamps, clock, created=True)
    text = json.dumps(sent, ensure_ascii=False)
    created = Change(
        entity=ref,
        operation=Operation.CREATE,
        actor=Actor.AGENT,
        body=text,
        parent=found.path,
        after=_snapshot(declaration, found, item_id, text),
    )
    return Kept(_json(collection.created_status, text), created)


def _list(declaration: DeclaredStore, found: _Found, path: str, store: Store) -> Kept:
    """The collection's items in the order they were created, a page of them when the call or the listing says,
    in the listing's envelope."""
    listing = found.collection.listing
    held: list[Stored] = []
    after: str | None = None
    while True:
        page = store.children(declaration.key, EntityKind.STORED, found.path, after=after, limit=_PAGE)
        held += page
        if len(page) < _PAGE:
            break
        after = page[-1].entity.external_id
    ordered = sorted(held, key=lambda s: store.versions(s.entity)[0].seq)
    ids = [s.entity.external_id[len(found.path) + 1 :] for s in ordered]
    query = dict(parse_qsl(urlsplit(path).query, keep_blank_values=True))
    start = 0
    if listing.cursor_param is not None and listing.cursor_param in query and query[listing.cursor_param]:
        cursor = query[listing.cursor_param]
        if cursor not in ids:
            return _refused(400, f"{listing.cursor_param}={cursor} names no {found.collection.key} item", declaration)
        start = ids.index(cursor) + 1
    limit = len(ordered)
    if listing.limit_param is not None:
        asked = query[listing.limit_param] if listing.limit_param in query else str(listing.default_limit)
        if not asked.isdigit() or int(asked) < 1:
            return _refused(400, f"{listing.limit_param}={asked} is not a whole number of items", declaration)
        limit = int(asked)
    shown = ordered[start : start + limit]
    if listing.items_at is None:
        return Kept(_json(200, "[" + ", ".join(s.body for s in shown) + "]"))
    envelope: dict[str, object] = json.loads(json.dumps(listing.envelope))
    _put(envelope, listing.items_at, [json.loads(s.body) for s in shown])
    if listing.next_at is not None and start + limit < len(ordered):
        _put(envelope, listing.next_at, ids[start + limit - 1])
    return Kept(_json(200, json.dumps(envelope, ensure_ascii=False)))


def _object(declaration: DeclaredStore, collection: Collection, body: str | None) -> dict[str, object] | Kept:
    """The item the agent sent, its credential fields and declared `redact` paths replaced; refused when it is not
    a JSON object."""
    try:
        parsed: object = json.loads(capture.redacted(body, JSON, declaration.redact)) if body else None
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        return _refused(400, f"a {collection.key} item is a JSON object, and this body is not one", declaration)
    return {str(k): v for k, v in parsed.items()}


def _keep_assigned(item: dict[str, object], before: dict[str, object], collection: Collection) -> None:
    """What the API assigned stays as it was: the id, and each stamp written on create."""
    kept = [collection.id.at, *(s.at for s in collection.stamps if s.on is StampOn.CREATE)]
    for at in kept:
        was = capture.values_at(before, at)
        if was:
            _put(item, at, was[0])


def _stamp(item: dict[str, object], stamps: list[Stamp], clock: Clock, *, created: bool) -> None:
    now = clock.now().astimezone(UTC)
    for stamp in stamps:
        if created or stamp.on is StampOn.WRITE:
            _put(item, stamp.at, _moment(now, stamp.format))


def _moment(now: datetime, shape: StampFormat) -> object:
    if shape is StampFormat.EPOCH_SECONDS:
        return int(now.timestamp())
    if shape is StampFormat.EPOCH_MILLISECONDS:
        return int(now.timestamp() * 1000)
    return now.isoformat().replace("+00:00", "Z")


def _put(holder: dict[str, object], at: str, value: object) -> None:
    """`value` at the dotted keys `at`, each object on the way made when it is missing or not an object."""
    *parents, last = at.split(".")
    here = holder
    for key in parents:
        following = here[key] if key in here else None
        if not isinstance(following, dict):
            following = {}
            here[key] = following
        here = following
    here[last] = value


def _ref(declaration: DeclaredStore, collection_path: str, item_id: str) -> EntityRef:
    return EntityRef(provider=declaration.key, kind=EntityKind.STORED, external_id=f"{collection_path}/{item_id}")


def _snapshot(declaration: DeclaredStore, found: _Found, item_id: str, text: str | None) -> StoredSnapshot:
    return StoredSnapshot(
        host=declaration.host, collection=found.collection.key, path=found.path, id=item_id, item=text
    )


def _json(status: int, text: str) -> Canned:
    return Canned(status=status, headers={"content-type": JSON}, body=text.encode("utf-8"))


def _refused(status: int, why: str, declaration: DeclaredStore) -> Kept:
    body = json.dumps({"error": why, "host": declaration.host}, ensure_ascii=False)
    return Kept(_json(status, body), refused=why)
