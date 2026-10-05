"""The agent's StateHooks: its SQLite file, copied into a snapshot and back.

    python hooks.py snapshot     copies AGENT_DB into $MINUTEHAND_SNAPSHOT_DIR/agent.db
    python hooks.py restore      copies it back over AGENT_DB
    python hooks.py busy         exits 1: this agent does all its work inside the request that wakes it
    python hooks.py fingerprint  prints a digest of every table's rows, in no particular order, leaving out the
                                 columns VOLATILE names

Both copies use SQLite's online backup, not a file copy: a snapshot taken while the agent has the file open
is a consistent one, and a restore replaces the whole database in one transaction, write-ahead log and all,
where copying the file would leave a stale `-wal` beside it. Minutehand stops the agent before a restore and
starts it after, so nothing the agent held is kept across it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

DATABASE = Path(os.environ.get("AGENT_DB", "agent.db"))
SNAPSHOT = Path(os.environ["MINUTEHAND_SNAPSHOT_DIR"]) / "agent.db"
VOLATILE: frozenset[str] = frozenset()
"""Columns a restore need not bring back (a timestamp from the machine's clock); this agent writes none."""


def copy(source: Path, target: Path) -> None:
    if not source.is_file():
        sys.exit(f"there is no database at {source} to copy")
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def fingerprint(path: Path) -> str:
    """The same digest for the same rows, whatever order they were written in."""
    with closing(sqlite3.connect(path)) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        held = {}
        for table in tables:
            cursor = db.execute(f'SELECT * FROM "{table}"')
            names = [d[0] for d in cursor.description]
            rows = [{n: v for n, v in zip(names, row, strict=True) if n not in VOLATILE} for row in cursor]
            held[table] = sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)
    return hashlib.sha256(json.dumps(held, sort_keys=True).encode()).hexdigest()


if __name__ == "__main__":
    if sys.argv[1:] == ["snapshot"]:
        copy(DATABASE, SNAPSHOT)
        print(f"snapshot of {DATABASE} in {SNAPSHOT}")
    elif sys.argv[1:] == ["restore"]:
        copy(SNAPSHOT, DATABASE)
        print(f"{DATABASE} restored from {SNAPSHOT}")
    elif sys.argv[1:] == ["busy"]:
        print("idle: the agent works only inside the request that wakes it, and has answered it")
        sys.exit(1)
    elif sys.argv[1:] == ["fingerprint"]:
        print(fingerprint(DATABASE))
    else:
        sys.exit("usage: hooks.py snapshot|restore|busy|fingerprint")
