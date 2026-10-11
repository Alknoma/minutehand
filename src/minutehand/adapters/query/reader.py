"""Reading a run's read model with SQL: opened read-only, every statement refused but a SELECT, a row cap with
paging, and the formats the command line prints.

The world file is opened through `session.reading`, SQLite's read-only mode, so a run still being written is read as
of its last commit and nothing here can change it. The read model built from it is locked the same way: an
authorizer lets a statement read and call functions, nothing else (no write, no CREATE, no PRAGMA, no ATTACH), so a
query can change neither the run nor what the next query sees.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from collections import OrderedDict
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from minutehand import session
from minutehand.adapters.query.build import build
from minutehand.application.refusals import RunRefused
from minutehand.domain.prices import Prices
from minutehand.domain.scenario import Model

Value = str | int | float | None
Record = dict[str, Value]

MAX_ROWS = 1000
"""The most rows one page of a query answers."""
SECONDS = 30.0
"""How long one statement may run before it is stopped."""
CELL = 120
"""Characters of a cell the table format shows; json and csv show every one."""

_ALLOWED = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE})
_KEPT = 8


class QueryRefused(RunRefused):
    """A statement the read model does not run: a write, more than one statement, or SQL it cannot read."""


class Format(StrEnum):
    TABLE = "table"
    JSON = "json"
    CSV = "csv"


class Rows(Model):
    """One page of a query's answer."""

    columns: list[str]
    rows: list[list[Value]]
    offset: int = Field(ge=0, description="Rows skipped before this page")
    more: bool = Field(description="True when rows follow this page: ask again with offset + len(rows)")

    def records(self) -> list[Record]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


def resolve(state: Path, run: str) -> str:
    """A run's id from what a reader typed: the id of a run or of a fork, or the start of exactly one."""
    known = [e.run_id for e in session.logged(state)]
    if run in known:
        return run
    found = [r for r in known if r.startswith(run)]
    if len(found) == 1:
        return found[0]
    if found:
        raise RunRefused(f"{run!r} starts {len(found)} runs ({', '.join(found)}); give more of the id")
    raise RunRefused(f"no run {run} under {state / session.RUNS}; `minutehand runs` lists them")


def _stamp(state: Path, run_id: str) -> tuple[object, ...]:
    """What changes when the run's record changes: its world file, its log and its results, by size and time."""
    entry = session.find(state, run_id)
    paths = [
        session.run_dir(state, entry.root) / session.WORLD,
        session.run_dir(state, entry.root) / f"{session.WORLD}-wal",
        session.run_dir(state, run_id) / session.RECORD,
        session.run_dir(state, run_id) / session.RESULT,
    ]
    return tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in paths)


_built: OrderedDict[tuple[object, ...], bytes] = OrderedDict()


def open_model(state: Path, run_id: str, prices: Prices | None = None) -> sqlite3.Connection:
    """The read model of a run, locked to reading. Built once per state of the run's files and kept for the next
    reader in this process (one page of a query after another)."""
    key = (str(state.resolve()), run_id, (prices or Prices()).model_dump_json(), *_stamp(state, run_id))
    if key in _built:
        _built.move_to_end(key)
        image = _built[key]
    else:
        built = build(state, run_id, prices)
        image = built.serialize()
        built.close()
        _built[key] = image
        while len(_built) > _KEPT:
            _built.popitem(last=False)
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.deserialize(image)
    db.execute("PRAGMA query_only = ON")
    db.set_authorizer(_reads_only)
    return db


def _reads_only(action: int, *_: str | None) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def query(db: sqlite3.Connection, sql: str, *, limit: int | None = None, offset: int = 0) -> Rows:
    """Run one SELECT; answer up to `limit` rows after `offset` (every row when `limit` is None)."""
    if limit is not None and not 1 <= limit <= MAX_ROWS:
        raise QueryRefused(f"limit is from 1 to {MAX_ROWS}, not {limit}")
    if offset < 0:
        raise QueryRefused(f"offset is 0 or more, not {offset}")
    deadline = time.monotonic() + SECONDS
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        cursor = db.execute(sql)
        if cursor.description is None:
            raise QueryRefused("the statement answers no rows: the read model runs SELECT (and WITH ... SELECT) only")
        columns = [d[0] for d in cursor.description]
        for _ in range(offset):
            if cursor.fetchone() is None:
                break
        fetched = cursor.fetchall() if limit is None else cursor.fetchmany(limit + 1)
    except sqlite3.DatabaseError as e:
        raise QueryRefused(_said(e)) from e
    finally:
        db.set_progress_handler(None, 0)
    more = limit is not None and len(fetched) > limit
    page = fetched[:limit] if limit is not None else fetched
    return Rows(columns=columns, rows=[[_value(v) for v in r] for r in page], offset=offset, more=more)


def _said(error: sqlite3.DatabaseError) -> str:
    text = str(error)
    if "authoriz" in text or "readonly" in text or "read-only" in text:
        return f"{text}: the read model is read-only, and runs SELECT (and WITH ... SELECT) only"
    if "one statement" in text:
        return f"{text} Give one SELECT."
    if "interrupted" in text:
        return f"the statement ran longer than {SECONDS:.0f} seconds and was stopped"
    return f"{text}; `minutehand query --schema` lists every view and column"


def _value(value: object) -> Value:
    if value is None or isinstance(value, str | int | float):
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def formatted(rows: Rows, shape: Format) -> str:
    if shape is Format.JSON:
        return json.dumps(rows.records(), ensure_ascii=False, indent=2) + "\n"
    if shape is Format.CSV:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(rows.columns)
        writer.writerows(rows.rows)
        return out.getvalue()
    cells = [[_cell(v) for v in row] for row in rows.rows]
    widths = [max([len(c), *(len(r[i]) for r in cells)]) for i, c in enumerate(rows.columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(rows.columns, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    lines.append(f"({len(rows.rows)} row{'' if len(rows.rows) == 1 else 's'})")
    return "\n".join(lines) + "\n"


def _cell(value: Value) -> str:
    if value is None:
        return "NULL"
    text = " ".join(str(value).split())
    return text if len(text) <= CELL else text[: CELL - 1] + "…"


def export(db: sqlite3.Connection, path: Path) -> None:
    """The read model written as an SQLite file of its own, which any SQLite client reads without Minutehand."""
    if path.exists():
        raise QueryRefused(f"{path} exists; give a path that does not")
    target = sqlite3.connect(path)
    try:
        db.backup(target)
    finally:
        target.close()
