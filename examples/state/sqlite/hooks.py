"""The agent's StateHooks: its SQLite file, copied into a snapshot and back.

    python hooks.py snapshot     copies AGENT_DB into $MINUTEHAND_SNAPSHOT_DIR/agent.db
    python hooks.py restore      copies it back over AGENT_DB

Both copies use SQLite's online backup, not a file copy: a snapshot taken while the agent has the file open
is a consistent one, and a restore replaces the whole database in one transaction, write-ahead log and all,
where copying the file would leave a stale `-wal` beside it. Minutehand stops the agent before a restore and
starts it after, so nothing the agent held is kept across it.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

DATABASE = Path(os.environ.get("AGENT_DB", "agent.db"))
SNAPSHOT = Path(os.environ["MINUTEHAND_SNAPSHOT_DIR"]) / "agent.db"


def copy(source: Path, target: Path) -> None:
    if not source.is_file():
        sys.exit(f"there is no database at {source} to copy")
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


if __name__ == "__main__":
    if sys.argv[1:] == ["snapshot"]:
        copy(DATABASE, SNAPSHOT)
        print(f"snapshot of {DATABASE} in {SNAPSHOT}")
    elif sys.argv[1:] == ["restore"]:
        copy(SNAPSHOT, DATABASE)
        print(f"{DATABASE} restored from {SNAPSHOT}")
    else:
        sys.exit("usage: hooks.py snapshot|restore")
