"""The world as an append-only SQLite log.

One file holds a root run and every fork of it. A run reads its own rows plus its
ancestors' rows up to the sequence number each fork was taken at, so a fork copies
nothing.

A body is kept once. Text shorter than `INLINE_LIMIT` bytes stays in the row that carries it; anything longer is
kept in `content` under the SHA-256 of its UTF-8 bytes, compressed when that makes it smaller, and the row holds
the hash. Entity versions, event snapshots, request and response bodies and the string values of span
attributes all go through the same door (`_keep`), so the same bytes written by any of them, in any run of the
file, are one row. Every read puts the text back exactly as it was written.

The agent's memory (`minutehand.agent.store`) is entity versions like any other (`EntityKind.MEMORY`), so it needs
nothing of its own here: a fork reads it as of its seq as it reads the world.
"""

from __future__ import annotations

import functools
import hashlib
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Concatenate

import zstandard
from pydantic import TypeAdapter

from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import ProviderKey
from minutehand.domain.storage import Freed, RunUsage
from minutehand.domain.telemetry import (
    ArrayValue,
    AttributeValue,
    ForwardFailure,
    MapValue,
    Placement,
    ReceivedSpan,
    Signal,
    SpanSource,
    StoredSpan,
    StringValue,
)
from minutehand.domain.world import (
    Actor,
    CallBegan,
    Change,
    EntityKind,
    EntityRef,
    Exchange,
    Operation,
    RecordedCall,
    Snapshot,
    Stored,
    WorldEvent,
)
from minutehand.ports.clock import Clock

_SNAPSHOT = TypeAdapter(Snapshot)


class _Edge(StrEnum):
    BEGAN = "began"
    ENDED = "ended"


class Codec(StrEnum):
    """How a stored body's bytes are held: as written, or zstd-compressed."""

    RAW = "raw"
    ZSTD = "zstd"


def _locked[**P, R](method: Callable[Concatenate[SqliteStore, P], R]) -> Callable[Concatenate[SqliteStore, P], R]:
    """Run a store method under the store's lock, so it is safe from any thread; one that raises part way through
    a write leaves nothing of it, and no transaction open to be committed by the next write."""

    @functools.wraps(method)
    def inner(self: SqliteStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            try:
                return method(self, *args, **kwargs)
            except BaseException:
                if self._db.in_transaction:
                    self._db.rollback()
                raise

    return inner


SCHEMA_VERSION = 8
"""Stamped into the file as SQLite's user_version. A file with another version is refused, not guessed at.
5: an exchange may carry `captured` and a message `answerable`, which a reader of version 4 would refuse row by
row; refused here as a whole file instead.
6: a body of `INLINE_LIMIT` bytes or more is a hash into `content`, and the agent's snapshots are manifests into
the pool beside the file; a version 5 file holds every body inline and its snapshots as plain directories.
7: an exchange's body that is not UTF-8 text is kept as its bytes in `content` (`request_binary`,
`response_binary`); a version 6 file has no such columns, and its calls with such a body were never recorded.
8: the agent's snapshots and their pool are gone, with the state hooks that wrote them; a version 7 file's
checkpoints name snapshots this version cannot restore."""

_UNKEPT_IN_ROW = {"request_body", "response_body", "request_bytes", "response_bytes"}
"""Kept in columns of their own, not in an exchange's JSON. `inbox_call` is left out of it too when None, so a row
of a run with no inbox reads as it did before inboxes existed."""

INLINE_LIMIT = 512
"""Bytes of UTF-8 below which a body stays in its own row. Measured on a chatty Slack run (docs/design.md, "Storage"):
a body under 512 bytes compresses to about its own size and a stored body costs about 110 bytes in hashes and
index, so moving it out saves nothing unless it repeats; from 512 bytes up a body compresses to about half, so
even one that never repeats costs less in `content` than inline."""

LOG_LIMIT = 1024 * 1024
"""Bytes the write-ahead log is cut back to after each checkpoint (`journal_size_limit`). Without it the log
keeps the size of the largest transaction it ever held, e.g. one 64 MiB body, for as long as the file is open."""

_BUSY_SECONDS = 60.0
"""How long a connection waits for another's write lock."""

_REFERENCES = """
  SELECT run_id, body_ref AS hash FROM entity_version WHERE body_ref IS NOT NULL
  UNION ALL SELECT run_id, after_ref FROM event WHERE after_ref IS NOT NULL
  UNION ALL SELECT run_id, request_ref FROM exchange WHERE request_ref IS NOT NULL
  UNION ALL SELECT run_id, response_ref FROM exchange WHERE response_ref IS NOT NULL
  UNION ALL SELECT run_id, ref FROM span_body"""
"""Every row that refers to a stored body, with the run it belongs to: the one list a sweep and a run's size read."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run(
  run_id TEXT PRIMARY KEY, parent TEXT REFERENCES run(run_id), forked_at INTEGER, forked_calls INTEGER,
  forked_wake INTEGER);
CREATE TABLE IF NOT EXISTS content(
  hash BLOB PRIMARY KEY, size INTEGER NOT NULL, codec TEXT NOT NULL, stored BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(
  run_id TEXT NOT NULL, seq INTEGER NOT NULL, wake INTEGER NOT NULL,
  sim_time TEXT NOT NULL, wall_time TEXT NOT NULL, actor TEXT NOT NULL, operation TEXT NOT NULL,
  provider TEXT NOT NULL, kind TEXT NOT NULL, external_id TEXT NOT NULL, after TEXT, after_ref BLOB,
  PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS entity_version(
  run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  provider TEXT NOT NULL, kind TEXT NOT NULL, external_id TEXT NOT NULL,
  parent TEXT, body TEXT, body_ref BLOB, sim_time TEXT NOT NULL,
  PRIMARY KEY (run_id, seq));
CREATE INDEX IF NOT EXISTS entity_lookup ON entity_version(provider, kind, external_id, seq);
CREATE INDEX IF NOT EXISTS entity_listing ON entity_version(provider, kind, parent, external_id);
CREATE TABLE IF NOT EXISTS exchange(
  run_id TEXT NOT NULL, position INTEGER NOT NULL, first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL,
  provider TEXT, wake INTEGER NOT NULL, sim_time TEXT NOT NULL, exchange TEXT NOT NULL,
  request_body TEXT, request_ref BLOB, response_body TEXT, response_ref BLOB,
  request_binary INTEGER NOT NULL DEFAULT 0, response_binary INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, position));
CREATE TABLE IF NOT EXISTS reply(run_id TEXT NOT NULL, position INTEGER NOT NULL, reply TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS span(
  run_id TEXT NOT NULL, position INTEGER NOT NULL, after_seq INTEGER NOT NULL, wake INTEGER NOT NULL,
  sim_time TEXT NOT NULL, source TEXT NOT NULL, trace_id TEXT NOT NULL, span TEXT NOT NULL,
  arrived_wake INTEGER NOT NULL, placed_by TEXT NOT NULL,
  PRIMARY KEY (run_id, position));
CREATE TABLE IF NOT EXISTS span_body(
  run_id TEXT NOT NULL, position INTEGER NOT NULL, value INTEGER NOT NULL, ref BLOB NOT NULL,
  PRIMARY KEY (run_id, position, value));
CREATE TABLE IF NOT EXISTS wake_edge(
  run_id TEXT NOT NULL, wake INTEGER NOT NULL, edge TEXT NOT NULL, wall_time TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS span_trace ON span(trace_id);
CREATE TABLE IF NOT EXISTS forward_failure(
  run_id TEXT NOT NULL, position INTEGER NOT NULL, failure TEXT NOT NULL, PRIMARY KEY (run_id, position));
"""

_RUN_TABLES = (
    "event",
    "entity_version",
    "exchange",
    "reply",
    "span",
    "span_body",
    "wake_edge",
    "forward_failure",
)
"""Every table whose rows belong to one run, which discarding the run empties of them; `run` itself last."""

_ROW_BYTES = {
    "event": "length(sim_time)+length(wall_time)+length(actor)+length(operation)+length(provider)+length(kind)"
    "+length(external_id)+COALESCE(length(after),0)+COALESCE(length(after_ref),0)+16",
    "entity_version": "length(provider)+length(kind)+length(external_id)+COALESCE(length(parent),0)"
    "+COALESCE(length(body),0)+COALESCE(length(body_ref),0)+length(sim_time)+8",
    "exchange": "length(exchange)+COALESCE(length(request_body),0)+COALESCE(length(request_ref),0)"
    "+COALESCE(length(response_body),0)+COALESCE(length(response_ref),0)+COALESCE(length(provider),0)"
    "+length(sim_time)+32",
    "reply": "length(reply)+8",
    "span": "length(span)+length(sim_time)+length(source)+length(trace_id)+length(placed_by)+32",
    "span_body": "length(ref)+16",
    "wake_edge": "length(edge)+length(wall_time)+8",
    "forward_failure": "length(failure)+8",
}
"""What each of a run's rows holds, in bytes, for `usage`: the columns as stored, not SQLite's page overhead."""


class SqliteStore:
    def __init__(self, path: Path, run_id: str, clock: Clock) -> None:
        self._attach(path, run_id, clock, sqlite3.connect(path, check_same_thread=False, timeout=_BUSY_SECONDS))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(f"PRAGMA journal_size_limit={LOG_LIMIT}")
        found = self._db.execute("PRAGMA user_version").fetchone()[0]
        tables = self._db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if tables and found != SCHEMA_VERSION:
            raise RuntimeError(
                f"{path} was written with store schema {found}; this version reads schema {SCHEMA_VERSION}"
            )
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.execute(
            "INSERT OR IGNORE INTO run(run_id, parent, forked_at, forked_calls, forked_wake)"
            " VALUES(?, NULL, NULL, NULL, NULL)",
            (run_id,),
        )
        self._db.commit()
        self._lineage = self._load_lineage()

    def _attach(self, path: Path, run_id: str, clock: Clock, db: sqlite3.Connection) -> None:
        """What every store holds, however its connection was opened."""
        self.run_id = run_id
        self._path = path
        self._clock = clock
        # One connection shared across threads behind one lock: a provider mounted as a
        # WSGI app is served from a worker thread, and a second writer would not help SQLite.
        self._lock = threading.RLock()
        self._db = db
        # zstd's (de)compressors are not safe across threads; each store's are used only under its lock.
        self._pack = zstandard.ZstdCompressor(level=3)
        self._unpack = zstandard.ZstdDecompressor()

    def close(self) -> None:
        """Close the file, cutting its write-ahead log to nothing when no other connection is reading it: a
        process that holds many worlds over its life keeps open only those still in use."""
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()

    def _load_lineage(self) -> list[tuple[str, int | None, int | None, int | None]]:
        """This run and its ancestors, each with the highest event seq, the number of recorded calls and the
        highest wake of its spans that this run may see (None: all)."""
        lineage: list[tuple[str, int | None, int | None, int | None]] = []
        run, seq_limit, call_limit, wake_limit = self.run_id, None, None, None
        while run is not None:
            row = self._db.execute(
                "SELECT parent, forked_at, forked_calls, forked_wake FROM run WHERE run_id=?", (run,)
            ).fetchone()
            if row is None:
                raise LookupError(f"no such run: {run}")
            lineage.append((run, seq_limit, call_limit, wake_limit))
            run, seq_limit, call_limit, wake_limit = row[0], row[1], row[2], row[3]
        return lineage

    def _visible(self, alias: str = "", column: str = "seq") -> tuple[str, list[object]]:
        """A WHERE fragment selecting the rows this run can see, optionally for a table alias."""
        return self._within([(run, limit) for run, limit, _, _ in self._lineage], f"{alias}." if alias else "", column)

    def _spans_visible(self) -> tuple[str, list[object]]:
        """The spans this run can see: its own, and each ancestor's placed in the wakes up to the one it was forked
        after, by the same placement `spans(wake=)` reads."""
        return self._within([(run, wake) for run, _, _, wake in self._lineage], "", "wake")

    @staticmethod
    def _within(limits: list[tuple[str, int | None]], at: str, column: str) -> tuple[str, list[object]]:
        parts: list[str] = []
        args: list[object] = []
        for run, limit in limits:
            if limit is None:
                parts.append(f"({at}run_id=?)")
                args.append(run)
            else:
                parts.append(f"({at}run_id=? AND {at}{column}<=?)")
                args += [run, limit]
        return "(" + " OR ".join(parts) + ")", args

    # -- bodies -------------------------------------------------------------------------------------------------

    def _keep(self, text: str | None) -> tuple[str | None, bytes | None]:
        """Where a body goes: (text, None) in its own row, (None, hash) in `content`, (None, None) for no body.
        Called inside the transaction that writes the row referring to it, so a sweep never sees the body
        unreferenced in between."""
        if text is None:
            return None, None
        raw = text.encode("utf-8")
        if len(raw) < INLINE_LIMIT:
            return text, None
        return None, self._keep_bytes(raw)

    def _keep_bytes(self, raw: bytes) -> bytes:
        """Bytes in `content`, whatever their size, under the hash of exactly those bytes."""
        digest = hashlib.sha256(raw).digest()
        if self._db.execute("SELECT 1 FROM content WHERE hash=?", (digest,)).fetchone() is None:
            packed = self._pack.compress(raw)
            codec, stored = (Codec.ZSTD, packed) if len(packed) < len(raw) else (Codec.RAW, raw)
            self._db.execute("INSERT INTO content VALUES(?,?,?,?)", (digest, len(raw), codec.value, stored))
        return digest

    def _bytes(self, codec: str | None, stored: bytes | None) -> bytes | None:
        if stored is None:
            return None
        return self._unpack.decompress(stored) if Codec(codec) is Codec.ZSTD else stored

    def _text(self, inline: str | None, codec: str | None, stored: bytes | None) -> str | None:
        """A body as it was written, from its row or from `content`."""
        if stored is None:
            return inline
        raw = self._unpack.decompress(stored) if Codec(codec) is Codec.ZSTD else stored
        return raw.decode("utf-8")

    def _content(self, digest: bytes) -> str:
        row = self._db.execute("SELECT codec, stored FROM content WHERE hash=?", (digest,)).fetchone()
        if row is None:
            raise LookupError(f"the stored body {digest.hex()} is missing from {self._path}")
        found = self._text(None, row[0], row[1])
        assert found is not None
        return found

    # -- the log --------------------------------------------------------------------------------------------------

    @_locked
    def head(self) -> int:
        where, args = self._visible()
        return self._db.execute(f"SELECT COALESCE(MAX(seq), 0) FROM event WHERE {where}", args).fetchone()[0]

    @_locked
    def apply(self, change: Change) -> WorldEvent:
        event = self._write(change)
        self._db.commit()
        return event

    @_locked
    def apply_all(self, changes: Sequence[Change]) -> list[WorldEvent]:
        events = [self._write(change) for change in changes]
        self._db.commit()
        return events

    def _write(self, change: Change) -> WorldEvent:
        """One change written in the open transaction, under the lock its caller holds."""
        seq = self.head() + 1
        sim = self._clock.now()
        wall = datetime.now(UTC)  # clock-lint: exempt wall_time is the one field that records the machine clock
        ref = change.entity
        after, after_ref = self._keep(_SNAPSHOT.dump_json(change.after).decode() if change.after is not None else None)
        self._db.execute(
            "INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id,
                seq,
                self._clock.wake(),
                sim.isoformat(),
                wall.isoformat(),
                change.actor.value,
                change.operation.value,
                ref.provider,
                ref.kind.value,
                ref.external_id,
                after,
                after_ref,
            ),
        )
        if change.operation is Operation.DELETE or change.body is not None:
            body, body_ref = self._keep(None if change.operation is Operation.DELETE else change.body)
            self._db.execute(
                "INSERT INTO entity_version VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    seq,
                    ref.provider,
                    ref.kind.value,
                    ref.external_id,
                    change.parent,
                    body,
                    body_ref,
                    sim.isoformat(),
                ),
            )
        return WorldEvent(
            seq=seq,
            run_id=self.run_id,
            wake=self._clock.wake(),
            sim_time=sim,
            wall_time=wall,
            actor=change.actor,
            operation=change.operation,
            entity=ref,
            after=change.after,
        )

    @_locked
    def get(self, entity: EntityRef) -> Stored | None:
        where, args = self._visible("v")
        row = self._db.execute(
            f"SELECT v.body, c.codec, c.stored, v.parent, v.seq, v.sim_time"
            f" FROM entity_version v INDEXED BY entity_lookup LEFT JOIN content c ON c.hash=v.body_ref"
            f" WHERE v.provider=? AND v.kind=? AND v.external_id=? AND {where}"
            " ORDER BY v.seq DESC LIMIT 1",
            [entity.provider, entity.kind.value, entity.external_id, *args],
        ).fetchone()
        if row is None:
            return None
        body = self._text(row[0], row[1], row[2])
        if body is None:
            return None
        return Stored(entity=entity, body=body, parent=row[3], seq=row[4], sim_time=datetime.fromisoformat(row[5]))

    @_locked
    def children(
        self, provider: ProviderKey, kind: EntityKind, parent: str | None, *, after: str | None = None, limit: int = 100
    ) -> list[Stored]:
        mine, mine_args = self._visible("v")
        newer, newer_args = self._visible("n")
        rows = self._db.execute(
            f"""SELECT v.external_id, v.body, c.codec, c.stored, v.parent, v.seq, v.sim_time
                FROM entity_version v INDEXED BY entity_listing LEFT JOIN content c ON c.hash=v.body_ref
                WHERE v.provider=? AND v.kind=? AND v.parent IS ? AND v.external_id > ? AND {mine}
                  AND (v.body IS NOT NULL OR v.body_ref IS NOT NULL)
                  AND NOT EXISTS (SELECT 1 FROM entity_version n INDEXED BY entity_lookup
                                  WHERE n.provider=v.provider AND n.kind=v.kind AND n.external_id=v.external_id
                                    AND n.seq > v.seq AND {newer})
                ORDER BY v.external_id LIMIT ?""",
            [provider, kind.value, parent, after or "", *mine_args, *newer_args, limit],
        ).fetchall()
        found: list[Stored] = []
        for r in rows:
            body = self._text(r[1], r[2], r[3])
            assert body is not None
            found.append(
                Stored(
                    entity=EntityRef(provider=provider, kind=kind, external_id=r[0]),
                    body=body,
                    parent=r[4],
                    seq=r[5],
                    sim_time=datetime.fromisoformat(r[6]),
                )
            )
        return found

    @_locked
    def events(self, *, since: int = 0) -> list[WorldEvent]:
        where, args = self._visible("e")
        rows = self._db.execute(
            f"SELECT e.run_id, e.seq, e.wake, e.sim_time, e.wall_time, e.actor, e.operation, e.provider, e.kind,"
            f" e.external_id, e.after, c.codec, c.stored"
            f" FROM event e LEFT JOIN content c ON c.hash=e.after_ref WHERE {where} AND e.seq>? ORDER BY e.seq",
            [*args, since],
        ).fetchall()
        attached: dict[int, Exchange] = {}
        for call in self._calls(touching_after=since):
            for seq in range(call.first_seq, call.last_seq + 1):
                attached[seq] = call.exchange
        found: list[WorldEvent] = []
        for r in rows:
            after = self._text(r[10], r[11], r[12])
            found.append(
                WorldEvent(
                    seq=r[1],
                    run_id=r[0],
                    wake=r[2],
                    sim_time=datetime.fromisoformat(r[3]),
                    wall_time=datetime.fromisoformat(r[4]),
                    actor=Actor(r[5]),
                    operation=Operation(r[6]),
                    entity=EntityRef(provider=r[7], kind=EntityKind(r[8]), external_id=r[9]),
                    after=_SNAPSHOT.validate_json(after) if after is not None else None,
                    exchange=attached.get(r[1]),
                )
            )
        return found

    @_locked
    def attach(
        self,
        exchange: Exchange,
        *,
        first_seq: int,
        last_seq: int,
        provider: ProviderKey | None = None,
        began: CallBegan | None = None,
    ) -> None:
        position = self._db.execute("SELECT COUNT(*) FROM exchange WHERE run_id=?", (self.run_id,)).fetchone()[0]
        request, request_ref = self._keep(exchange.request_body)
        response, response_ref = self._keep(exchange.response_body)
        if exchange.request_bytes is not None:
            request_ref = self._keep_bytes(exchange.request_bytes)
        if exchange.response_bytes is not None:
            response_ref = self._keep_bytes(exchange.response_bytes)
        self._db.execute(
            "INSERT INTO exchange VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id,
                position,
                first_seq,
                last_seq,
                provider,
                began.wake if began is not None else self._clock.wake(),
                (began.sim_time if began is not None else self._clock.now()).isoformat(),
                exchange.model_dump_json(
                    exclude=_UNKEPT_IN_ROW | ({"inbox_call"} if exchange.inbox_call is None else set())
                ),
                request,
                request_ref,
                response,
                response_ref,
                int(exchange.request_bytes is not None),
                int(exchange.response_bytes is not None),
            ),
        )
        self._db.commit()

    @_locked
    def calls(self) -> list[RecordedCall]:
        return self._calls(touching_after=None)

    def _calls(self, *, touching_after: int | None) -> list[RecordedCall]:
        """Recorded calls this run can see, oldest ancestor first. A fork sees the calls its parent had
        recorded when the fork was taken. `touching_after` keeps only calls with an event above that seq."""
        parts: list[str] = []
        args: list[object] = []
        for depth, (run, _, call_limit, _) in enumerate(self._lineage):
            clause = (
                f"SELECT {depth} AS depth, x.position, x.first_seq, x.last_seq, x.provider, x.wake, x.sim_time,"
                " x.exchange, x.request_body, q.codec, q.stored, x.response_body, a.codec, a.stored,"
                " x.request_binary, x.response_binary"
                " FROM exchange x LEFT JOIN content q ON q.hash=x.request_ref"
                " LEFT JOIN content a ON a.hash=x.response_ref WHERE x.run_id=?"
            )
            args.append(run)
            if call_limit is not None:
                clause += " AND x.position<?"
                args.append(call_limit)
            if touching_after is not None:
                clause += " AND x.first_seq<=x.last_seq AND x.last_seq>?"
                args.append(touching_after)
            parts.append(clause)
        rows = self._db.execute(" UNION ALL ".join(parts) + " ORDER BY depth DESC, position", args).fetchall()
        return [
            RecordedCall(
                exchange=Exchange.model_validate_json(r[7]).model_copy(
                    update={
                        "request_body": None if r[14] else self._text(r[8], r[9], r[10]),
                        "response_body": None if r[15] else self._text(r[11], r[12], r[13]),
                        "request_bytes": self._bytes(r[9], r[10]) if r[14] else None,
                        "response_bytes": self._bytes(r[12], r[13]) if r[15] else None,
                    }
                ),
                provider=r[4],
                first_seq=r[2],
                last_seq=r[3],
                wake=r[5],
                sim_time=datetime.fromisoformat(r[6]),
            )
            for r in rows
        ]

    @_locked
    def remember(self, reply: PersonReply) -> None:
        position = self._db.execute("SELECT COUNT(*) FROM reply WHERE run_id=?", (self.run_id,)).fetchone()[0]
        self._db.execute("INSERT INTO reply VALUES(?,?,?)", (self.run_id, position, reply.model_dump_json()))
        self._db.commit()

    @_locked
    def replies(self) -> list[PersonReply]:
        rows = self._db.execute("SELECT reply FROM reply WHERE run_id=? ORDER BY position", (self.run_id,)).fetchall()
        return [PersonReply.model_validate_json(r[0]) for r in rows]

    @_locked
    def versions(self, entity: EntityRef) -> list[Stored]:
        where, args = self._visible("v")
        rows = self._db.execute(
            f"SELECT v.body, c.codec, c.stored, v.parent, v.seq, v.sim_time"
            f" FROM entity_version v INDEXED BY entity_lookup LEFT JOIN content c ON c.hash=v.body_ref"
            f" WHERE v.provider=? AND v.kind=? AND v.external_id=? AND {where}"
            " AND (v.body IS NOT NULL OR v.body_ref IS NOT NULL) ORDER BY v.seq",
            [entity.provider, entity.kind.value, entity.external_id, *args],
        ).fetchall()
        found: list[Stored] = []
        for r in rows:
            body = self._text(r[0], r[1], r[2])
            assert body is not None
            found.append(Stored(entity=entity, body=body, parent=r[3], seq=r[4], sim_time=datetime.fromisoformat(r[5])))
        return found

    @_locked
    def discard(self) -> None:
        children = [r[0] for r in self._db.execute("SELECT run_id FROM run WHERE parent=?", (self.run_id,))]
        if children:
            raise ValueError(f"run {self.run_id} has forks ({', '.join(children)}) reading through it")
        # One transaction: the run's rows and every body only they referred to go together, or not at all.
        for table in (*_RUN_TABLES, "run"):
            self._db.execute(f"DELETE FROM {table} WHERE run_id=?", (self.run_id,))
        self._sweep_content()
        self._db.commit()

    def _sweep_content(self) -> tuple[int, int]:
        """Delete every stored body no row refers to, inside the caller's transaction; answers how many and their
        bytes as stored."""
        unreferenced = f"FROM content WHERE hash NOT IN (SELECT hash FROM ({_REFERENCES}))"
        count, size = self._db.execute(f"SELECT COUNT(*), COALESCE(SUM(length(stored)), 0) {unreferenced}").fetchone()
        self._db.execute(f"DELETE {unreferenced}")
        return count, size

    # -- wakes and spans ------------------------------------------------------------------------------------------

    @_locked
    def wake_began(self, wake: int) -> None:
        self._edge(wake, _Edge.BEGAN)

    @_locked
    def wake_ended(self, wake: int) -> None:
        self._edge(wake, _Edge.ENDED)

    def _edge(self, wake: int, edge: _Edge) -> None:
        wall = datetime.now(UTC)  # clock-lint: exempt wall_time of a wake's edge: the agent's spans are placed by it
        self._db.execute("INSERT INTO wake_edge VALUES(?,?,?,?)", (self.run_id, wake, edge.value, wall.isoformat()))
        self._db.commit()

    def _windows(self) -> list[tuple[int, datetime, datetime | None]]:
        """Each of this run's wakes with the real moment it began and, once it has, ended; the latest to begin
        first."""
        began: dict[int, datetime] = {}
        ended: dict[int, datetime] = {}
        for wake, edge, wall in self._db.execute(
            "SELECT wake, edge, wall_time FROM wake_edge WHERE run_id=? ORDER BY rowid", (self.run_id,)
        ):
            (began if _Edge(edge) is _Edge.BEGAN else ended).setdefault(wake, datetime.fromisoformat(wall))
        windows = [(wake, at, ended[wake] if wake in ended else None) for wake, at in began.items()]
        return sorted(windows, key=lambda window: window[1], reverse=True)

    @_locked
    def receive(self, spans: Sequence[ReceivedSpan], *, source: SpanSource) -> list[StoredSpan]:
        position = self._db.execute("SELECT COUNT(*) FROM span WHERE run_id=?", (self.run_id,)).fetchone()[0]
        head, arrived, sim = self.head(), self._clock.wake(), self._clock.now()
        windows = self._windows()
        stored: list[StoredSpan] = []
        for span in spans:
            window = next(
                (w for w, began, ended in windows if began <= span.start and (ended is None or span.start < ended)),
                None,
            )
            stored.append(
                StoredSpan(
                    span=span,
                    run_id=self.run_id,
                    source=source,
                    wake=window if window is not None else arrived,
                    placed_by=Placement.WINDOW if window is not None else Placement.ARRIVAL,
                    arrived_in_wake=arrived,
                    sim_time=sim,
                    after_seq=head,
                )
            )
        for i, one in enumerate(stored):
            refs: list[tuple[int, bytes]] = []

            def kept(index: int, value: StringValue, refs: list[tuple[int, bytes]] = refs) -> StringValue:
                _, ref = self._keep(value.value)
                if ref is None:
                    return value
                refs.append((index, ref))
                return StringValue(value="")

            shell = _strings(one.span, kept)
            self._db.execute(
                "INSERT INTO span VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    position + i,
                    head,
                    one.wake,
                    sim.isoformat(),
                    source.value,
                    one.span.trace_id,
                    shell.model_dump_json(),
                    arrived,
                    one.placed_by.value,
                ),
            )
            self._db.executemany(
                "INSERT INTO span_body VALUES(?,?,?,?)", [(self.run_id, position + i, n, ref) for n, ref in refs]
            )
        self._db.commit()
        return stored

    @_locked
    def spans(self, *, trace_id: str | None = None, wake: int | None = None) -> list[StoredSpan]:
        where, args = self._spans_visible()
        query = (
            "SELECT run_id, after_seq, wake, sim_time, source, span, position, arrived_wake, placed_by"
            f" FROM span WHERE {where}"
        )
        if trace_id is not None:
            query += " AND trace_id=?"
            args.append(trace_id)
        if wake is not None:
            query += " AND wake=?"
            args.append(wake)
        depth = {run: d for d, (run, _, _, _) in enumerate(self._lineage)}
        # Oldest ancestor first, and in each run the order they arrived.
        rows = sorted(self._db.execute(query, args).fetchall(), key=lambda r: (-depth[r[0]], r[6]))
        found: list[StoredSpan] = []
        for r in rows:
            span = ReceivedSpan.model_validate_json(r[5])
            held = {
                value: self._content(ref)
                for value, ref in self._db.execute(
                    "SELECT value, ref FROM span_body WHERE run_id=? AND position=?", (r[0], r[6])
                )
            }
            if held:

                def back(index: int, value: StringValue, held: dict[int, str] = held) -> StringValue:
                    return StringValue(value=held[index]) if index in held else value

                span = _strings(span, back)
            found.append(
                StoredSpan(
                    span=span,
                    run_id=r[0],
                    source=SpanSource(r[4]),
                    wake=r[2],
                    placed_by=Placement(r[8]),
                    arrived_in_wake=r[7],
                    sim_time=datetime.fromisoformat(r[3]),
                    after_seq=r[1],
                )
            )
        return found

    @_locked
    def forward_failed(self, signal: Signal, endpoint: str, reason: str) -> ForwardFailure:
        position = self._db.execute("SELECT COUNT(*) FROM forward_failure WHERE run_id=?", (self.run_id,)).fetchone()[0]
        failure = ForwardFailure(
            signal=signal, endpoint=endpoint, reason=reason, wake=self._clock.wake(), sim_time=self._clock.now()
        )
        self._db.execute(
            "INSERT INTO forward_failure VALUES(?,?,?)", (self.run_id, position, failure.model_dump_json())
        )
        self._db.commit()
        return failure

    @_locked
    def forward_failures(self) -> list[ForwardFailure]:
        rows = self._db.execute(
            "SELECT failure FROM forward_failure WHERE run_id=? ORDER BY position", (self.run_id,)
        ).fetchall()
        return [ForwardFailure.model_validate_json(r[0]) for r in rows]

    @_locked
    def fork(self, run_id: str, *, at_seq: int, clock: Clock) -> SqliteStore:
        if not 0 <= at_seq <= self.head():
            raise ValueError(f"cannot fork at {at_seq}: this run's head is {self.head()}")
        where, args = self._visible()
        at_wake = self._db.execute(f"SELECT wake FROM event WHERE {where} AND seq=?", [*args, at_seq]).fetchone()
        # A call whose first event would be at_seq + 1 was made at the fork's moment, or is the next wake's first:
        # only its wake tells them apart.
        recorded = self._db.execute(
            "SELECT COUNT(*) FROM exchange WHERE run_id=? AND first_seq-1<=? AND wake<=?",
            (self.run_id, at_seq, at_wake[0] if at_wake is not None else 0),
        ).fetchone()[0]
        self._db.execute(
            "INSERT INTO run(run_id, parent, forked_at, forked_calls, forked_wake) VALUES(?,?,?,?,?)",
            (run_id, self.run_id, at_seq, recorded, at_wake[0] if at_wake is not None else 0),
        )
        self._db.commit()
        return SqliteStore(self._path, run_id, clock)

    @_locked
    def sweep(self) -> Freed:
        bodies, body_bytes = self._sweep_content()
        self._db.commit()
        return Freed(bodies=bodies, body_bytes=body_bytes)

    @_locked
    def usage(self) -> RunUsage:
        rows = sum(
            self._db.execute(f"SELECT COALESCE(SUM({size}), 0) FROM {table} WHERE run_id=?", (self.run_id,)).fetchone()[
                0
            ]
            for table, size in _ROW_BYTES.items()
        )
        bodies = self._db.execute(
            f"""WITH refs AS ({_REFERENCES})
                SELECT COALESCE(SUM(length(stored)), 0) FROM content
                WHERE hash IN (SELECT hash FROM refs WHERE run_id=?)
                  AND hash NOT IN (SELECT hash FROM refs WHERE run_id!=?)""",
            (self.run_id, self.run_id),
        ).fetchone()[0]
        return RunUsage(run_id=self.run_id, rows=rows, bodies=bodies)


def _strings(span: ReceivedSpan, swap: Callable[[int, StringValue], StringValue]) -> ReceivedSpan:
    """The span with every string attribute value passed through `swap`, numbered in one fixed order (attribute by
    attribute, depth first), so a value taken out on write is put back at the same place on read."""
    counter = [0]

    def value(found: AttributeValue | None) -> AttributeValue | None:
        if isinstance(found, StringValue):
            index = counter[0]
            counter[0] += 1
            return swap(index, found)
        if isinstance(found, ArrayValue):
            return found.model_copy(update={"values": [v for v in (value(v) for v in found.values) if v is not None]})
        if isinstance(found, MapValue):
            return found.model_copy(
                update={"values": [a.model_copy(update={"value": value(a.value)}) for a in found.values]}
            )
        return found

    return span.model_copy(
        update={"attributes": [a.model_copy(update={"value": value(a.value)}) for a in span.attributes]}
    )


def truncate_log(path: Path) -> None:
    """Cut a world file's write-ahead log to nothing, once the run writing it is over: the log is a working file,
    not part of the record. Does nothing while another connection is reading the file."""
    db = sqlite3.connect(path, timeout=_BUSY_SECONDS)
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
