"""Where the reference agent keeps everything it knows, in one of two interchangeable databases:

    REFERENCE_DB=path/to/agent.db                  a SQLite file (the default, `agent.db` in REFERENCE_HOME)
    REFERENCE_FIRESTORE=127.0.0.1:8085             a Firestore emulator, through its REST API
    REFERENCE_FIRESTORE_PROJECT=demo-minutehand    its project

Both hold the same five things:

    facts     what the job is and where it stands: the goal, the owner, the venue, whether it is done, the next
              moment it wants to be woken, and the job queue's counter
    jobs      the queue the worker polls: one job per wake, queued by the API, run by the worker
    sent      every email the agent sent, with the message id the email API gave it
    replies   every email answer that reached the inbound webhook
    notes     what the owner said along the way

`digest()` is a stable digest of all of it, the same whatever order rows were written in, leaving out the
columns that say when something happened on the machine's clock (VOLATILE): two databases holding the same
facts give the same digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

VOLATILE = frozenset({"queued_wall", "finished_wall"})
"""Columns written from the machine's clock, which a restore cannot and need not bring back."""


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, object]


class Store(Protocol):
    def fact(self, key: str) -> str | None: ...

    def set_facts(self, values: dict[str, str | None]) -> None: ...

    def enqueue(self, kind: str, payload: dict[str, object], wall: float) -> int: ...

    def claim(self) -> Job | None: ...

    def finish(self, job: Job, wall: float) -> None: ...

    def in_flight(self) -> int: ...

    def queued(self) -> int: ...

    def add_sent(self, row: dict[str, str]) -> None: ...

    def sent(self) -> list[dict[str, str]]: ...

    def add_reply(self, row: dict[str, str]) -> bool: ...

    def replies(self) -> list[dict[str, str]]: ...

    def mark_read(self, reply_id: str) -> None: ...

    def add_note(self, row: dict[str, str]) -> None: ...

    def digest(self) -> str: ...


def open_store() -> Store:
    if os.environ.get("REFERENCE_FIRESTORE"):
        return FirestoreStore(
            os.environ["REFERENCE_FIRESTORE"], os.environ.get("REFERENCE_FIRESTORE_PROJECT", "demo-minutehand")
        )
    home = Path(os.environ.get("REFERENCE_HOME", "."))
    return SqliteStore(Path(os.environ.get("REFERENCE_DB", str(home / "agent.db"))))


def _digest(tables: dict[str, list[dict[str, object]]]) -> str:
    canonical = {
        name: sorted(
            json.dumps({k: v for k, v in row.items() if k not in VOLATILE}, sort_keys=True, default=str) for row in rows
        )
        for name, rows in sorted(tables.items())
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


# -- SQLite ----------------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
  state TEXT NOT NULL, queued_wall REAL, finished_wall REAL);
CREATE TABLE IF NOT EXISTS sent(id TEXT PRIMARY KEY, kind TEXT, to_addr TEXT, subject TEXT, text TEXT,
  at TEXT, message_id TEXT);
CREATE TABLE IF NOT EXISTS replies(id TEXT PRIMARY KEY, from_addr TEXT, text TEXT, in_reply_to TEXT, read INTEGER);
CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, text TEXT, at TEXT);
"""


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        for attempt in range(50):  # the API and the worker open the file at the same moment as they start
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.executescript(SCHEMA)
                break
            except sqlite3.OperationalError:
                if attempt == 49:
                    raise
                time.sleep(0.1)
        self._lock = threading.Lock()

    def _rows(self, sql: str, *args: object) -> list[dict[str, object]]:
        with self._lock:
            cursor = self._db.execute(sql, args)
            names = [d[0] for d in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def _do(self, sql: str, *args: object) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    def fact(self, key: str) -> str | None:
        rows = self._rows("SELECT value FROM facts WHERE key = ?", key)
        value = rows[0]["value"] if rows else None
        return value if isinstance(value, str) else None

    def set_facts(self, values: dict[str, str | None]) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            for key, value in values.items():
                if value is None:
                    self._db.execute("DELETE FROM facts WHERE key = ?", (key,))
                else:
                    self._db.execute("INSERT OR REPLACE INTO facts(key, value) VALUES (?, ?)", (key, value))
            self._db.execute("COMMIT")

    def enqueue(self, kind: str, payload: dict[str, object], wall: float) -> int:
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO jobs(kind, payload, state, queued_wall) VALUES (?, ?, 'queued', ?)",
                (kind, json.dumps(payload, sort_keys=True), wall),
            )
            return int(cursor.lastrowid or 0)

    def claim(self) -> Job | None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT id, kind, payload FROM jobs WHERE state = 'queued' ORDER BY id LIMIT 1")
            found = row.fetchone()
            if found is not None:
                self._db.execute("UPDATE jobs SET state = 'running' WHERE id = ?", (found[0],))
            self._db.execute("COMMIT")
        return Job(found[0], found[1], json.loads(found[2])) if found is not None else None

    def finish(self, job: Job, wall: float) -> None:
        self._do("UPDATE jobs SET state = 'done', finished_wall = ? WHERE id = ?", wall, job.id)

    def in_flight(self) -> int:
        rows = self._rows("SELECT count(*) AS n FROM jobs WHERE state IN ('queued', 'running')")
        return int(str(rows[0]["n"]))

    def queued(self) -> int:
        rows = self._rows("SELECT count(*) AS n FROM jobs WHERE state = 'queued'")
        return int(str(rows[0]["n"]))

    def add_sent(self, row: dict[str, str]) -> None:
        self._do(
            "INSERT INTO sent(id, kind, to_addr, subject, text, at, message_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            row["id"],
            row["kind"],
            row["to_addr"],
            row["subject"],
            row["text"],
            row["at"],
            row["message_id"],
        )

    def sent(self) -> list[dict[str, str]]:
        return [{k: str(v) for k, v in r.items()} for r in self._rows("SELECT * FROM sent ORDER BY at, id")]

    def add_reply(self, row: dict[str, str]) -> bool:
        return (
            self._do(
                "INSERT OR IGNORE INTO replies(id, from_addr, text, in_reply_to, read) VALUES (?, ?, ?, ?, 0)",
                row["id"],
                row["from_addr"],
                row["text"],
                row["in_reply_to"],
            )
            == 1
        )

    def replies(self) -> list[dict[str, str]]:
        return [{k: str(v) for k, v in r.items()} for r in self._rows("SELECT * FROM replies ORDER BY id")]

    def mark_read(self, reply_id: str) -> None:
        self._do("UPDATE replies SET read = 1 WHERE id = ?", reply_id)

    def add_note(self, row: dict[str, str]) -> None:
        self._do("INSERT OR IGNORE INTO notes(id, text, at) VALUES (?, ?, ?)", row["id"], row["text"], row["at"])

    def digest(self) -> str:
        return _digest({t: self._rows(f"SELECT * FROM {t}") for t in ("facts", "jobs", "sent", "replies", "notes")})


# -- Firestore, over the emulator's REST API --------------------------------------------------------------------------


class FirestoreStore:
    """The same store as documents: facts/{key}, jobs/{id}, sent/{id}, replies/{id}, notes/{id}. One worker
    claims jobs, so a claim is a plain update; the queue's counter is a fact only the API moves."""

    def __init__(self, host: str, project: str) -> None:
        self._base = f"http://{host}/v1/projects/{project}/databases/(default)/documents"
        self._lock = threading.Lock()

    def _call(self, method: str, path: str, body: object | None = None) -> dict[str, object] | None:
        request = urllib.request.Request(
            f"{self._base}/{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"authorization": "Bearer owner", "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return json.loads(raw) if raw else {}

    @staticmethod
    def _fields(row: dict[str, object]) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in row.items():
            if v is None:
                out[k] = {"nullValue": None}
            elif isinstance(v, bool):
                out[k] = {"booleanValue": v}
            elif isinstance(v, int):
                out[k] = {"integerValue": str(v)}
            elif isinstance(v, float):
                out[k] = {"doubleValue": v}
            else:
                out[k] = {"stringValue": str(v)}
        return out

    @staticmethod
    def _plain(document: dict[str, object]) -> dict[str, object]:
        fields = document.get("fields") or {}
        assert isinstance(fields, dict)
        out: dict[str, object] = {}
        for k, typed in fields.items():
            kind, value = next(iter(typed.items()))
            out[k] = int(value) if kind == "integerValue" else value
        return out

    def _put(self, collection: str, key: str, row: dict[str, object]) -> None:
        self._call("PATCH", f"{collection}/{key}", {"fields": self._fields(row)})

    def _all(self, collection: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        token = ""
        while True:
            page = self._call("GET", f"{collection}?pageSize=300{'&pageToken=' + token if token else ''}") or {}
            for document in page.get("documents") or []:
                assert isinstance(document, dict)
                found.append({"_id": str(document["name"]).rsplit("/", 1)[1], **self._plain(document)})
            token = str(page.get("nextPageToken") or "")
            if not token:
                return found

    def fact(self, key: str) -> str | None:
        document = self._call("GET", f"facts/{key}")
        if document is None:
            return None
        value = self._plain(document).get("value")
        return value if isinstance(value, str) else None

    def set_facts(self, values: dict[str, str | None]) -> None:
        for key, value in values.items():
            if value is None:
                self._call("DELETE", f"facts/{key}")
            else:
                self._put("facts", key, {"value": value})

    def enqueue(self, kind: str, payload: dict[str, object], wall: float) -> int:
        with self._lock:
            number = int(self.fact("job_counter") or "0") + 1
            self.set_facts({"job_counter": str(number)})
        self._put(
            "jobs",
            f"{number:08d}",
            {"kind": kind, "payload": json.dumps(payload, sort_keys=True), "state": "queued", "queued_wall": wall},
        )
        return number

    def claim(self) -> Job | None:
        queued = sorted((j for j in self._all("jobs") if j.get("state") == "queued"), key=lambda j: str(j["_id"]))
        if not queued:
            return None
        first = queued[0]
        self._call(
            "PATCH",
            f"jobs/{first['_id']}?updateMask.fieldPaths=state",
            {"fields": {"state": {"stringValue": "running"}}},
        )
        return Job(int(str(first["_id"])), str(first["kind"]), json.loads(str(first["payload"])))

    def finish(self, job: Job, wall: float) -> None:
        self._call(
            "PATCH",
            f"jobs/{job.id:08d}?updateMask.fieldPaths=state&updateMask.fieldPaths=finished_wall",
            {"fields": {"state": {"stringValue": "done"}, "finished_wall": {"doubleValue": wall}}},
        )

    def in_flight(self) -> int:
        return sum(1 for j in self._all("jobs") if j.get("state") in ("queued", "running"))

    def queued(self) -> int:
        return sum(1 for j in self._all("jobs") if j.get("state") == "queued")

    def add_sent(self, row: dict[str, str]) -> None:
        self._put("sent", row["id"], dict(row))

    def sent(self) -> list[dict[str, str]]:
        rows = [{k: str(v) for k, v in r.items() if k != "_id"} for r in self._all("sent")]
        return sorted(rows, key=lambda r: (r["at"], r["id"]))

    def add_reply(self, row: dict[str, str]) -> bool:
        if self._call("GET", f"replies/{row['id']}") is not None:
            return False
        self._put("replies", row["id"], {**row, "read": 0})
        return True

    def replies(self) -> list[dict[str, str]]:
        rows = [{k: str(v) for k, v in r.items() if k != "_id"} for r in self._all("replies")]
        return sorted(rows, key=lambda r: r["id"])

    def mark_read(self, reply_id: str) -> None:
        self._call(
            "PATCH", f"replies/{reply_id}?updateMask.fieldPaths=read", {"fields": {"read": {"integerValue": "1"}}}
        )

    def add_note(self, row: dict[str, str]) -> None:
        self._put("notes", row["id"], dict(row))

    def digest(self) -> str:
        tables = {}
        for collection in ("facts", "jobs", "sent", "replies", "notes"):
            tables[collection] = self._all(collection)
        return _digest(tables)
