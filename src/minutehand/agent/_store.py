"""The store's machinery: the adapter interface, the two adapters shipped, Minutehand's own backend, and the store
itself. `minutehand.agent.store` is the public face of it."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol, TypeVar

from minutehand.agent import _wire

DEFAULT = "default"
"""The collection a key lives in when none is named."""

COLLECTION = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
KEY_LIMIT = 1024

T = TypeVar("T")


@dataclass(frozen=True)
class Put:
    """Write `value` (JSON text) under `key` in `collection`, replacing what was there. With `version`, only when no
    version the key holds is newer (an equal one lands)."""

    collection: str
    key: str
    value: str
    version: int | None = None


@dataclass(frozen=True)
class Delete:
    """Remove `key` from `collection`; nothing happens when it is not there. With `version`, only when no version
    the key holds is newer, and the key keeps the version, so an older put arriving later does not bring it back."""

    collection: str
    key: str
    version: int | None = None


Write = Put | Delete


def lands(held: int | None, version: int | None) -> bool:
    """Whether a write carrying `version` lands on a key holding `held`: one with no version always does; one with
    a version does unless the key holds a newer one."""
    return version is None or held is None or held <= version


class Backend(Protocol):
    """Where the agent's memory lives in production. Three methods over JSON text; implement them over any database
    to keep the agent's memory there (`docs/agent-contract.md`, "Writing an adapter").

    Values cross as JSON text, already serialised: an adapter stores and returns the string unchanged."""

    def get(self, collection: str, key: str) -> str | None:
        """The value under `key`, or None when there is none."""
        ...

    def scan(self, collection: str, prefix: str) -> list[tuple[str, str]]:
        """Every (key, value) whose key starts with `prefix`, ordered by key ("" is every key)."""
        ...

    def write(self, writes: Sequence[Write]) -> list[bool]:
        """Apply every write that lands, in order, all or none: a reader never sees some of them without the rest.
        A write with a `version` lands only when no version the key holds is newer, decided in the same
        transaction; a delete with a version keeps it. Answers, for each write in order, whether it landed."""
        ...


class NotConfigured(RuntimeError):
    """The store was used in production before `configure` named where it lives."""


# -- adapters --------------------------------------------------------------------------------------------------------


class MemoryBackend:
    """The memory in this process: gone when it exits. For tests, and for an agent whose memory may be lost."""

    def __init__(self) -> None:
        self._held: dict[tuple[str, str], str] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def get(self, collection: str, key: str) -> str | None:
        with self._lock:
            return self._held.get((collection, key))

    def scan(self, collection: str, prefix: str) -> list[tuple[str, str]]:
        with self._lock:
            found = [(k, v) for (c, k), v in self._held.items() if c == collection and k.startswith(prefix)]
        return sorted(found)

    def write(self, writes: Sequence[Write]) -> list[bool]:
        with self._lock:
            applied: list[bool] = []
            for one in writes:
                key = (one.collection, one.key)
                landed = lands(self._versions.get(key), one.version)
                applied.append(landed)
                if not landed:
                    continue
                if one.version is not None:
                    self._versions[key] = one.version
                if isinstance(one, Put):
                    self._held[key] = one.value
                else:
                    self._held.pop(key, None)
            return applied


SCHEMA = """CREATE TABLE IF NOT EXISTS minutehand_store(
  collection TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (collection, key))"""
VERSIONS = """CREATE TABLE IF NOT EXISTS minutehand_store_version(
  collection TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY (collection, key))"""
"""Each key's version, beside its value, so a delete keeps it; a file written before versions existed gains the table
the first time it is opened."""


class SqliteBackend:
    """A SQLite file, shared by every process of the agent that opens the same path (write-ahead logging, a busy
    timeout, and each batch in one immediate transaction). The file is opened at the first call, never before, so
    an agent played by Minutehand, which never calls it, never touches it."""

    def __init__(self, path: str | Path, *, timeout: float = 30.0) -> None:
        self.path = Path(path)
        self._timeout = timeout
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        if self._db is None:
            db = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None, check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(SCHEMA)
            db.execute(VERSIONS)
            self._db = db
        return self._db

    def get(self, collection: str, key: str) -> str | None:
        with self._lock:
            row = (
                self._open()
                .execute("SELECT value FROM minutehand_store WHERE collection=? AND key=?", (collection, key))
                .fetchone()
            )
        return None if row is None else str(row[0])

    def scan(self, collection: str, prefix: str) -> list[tuple[str, str]]:
        with self._lock:
            rows = (
                self._open()
                .execute(
                    "SELECT key, value FROM minutehand_store WHERE collection=? AND key>=? "
                    "AND substr(key, 1, length(?))=? ORDER BY key",
                    (collection, prefix, prefix, prefix),
                )
                .fetchall()
            )
        return [(str(k), str(v)) for k, v in rows]

    def write(self, writes: Sequence[Write]) -> list[bool]:
        with self._lock:
            db = self._open()
            db.execute("BEGIN IMMEDIATE")
            applied: list[bool] = []
            try:
                for one in writes:
                    row = db.execute(
                        "SELECT version FROM minutehand_store_version WHERE collection=? AND key=?",
                        (one.collection, one.key),
                    ).fetchone()
                    landed = lands(None if row is None else int(row[0]), one.version)
                    applied.append(landed)
                    if not landed:
                        continue
                    if one.version is not None:
                        db.execute(
                            "INSERT INTO minutehand_store_version(collection, key, version) VALUES (?, ?, ?) "
                            "ON CONFLICT(collection, key) DO UPDATE SET version=excluded.version",
                            (one.collection, one.key, one.version),
                        )
                    if isinstance(one, Put):
                        db.execute(
                            "INSERT INTO minutehand_store(collection, key, value) VALUES (?, ?, ?) "
                            "ON CONFLICT(collection, key) DO UPDATE SET value=excluded.value",
                            (one.collection, one.key, one.value),
                        )
                    else:
                        db.execute(
                            "DELETE FROM minutehand_store WHERE collection=? AND key=?", (one.collection, one.key)
                        )
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
            return applied

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


class MinutehandBackend:
    """The run's own memory, reached over HTTP (`_wire`). What the agent writes is recorded in the run's log as the
    agent's, in the wake it was written in; what it reads is answered from the run as it stands, which in a fork is
    the parent's memory up to the checkpoint and the fork's own after it."""

    def get(self, collection: str, key: str) -> str | None:
        answer = _object(_wire.call("store", {"op": "get", "collection": collection, "key": key}))
        if "found" not in answer or answer["found"] is not True:
            return None
        return _canonical(answer["value"] if "value" in answer else None)

    def scan(self, collection: str, prefix: str) -> list[tuple[str, str]]:
        answer = _object(_wire.call("store", {"op": "list", "collection": collection, "prefix": prefix}))
        items = answer["items"] if "items" in answer else None
        if not isinstance(items, list):
            raise _wire.MinutehandRefused(f"Minutehand answered a list without items: {answer!r}")
        found: list[tuple[str, str]] = []
        for item in items:
            one = _object(item)
            found.append((str(one["key"]), _canonical(one["value"] if "value" in one else None)))
        return found

    def write(self, writes: Sequence[Write]) -> list[bool]:
        said: list[dict[str, object]] = []
        for one in writes:
            if isinstance(one, Put):
                said.append({"op": "put", "collection": one.collection, "key": one.key, "value": json.loads(one.value)})
            else:
                said.append({"op": "delete", "collection": one.collection, "key": one.key})
            if one.version is not None:
                said[-1]["version"] = one.version
        answer = _object(_wire.call("store", {"op": "write", "writes": said}))
        applied = answer["applied"] if "applied" in answer else None
        if (
            not isinstance(applied, list)
            or len(applied) != len(writes)
            or not all(isinstance(a, bool) for a in applied)
        ):
            raise _wire.MinutehandRefused(f"Minutehand answered a write without saying which landed: {answer!r}")
        return [a is True for a in applied]


def _object(answer: object) -> dict[str, object]:
    if not isinstance(answer, dict):
        raise _wire.MinutehandRefused(f"Minutehand answered something that is not a JSON object: {answer!r}")
    return {str(k): v for k, v in answer.items()}


def _canonical(value: object) -> str:
    """One spelling of a JSON value: keys sorted, no spaces. Two writes of equal values store equal text."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ValueError(f"the store keeps JSON values (objects, lists, strings, numbers, booleans, null): {e}") from e


# -- the store -------------------------------------------------------------------------------------------------------


class _Configured:
    """Where the production backend is kept, shared by every `Store` that names none of its own."""

    backend: Backend | None = None


_MINUTEHAND = MinutehandBackend()


class Store:
    """The agent's memory: JSON values under string keys, in named collections.

    Under Minutehand (MINUTEHAND_ON set) every call goes to the run, whatever backend the store was given; otherwise
    to `backend`, or the one `configure` named. Reads inside a `batch` do not see the batch's own writes."""

    def __init__(self, collection: str = DEFAULT, *, backend: Backend | None = None) -> None:
        if not COLLECTION.match(collection):
            raise ValueError(f"a collection is named by 1 to 64 letters, digits, '_', '.' or '-', not {collection!r}")
        self.name = collection
        self._backend = backend

    def collection(self, name: str) -> Store:
        """The same store's collection `name`: its keys are its own, apart from every other collection's."""
        return Store(name, backend=self._backend)

    def _where(self) -> Backend:
        if _wire.on():
            return _MINUTEHAND
        backend = self._backend or _Configured.backend
        if backend is None:
            raise NotConfigured(
                "the store has no backend: call minutehand.agent.store.configure(...) once at start with "
                "store.SqliteBackend(path), store.MemoryBackend(), or an adapter of your own (docs/agent-contract.md)"
            )
        return backend

    def get(self, key: str, default: object = None) -> object:
        """The value under `key`, or `default` when there is none."""
        found = self._where().get(self.name, _key(key))
        return default if found is None else json.loads(found)

    def put(self, key: str, value: object, *, version: int | None = None) -> bool:
        """Keep `value`, any JSON value, under `key`, replacing what was there. With `version`, a JSON integer (a
        row's version, or its update time in microseconds), only when no version the key holds is newer: two
        processes mirroring the same record after their own commits keep the newer, whichever arrives last. Answers
        whether it landed."""
        one = Put(self.name, _key(key), _canonical(value), _version(version))
        [landed] = _applied(self._where().write([one]), [one])
        return landed

    def delete(self, key: str, *, version: int | None = None) -> bool:
        """Remove `key`. With `version`, only when no version the key holds is newer; the key keeps the version, so
        an older put arriving later does not bring it back. Answers whether it landed."""
        one = Delete(self.name, _key(key), _version(version))
        [landed] = _applied(self._where().write([one]), [one])
        return landed

    def list(self, prefix: str = "") -> list[tuple[str, object]]:
        """Every (key, value) whose key starts with `prefix`, ordered by key."""
        return [(k, json.loads(v)) for k, v in self._where().scan(self.name, prefix)]

    def query(self, prefix: str = "", where: Mapping[str, object] | None = None) -> list[tuple[str, object]]:
        """Every (key, value) under `prefix` whose value is an object with each field of `where` equal to the value
        given there; a field may be a dotted path into nested objects (`"venue.city"`)."""
        wanted = dict(where or {})
        return [(k, v) for k, v in self.list(prefix) if all(_field(v, f) == (True, w) for f, w in wanted.items())]

    def batch(self) -> Batch:
        """Writes made through it are applied together, all or none, when the `with` block ends without raising."""
        return Batch(self)

    async def aget(self, key: str, default: object = None) -> object:
        return await _thread(lambda: self.get(key, default))

    async def aput(self, key: str, value: object, *, version: int | None = None) -> bool:
        return await _thread(lambda: self.put(key, value, version=version))

    async def adelete(self, key: str, *, version: int | None = None) -> bool:
        return await _thread(lambda: self.delete(key, version=version))

    async def alist(self, prefix: str = "") -> list[tuple[str, object]]:
        return await _thread(lambda: self.list(prefix))

    async def aquery(self, prefix: str = "", where: Mapping[str, object] | None = None) -> list[tuple[str, object]]:
        return await _thread(lambda: self.query(prefix, where))


class Batch:
    """Writes held until the block ends, then applied as one. `with store.batch() as b:` or `async with`."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._writes: list[Write] = []
        self.applied: list[bool] = []
        """Once the block has ended: for each write in order, whether it landed."""

    def put(self, key: str, value: object, *, collection: str | None = None, version: int | None = None) -> None:
        self._writes.append(Put(self._in(collection), _key(key), _canonical(value), _version(version)))

    def delete(self, key: str, *, collection: str | None = None, version: int | None = None) -> None:
        self._writes.append(Delete(self._in(collection), _key(key), _version(version)))

    def _in(self, collection: str | None) -> str:
        return self._store.name if collection is None else Store(collection).name

    def commit(self) -> list[bool]:
        writes, self._writes = self._writes, []
        self.applied = _applied(self._store._where().write(writes), writes) if writes else []  # pyright: ignore[reportPrivateUsage]
        return self.applied

    def __enter__(self) -> Batch:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        if kind is None:
            self.commit()

    async def __aenter__(self) -> Batch:
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None
    ) -> None:
        if kind is None:
            await _thread(self.commit)


def configure(backend: Backend) -> None:
    """Where the agent's memory lives in production, for every store that names no backend of its own. Ignored while
    MINUTEHAND_ON is set: the run's memory is used instead, and `backend` is never called."""
    _Configured.backend = backend


def _version(version: object) -> int | None:
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        raise ValueError(
            f"a write's version is an integer (a row's version, or its update time in microseconds), not {version!r}: "
            "an ISO 8601 instant or any other text is refused, since two writes in one millisecond would tie"
        )
    return version


def _applied(answer: object, writes: Sequence[Write]) -> list[bool]:
    """What a backend's `write` answered, held to the contract: one bool per write. An adapter written before
    versions existed answers None, and is refused by name rather than read as every write landing."""
    if not isinstance(answer, list) or len(answer) != len(writes) or not all(isinstance(a, bool) for a in answer):
        raise TypeError(
            f"a backend's write answers one bool per write, whether it landed, not {answer!r}: an adapter written "
            "before versions existed returns None, and one that keeps no versions must refuse a write that carries "
            'one rather than land it unchecked (docs/agent-contract.md, "Writing an adapter")'
        )
    return [a is True for a in answer]


def _key(key: str) -> str:
    if not key or len(key) > KEY_LIMIT:
        raise ValueError(f"a key is a string of 1 to {KEY_LIMIT} characters, not {key!r}")
    return key


def _field(value: object, path: str) -> tuple[bool, object]:
    """(True, the value at the dotted `path`), or (False, None) when it is not there."""
    here = value
    for part in path.split("."):
        if not isinstance(here, dict) or part not in here:
            return False, None
        here = here[part]
    return True, here


async def _thread(work: Callable[[], T]) -> T:
    import asyncio  # only an agent that awaits the store pays for it

    return await asyncio.to_thread(work)
