"""A real PostgreSQL for the tests of the agent's database recorded at the wire: one server for the whole session,
the one MINUTEHAND_TEST_POSTGRES names (postgres://user:password@host:port, on which the tests may create and drop
databases) or a `postgres:16` container of its own, and a database per test, dropped with every base taken of it."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import time
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

from minutehand.domain.database import Committed, DatabaseRecord, SequencesMoved

IMAGE = "postgres:16"


@pytest.fixture(scope="session")
def server() -> Iterator[str]:
    """A PostgreSQL server: the one MINUTEHAND_TEST_POSTGRES names, or a container of its own."""
    given = os.environ.get("MINUTEHAND_TEST_POSTGRES")
    if given:
        yield given.rstrip("/")
        return
    name = f"minutehand-ext-postgres-{secrets.token_hex(4)}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=secret",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True)
            .stdout.split(":")[-1]
            .strip()
        )
        url = f"postgresql://postgres:secret@127.0.0.1:{port}"
        give_up = time.monotonic() + 60
        while True:
            try:
                psycopg.connect(f"{url}/postgres", connect_timeout=2).close()
                break
            except psycopg.OperationalError:
                if time.monotonic() > give_up:
                    raise
                time.sleep(0.5)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def database(server: str) -> Iterator[str]:
    """An empty database of the test's own, dropped afterwards with every database whose name begins with it."""
    name = f"app_{secrets.token_hex(4)}"
    create(server, name)
    try:
        yield name
    finally:
        drop_like(server, name)


def create(server: str, database: str) -> None:
    with psycopg.connect(f"{server}/postgres", autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


def drop_like(server: str, prefix: str) -> None:
    with psycopg.connect(f"{server}/postgres", autocommit=True) as db:
        for (other,) in db.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (f"{prefix}%",)):
            db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(other)))


def exists(server: str, database: str) -> bool:
    with psycopg.connect(f"{server}/postgres") as db:
        return db.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone() is not None


def digest(server: str, database: str) -> str:
    """Every table's rows and every sequence's position, in no particular order of writing."""
    with psycopg.connect(f"{server}/{database}") as db:
        tables = [r[0] for r in db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]
        held: dict[str, object] = {}
        for table in tables:
            rows = db.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
            held[table] = sorted(json.dumps(r, default=str) for r in rows)
        held["sequences"] = db.execute("SELECT sequencename, last_value FROM pg_sequences ORDER BY 1").fetchall()
    return hashlib.sha256(json.dumps(held, sort_keys=True, default=str).encode()).hexdigest()


class Heard:
    """`ports.database.HearsWrites` for a relay started by a test: every record, in order."""

    def __init__(self) -> None:
        self.records: list[DatabaseRecord] = []

    def heard(self, record: DatabaseRecord) -> None:
        self.records.append(record)

    @property
    def history(self) -> list[Committed | SequencesMoved]:
        """What a put-back replays."""
        return [r for r in self.records if isinstance(r, Committed | SequencesMoved)]
