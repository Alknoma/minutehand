"""The world as an append-only SQLite log.

One file holds a root run and every fork of it. A run reads its own rows plus its
ancestors' rows up to the sequence number each fork was taken at, so a fork copies
nothing.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pydantic import TypeAdapter

from minutehand.domain.people import PersonReply
from minutehand.domain.scenario import ProviderKey
from minutehand.domain.world import (
    Actor, Change, EntityKind, EntityRef, Exchange, Operation, Snapshot, Stored, WorldEvent,
)
from minutehand.ports.clock import Clock

_SNAPSHOT = TypeAdapter(Snapshot)

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS run(
  run_id TEXT PRIMARY KEY, parent TEXT REFERENCES run(run_id), forked_at INTEGER);
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
CREATE INDEX IF NOT EXISTS entity_parent ON entity_version(provider, kind, parent);
CREATE TABLE IF NOT EXISTS exchange(
  run_id TEXT NOT NULL, first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL, exchange TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reply(run_id TEXT NOT NULL, position INTEGER NOT NULL, reply TEXT NOT NULL);
"""


class SqliteStore:
    def __init__(self, path: Path, run_id: str, clock: Clock) -> None:
        self.run_id = run_id
        self._path = path
        self._clock = clock
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO run(run_id, parent, forked_at) VALUES(?, NULL, NULL)", (run_id,))
        self._db.commit()
        self._lineage = self._load_lineage()

    def _load_lineage(self) -> list[tuple[str, int | None]]:
        """This run and its ancestors, each with the highest seq of it that this run may see (None: all)."""
        lineage: list[tuple[str, int | None]] = []
        run, limit = self.run_id, None
        while run is not None:
            row = self._db.execute("SELECT parent, forked_at FROM run WHERE run_id=?", (run,)).fetchone()
            if row is None:
                raise LookupError(f"no such run: {run}")
            lineage.append((run, limit))
            run, limit = row[0], row[1]
        return lineage

    def _visible(self) -> tuple[str, list[object]]:
        """A WHERE fragment selecting the rows this run can see."""
        parts: list[str] = []
        args: list[object] = []
        for run, limit in self._lineage:
            if limit is None:
                parts.append("(run_id=?)")
                args.append(run)
            else:
                parts.append("(run_id=? AND seq<=?)")
                args += [run, limit]
        return "(" + " OR ".join(parts) + ")", args

    def head(self) -> int:
        where, args = self._visible()
        return self._db.execute(f"SELECT COALESCE(MAX(seq), 0) FROM event WHERE {where}", args).fetchone()[0]

    def apply(self, change: Change) -> WorldEvent:
        seq = self.head() + 1
        sim = self._clock.now()
        wall = datetime.now(timezone.utc)  # clock-lint: exempt wall_time is the one field that records the machine clock
        ref = change.entity
        self._db.execute(
            "INSERT INTO event VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, seq, self._clock.wake(), sim.isoformat(), wall.isoformat(), change.actor.value,
             change.operation.value, ref.provider, ref.kind.value, ref.external_id,
             _SNAPSHOT.dump_json(change.after).decode() if change.after is not None else None),
        )
        if change.operation is Operation.DELETE or change.body is not None:
            self._db.execute(
                "INSERT INTO entity_version VALUES(?,?,?,?,?,?,?,?)",
                (self.run_id, seq, ref.provider, ref.kind.value, ref.external_id, change.parent,
                 None if change.operation is Operation.DELETE else change.body, sim.isoformat()),
            )
        self._db.commit()
        return WorldEvent(
            seq=seq, run_id=self.run_id, wake=self._clock.wake(), sim_time=sim, wall_time=wall,
            actor=change.actor, operation=change.operation, entity=ref, after=change.after,
        )

    def get(self, entity: EntityRef) -> Stored | None:
        where, args = self._visible()
        row = self._db.execute(
            f"SELECT body, parent, seq, sim_time FROM entity_version WHERE provider=? AND kind=? AND external_id=? AND {where}"
            " ORDER BY seq DESC LIMIT 1",
            [entity.provider, entity.kind.value, entity.external_id, *args],
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return Stored(entity=entity, body=row[0], parent=row[1], seq=row[2], sim_time=datetime.fromisoformat(row[3]))

    def children(
        self, provider: ProviderKey, kind: EntityKind, parent: str | None, *, after: str | None = None, limit: int = 100
    ) -> list[Stored]:
        where, args = self._visible()
        rows = self._db.execute(
            f"""SELECT external_id, body, parent, seq, sim_time FROM entity_version v
                WHERE provider=? AND kind=? AND {where}
                  AND seq=(SELECT MAX(seq) FROM entity_version
                           WHERE provider=v.provider AND kind=v.kind AND external_id=v.external_id AND {where})
                  AND body IS NOT NULL AND parent IS ? AND external_id > ?
                ORDER BY external_id LIMIT ?""",
            [provider, kind.value, *args, *args, parent, after or "", limit],
        ).fetchall()
        return [
            Stored(entity=EntityRef(provider=provider, kind=kind, external_id=r[0]), body=r[1], parent=r[2], seq=r[3],
                   sim_time=datetime.fromisoformat(r[4]))
            for r in rows
        ]

    def events(self, *, since: int = 0) -> list[WorldEvent]:
        where, args = self._visible()
        rows = self._db.execute(
            f"SELECT run_id, seq, wake, sim_time, wall_time, actor, operation, provider, kind, external_id, after"
            f" FROM event WHERE {where} AND seq>? ORDER BY seq",
            [*args, since],
        ).fetchall()
        attached = {
            first: Exchange.model_validate_json(text)
            for first, text in self._db.execute(
                f"SELECT first_seq, exchange FROM exchange WHERE {where.replace('seq<=', 'first_seq<=')}", args
            )
        }
        return [
            WorldEvent(
                seq=r[1], run_id=r[0], wake=r[2], sim_time=datetime.fromisoformat(r[3]),
                wall_time=datetime.fromisoformat(r[4]), actor=Actor(r[5]), operation=Operation(r[6]),
                entity=EntityRef(provider=r[7], kind=EntityKind(r[8]), external_id=r[9]),
                after=_SNAPSHOT.validate_json(r[10]) if r[10] is not None else None,
                exchange=attached.get(r[1]),
            )
            for r in rows
        ]

    def attach(self, exchange: Exchange, *, first_seq: int, last_seq: int) -> None:
        self._db.execute(
            "INSERT INTO exchange VALUES(?,?,?,?)", (self.run_id, first_seq, last_seq, exchange.model_dump_json())
        )
        self._db.commit()

    def remember(self, reply: PersonReply) -> None:
        position = self._db.execute("SELECT COUNT(*) FROM reply WHERE run_id=?", (self.run_id,)).fetchone()[0]
        self._db.execute("INSERT INTO reply VALUES(?,?,?)", (self.run_id, position, reply.model_dump_json()))
        self._db.commit()

    def replies(self) -> list[PersonReply]:
        rows = self._db.execute("SELECT reply FROM reply WHERE run_id=? ORDER BY position", (self.run_id,)).fetchall()
        return [PersonReply.model_validate_json(r[0]) for r in rows]

    def fork(self, run_id: str, *, at_seq: int) -> "SqliteStore":
        if not 0 <= at_seq <= self.head():
            raise ValueError(f"cannot fork at {at_seq}: this run's head is {self.head()}")
        self._db.execute("INSERT INTO run(run_id, parent, forked_at) VALUES(?,?,?)", (run_id, self.run_id, at_seq))
        self._db.commit()
        return SqliteStore(self._path, run_id, self._clock)
