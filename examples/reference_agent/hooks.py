"""The reference agent's state hooks, for either database (see store.py):

    python hooks.py snapshot      the database into $MINUTEHAND_SNAPSHOT_DIR
    python hooks.py restore       the database back from it
    python hooks.py busy          exits 0 while any job is queued or running (busy), 1 when none is (idle)
    python hooks.py fingerprint   prints a digest of the database and of what each process holds in memory

SQLite is copied with SQLite's online backup. A Firestore emulator is exported while it runs and restarted with
`--import`, by the recipe in examples/state/firestore_emulator (FIRESTORE_PORT, FIRESTORE_HUB_PORT,
MINUTEHAND_FIRESTORE_PROJECT and FIRESTORE_EXPORTS reach it).

`fingerprint` covers the processes too: each writes a digest of what it holds in memory to REFERENCE_HOME as it
changes (api.memory, worker.memory), and a process restarted after a restore writes it again from the restored
database. A process the restore did not restart still holds another moment's memory, and the fingerprint
differs from the one taken at the checkpoint.

REFERENCE_RESTORE_BUG=next makes `restore` put back the snapshot taken right after the one it was given (each
snapshot also leaves a copy in REFERENCE_HOME/taken/, as a backup job might): a restore of the wrong moment, for
the tests that show verification catching one.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from contextlib import closing
from pathlib import Path

from store import open_store

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("REFERENCE_HOME", ".")).resolve()
FIRESTORE_RECIPE = HERE.parent / "state" / "firestore_emulator" / "hooks.py"
MEMORY = ("api.memory", "worker.memory")


def _snapshot_dir() -> Path:
    return Path(os.environ["MINUTEHAND_SNAPSHOT_DIR"])


TAKEN = HOME / "taken"


def _digest(path: Path) -> str:
    """Of a snapshot directory: every file's path and bytes, in order."""
    found = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        found.update(str(file.relative_to(path)).encode() + b"\0" + file.read_bytes())
    return found.hexdigest()


def _keep_copy(snapshot: Path) -> None:
    TAKEN.mkdir(exist_ok=True)
    shutil.copytree(snapshot, TAKEN / str(len([d for d in TAKEN.iterdir() if d.is_dir()])))


def _next_taken(given: Path) -> Path:
    """The copy taken right after the one equal to `given`: the bug REFERENCE_RESTORE_BUG=next makes."""
    copies = sorted((d for d in TAKEN.iterdir() if d.is_dir()), key=lambda d: int(d.name))
    digests = [_digest(d) for d in copies]
    wanted = _digest(given)
    if wanted not in digests or digests.index(wanted) + 1 >= len(copies):
        return given
    return copies[digests.index(wanted) + 1]


def _database() -> Path:
    return Path(os.environ.get("REFERENCE_DB", str(HOME / "agent.db"))).resolve()


def _copy(source: Path, target: Path) -> None:
    if not source.is_file():
        sys.exit(f"there is no database at {source} to copy")
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def _firestore(command: str, snapshot: Path) -> None:
    env = {**os.environ, "MINUTEHAND_SNAPSHOT_DIR": str(snapshot)}
    subprocess.run([sys.executable, str(FIRESTORE_RECIPE), command], env=env, check=True)


def snapshot() -> None:
    into = Path(os.environ["MINUTEHAND_SNAPSHOT_DIR"])
    if os.environ.get("REFERENCE_FIRESTORE"):
        _firestore("snapshot", into)
    else:
        _copy(_database(), into / "agent.db")
    _keep_copy(into)
    print(f"snapshot in {into}")


def restore() -> None:
    source = _snapshot_dir()
    if os.environ.get("REFERENCE_RESTORE_BUG") == "next":
        source = _next_taken(source)
    if os.environ.get("REFERENCE_FIRESTORE"):
        _firestore("restore", source)
    else:
        _copy(source / "agent.db", _database())
    print(f"restored from {source}")


def busy() -> None:
    working = open_store().in_flight()
    print(f"{working} job(s) queued or running")
    sys.exit(0 if working else 1)


def clear() -> None:
    """A Firestore emulator emptied of every document, so a run starts from nothing."""
    host = os.environ["REFERENCE_FIRESTORE"]
    project = os.environ.get("REFERENCE_FIRESTORE_PROJECT", "demo-minutehand")
    url = f"http://{host}/emulator/v1/projects/{project}/databases/(default)/documents"
    urllib.request.urlopen(urllib.request.Request(url, method="DELETE"), timeout=30).close()


def fingerprint() -> None:
    parts = [open_store().digest()]
    for name in MEMORY:
        path = HOME / name
        parts.append(f"{name}={path.read_text().strip() if path.is_file() else 'absent'}")
    print(hashlib.sha256("\n".join(parts).encode()).hexdigest())


COMMANDS = {"clear": clear, "snapshot": snapshot, "restore": restore, "busy": busy, "fingerprint": fingerprint}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: hooks.py {'|'.join(COMMANDS)}")
    COMMANDS[sys.argv[1]]()
