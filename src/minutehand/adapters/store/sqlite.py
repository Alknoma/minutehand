"""The world as an append-only SQLite log.

One file holds a root run and every fork of it. A run reads its own rows plus its
ancestors' rows up to the sequence number each fork was taken at, so a fork copies
nothing.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Concatenate

from pydantic import TypeAdapter

from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import ProviderKey
from minutehand.domain.world import (
    Actor,
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


def _locked[**P, R](method: Callable[Concatenate[SqliteStore, P], R]) -> Callable[Concatenate[SqliteStore, P], R]:
    """Run a store method under the store's lock, so it is safe from any thread."""

    @functools.wraps(method)
    def inner(self: SqliteStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    return inner


SCHEMA_VERSION = 2
"""Stamped into the file as SQLite's user_version. A file with another version is refused, not guessed at."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run(
  run_id TEXT PRIMARY KEY, parent TEXT REFERENCES run(run_id), forked_at INTEGER, forked_calls INTEGER);
CREATE TABLE IF NOT EXISTS event(
  run_id TEXT NOT NULL, seq INTEGER NOT NULL, wake INTEGER NOT NULL,
  sim_time TEXT NOT NULL, wall_time TEXT NOT NULL, actor TEXT NOT NULL, operation TEXT NOT NULL,
  provider TEXT NOT NULL, kind TEXT NOT NULL, external_id TEXT NOT NULL, after TEXT,
  PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS entity_version(
  run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  provider TEXT NOT NULL, kind TEXT NOT NULL, external_id TEXT NOT NULL,
  parent TEXT, body TEXT, sim_time TEXT NOT NULL,
  PRIMARY KEY (run_id, seq));
CREATE INDEX IF NOT EXISTS entity_lookup ON entity_version(provider, kind, external_id, seq);
CREATE INDEX IF NOT EXISTS entity_listing ON entity_version(provider, kind, parent, external_id);
CREATE TABLE IF NOT EXISTS exchange(
  run_id TEXT NOT NULL, position INTEGER NOT NULL, first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL,
  provider TEXT, wake INTEGER NOT NULL, sim_time TEXT NOT NULL, exchange TEXT NOT NULL,
  PRIMARY KEY (run_id, position));
CREATE TABLE IF NOT EXISTS reply(run_id TEXT NOT NULL, position INTEGER NOT NULL, reply TEXT NOT NULL);
"""


class SqliteStore:
    def __init__(self, path: Path, run_id: str, clock: Clock) -> None:
        self.run_id = run_id
        self._path = path
        self._clock = clock
        # One connection shared across threads behind one lock: a provider mounted as a
        # WSGI app is served from a worker thread, and a second writer would not help SQLite.
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        found = self._db.execute("PRAGMA user_version").fetchone()[0]
        tables = self._db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if tables and found != SCHEMA_VERSION:
            raise RuntimeError(
                f"{path} was written with store schema {found}; this version reads schema {SCHEMA_VERSION}"
            )
        self._db.executescript(_SCHEMA)
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.execute(
            "INSERT OR IGNORE INTO run(run_id, parent, forked_at, forked_calls) VALUES(?, NULL, NULL, NULL)", (run_id,)
        )
        self._db.commit()
        self._lineage = self._load_lineage()

    def _load_lineage(self) -> list[tuple[str, int | None, int | None]]:
        """This run and its ancestors, each with the highest event seq and the number of recorded
        calls of it that this run may see (None: all)."""
        lineage: list[tuple[str, int | None, int | None]] = []
        run, seq_limit, call_limit = self.run_id, None, None
        while run is not None:
            row = self._db.execute("SELECT parent, forked_at, forked_calls FROM run WHERE run_id=?", (run,)).fetchone()
            if row is None:
                raise LookupError(f"no such run: {run}")
            lineage.append((run, seq_limit, call_limit))
            run, seq_limit, call_limit = row[0], row[1], row[2]
        return lineage

    def _visible(self, alias: str = "", column: str = "seq") -> tuple[str, list[object]]:
        """A WHERE fragment selecting the rows this run can see, optionally for a table alias."""
        at = f"{alias}." if alias else ""
        parts: list[str] = []
        args: list[object] = []
        for run, limit, _ in self._lineage:
            if limit is None:
                parts.append(f"({at}run_id=?)")
                args.append(run)
            else:
                parts.append(f"({at}run_id=? AND {at}{column}<=?)")
                args += [run, limit]
        return "(" + " OR ".join(parts) + ")", args

    @_locked
    def head(self) -> int:
        where, args = self._visible()
        return self._db.execute(f"SELECT COALESCE(MAX(seq), 0) FROM event WHERE {where}", args).fetchone()[0]

    @_locked
    def apply(self, change: Change) -> WorldEvent:
        seq = self.head() + 1
        sim = self._clock.now()
        wall = datetime.now(UTC)  # clock-lint: exempt wall_time is the one field that records the machine clock
        ref = change.entity
        self._db.execute(
            "INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                _SNAPSHOT.dump_json(change.after).decode() if change.after is not None else None,
            ),
        )
        if change.operation is Operation.DELETE or change.body is not None:
            self._db.execute(
                "INSERT INTO entity_version VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    seq,
                    ref.provider,
                    ref.kind.value,
                    ref.external_id,
                    change.parent,
                    None if change.operation is Operation.DELETE else change.body,
                    sim.isoformat(),
                ),
            )
        self._db.commit()
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
        where, args = self._visible()
        row = self._db.execute(
            f"SELECT body, parent, seq, sim_time FROM entity_version INDEXED BY entity_lookup"
            f" WHERE provider=? AND kind=? AND external_id=? AND {where}"
            " ORDER BY seq DESC LIMIT 1",
            [entity.provider, entity.kind.value, entity.external_id, *args],
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return Stored(entity=entity, body=row[0], parent=row[1], seq=row[2], sim_time=datetime.fromisoformat(row[3]))

    @_locked
    def children(
        self, provider: ProviderKey, kind: EntityKind, parent: str | None, *, after: str | None = None, limit: int = 100
    ) -> list[Stored]:
        mine, mine_args = self._visible("v")
        newer, newer_args = self._visible("n")
        rows = self._db.execute(
            f"""SELECT v.external_id, v.body, v.parent, v.seq, v.sim_time
                FROM entity_version v INDEXED BY entity_listing
                WHERE v.provider=? AND v.kind=? AND v.parent IS ? AND v.external_id > ? AND {mine}
                  AND v.body IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM entity_version n INDEXED BY entity_lookup
                                  WHERE n.provider=v.provider AND n.kind=v.kind AND n.external_id=v.external_id
                                    AND n.seq > v.seq AND {newer})
                ORDER BY v.external_id LIMIT ?""",
            [provider, kind.value, parent, after or "", *mine_args, *newer_args, limit],
        ).fetchall()
        return [
            Stored(
                entity=EntityRef(provider=provider, kind=kind, external_id=r[0]),
                body=r[1],
                parent=r[2],
                seq=r[3],
                sim_time=datetime.fromisoformat(r[4]),
            )
            for r in rows
        ]

    @_locked
    def events(self, *, since: int = 0) -> list[WorldEvent]:
        where, args = self._visible()
        rows = self._db.execute(
            f"SELECT run_id, seq, wake, sim_time, wall_time, actor, operation, provider, kind, external_id, after"
            f" FROM event WHERE {where} AND seq>? ORDER BY seq",
            [*args, since],
        ).fetchall()
        attached: dict[int, Exchange] = {}
        for call in self._calls(touching_after=since):
            for seq in range(call.first_seq, call.last_seq + 1):
                attached[seq] = call.exchange
        return [
            WorldEvent(
                seq=r[1],
                run_id=r[0],
                wake=r[2],
                sim_time=datetime.fromisoformat(r[3]),
                wall_time=datetime.fromisoformat(r[4]),
                actor=Actor(r[5]),
                operation=Operation(r[6]),
                entity=EntityRef(provider=r[7], kind=EntityKind(r[8]), external_id=r[9]),
                after=_SNAPSHOT.validate_json(r[10]) if r[10] is not None else None,
                exchange=attached.get(r[1]),
            )
            for r in rows
        ]

    @_locked
    def attach(self, exchange: Exchange, *, first_seq: int, last_seq: int, provider: ProviderKey | None = None) -> None:
        position = self._db.execute("SELECT COUNT(*) FROM exchange WHERE run_id=?", (self.run_id,)).fetchone()[0]
        self._db.execute(
            "INSERT INTO exchange VALUES(?,?,?,?,?,?,?,?)",
            (
                self.run_id,
                position,
                first_seq,
                last_seq,
                provider,
                self._clock.wake(),
                self._clock.now().isoformat(),
                exchange.model_dump_json(),
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
        for depth, (run, _, call_limit) in enumerate(self._lineage):
            clause = f"SELECT {depth} AS depth, * FROM exchange WHERE run_id=?"
            args.append(run)
            if call_limit is not None:
                clause += " AND position<?"
                args.append(call_limit)
            if touching_after is not None:
                clause += " AND first_seq<=last_seq AND last_seq>?"
                args.append(touching_after)
            parts.append(clause)
        rows = self._db.execute(" UNION ALL ".join(parts) + " ORDER BY depth DESC, position", args).fetchall()
        return [
            RecordedCall(
                exchange=Exchange.model_validate_json(r[8]),
                provider=r[5],
                first_seq=r[3],
                last_seq=r[4],
                wake=r[6],
                sim_time=datetime.fromisoformat(r[7]),
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
        where, args = self._visible()
        rows = self._db.execute(
            f"SELECT body, parent, seq, sim_time FROM entity_version INDEXED BY entity_lookup"
            f" WHERE provider=? AND kind=? AND external_id=? AND {where} AND body IS NOT NULL ORDER BY seq",
            [entity.provider, entity.kind.value, entity.external_id, *args],
        ).fetchall()
        return [
            Stored(entity=entity, body=r[0], parent=r[1], seq=r[2], sim_time=datetime.fromisoformat(r[3])) for r in rows
        ]

    @_locked
    def discard(self) -> None:
        children = [r[0] for r in self._db.execute("SELECT run_id FROM run WHERE parent=?", (self.run_id,))]
        if children:
            raise ValueError(f"run {self.run_id} has forks ({', '.join(children)}) reading through it")
        for table in ("event", "entity_version", "exchange", "reply", "run"):
            self._db.execute(f"DELETE FROM {table} WHERE run_id=?", (self.run_id,))
        self._db.commit()

    @_locked
    def fork(self, run_id: str, *, at_seq: int, clock: Clock) -> SqliteStore:
        if not 0 <= at_seq <= self.head():
            raise ValueError(f"cannot fork at {at_seq}: this run's head is {self.head()}")
        recorded = self._db.execute(
            "SELECT COUNT(*) FROM exchange WHERE run_id=? AND first_seq-1<=?", (self.run_id, at_seq)
        ).fetchone()[0]
        self._db.execute(
            "INSERT INTO run(run_id, parent, forked_at, forked_calls) VALUES(?,?,?,?)",
            (run_id, self.run_id, at_seq, recorded),
        )
        self._db.commit()
        return SqliteStore(self._path, run_id, clock)
