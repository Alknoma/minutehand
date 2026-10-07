"""examples/state/postgres, end to end against a real PostgreSQL: the agent's database fronted by Minutehand's relay,
its committed writes recorded, and a fork from the middle of the run that puts the database back from the base
and those writes, with no state hooks. Needs Docker, or MINUTEHAND_TEST_POSTGRES naming a server
(postgres://user:password@host:port) the tests may create and drop databases on; run with -m docker."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from minutehand import session
from minutehand.adapters.database.postgres.conversation import CHANGES, WRITE_IN_SELECT
from minutehand.application.checkpoint import CHECKPOINT, Replayable
from minutehand.application.databases import base_name, records
from minutehand.application.files import load_agent, load_fork, load_scenario
from minutehand.application.refusals import RunRefused
from minutehand.application.restore import RestoreStep
from minutehand.domain.agent import AgentUnderTest, WakeReason
from minutehand.domain.checks import Effectiveness, Finding
from minutehand.domain.database import BaseTaken, Committed, Database, SequencesMoved
from minutehand.domain.emulator import EmulatorChange
from minutehand.domain.run import RunRecord, StopReason
from minutehand.domain.scenario import Scenario
from minutehand.domain.world import RecordedCall, WorldEvent
from tests.e2e.support import free_port

pytestmark = pytest.mark.docker

RECIPE = Path(__file__).parents[2] / "examples" / "state" / "postgres"
SQLITE = Path(__file__).parents[2] / "examples" / "state" / "sqlite"
IMAGE = "postgres:16"


@pytest.fixture(scope="module")
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


def create(server: str, database: str) -> None:
    with psycopg.connect(f"{server}/postgres", autocommit=True) as db:
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


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


class AtCheckpoints:
    """`ports.telemetry.Telemetry` that takes the database's digest each time a checkpoint enters the log: the
    agent's wake is over then, so the digest is the database at that checkpoint."""

    def __init__(self, server: str, database: str) -> None:
        self.server = server
        self.database = database
        self.digests: dict[int, str] = {}

    def recorded(self, event: WorldEvent) -> None:
        if event.entity == CHECKPOINT:
            self.digests[event.seq] = digest(self.server, self.database)

    def run_started(self, run_id: str, scenario: Scenario) -> None: ...
    def wake_started(self, wake: int, reason: WakeReason, now: datetime) -> None: ...
    def captured(self, call: RecordedCall) -> None: ...
    def emulator_changed(self, change: EmulatorChange) -> None: ...
    def wake_ended(self, wake: int) -> None: ...
    def found(self, finding: Finding) -> None: ...
    def run_ended(self, record: RunRecord, effectiveness: Effectiveness | None) -> None: ...


def recipe_agent(port: int, listen: int, upstream: str) -> AgentUnderTest:
    """agent.yaml as written, on ports of the test's own, fronting a database of the test's own."""
    agent = load_agent(RECIPE / "agent.yaml")
    moved = AgentUnderTest.model_validate_json(agent.model_dump_json().replace("127.0.0.1:8701", f"127.0.0.1:{port}"))
    [database] = moved.databases
    fronted = Database.model_validate({**database.model_dump(), "listen": f"127.0.0.1:{listen}", "upstream": upstream})
    return moved.model_copy(update={"databases": [fronted]})


@pytest.fixture
def fresh(server: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, int]]:
    """A database of the test's own, empty, and the agent's port."""
    name = f"follow_up_{secrets.token_hex(4)}"
    create(server, name)
    port = free_port()
    monkeypatch.setenv("PORT", str(port))
    try:
        yield name, port
    finally:
        with psycopg.connect(f"{server}/postgres", autocommit=True) as db:
            for (other,) in db.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (f"{name}%",)):
                db.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(other)))


def command() -> list[str]:
    return [sys.executable, str(RECIPE / "agent.py")]


async def test_a_fork_from_the_middle_puts_the_database_back_as_it_was_there_from_the_base_and_the_writes(
    tmp_path: Path, server: str, fresh: tuple[str, int]
) -> None:
    name, port = fresh
    state = tmp_path / "state"
    agent = recipe_agent(port, free_port(), f"{server}/{name}")
    watched = AtCheckpoints(server, name)
    began = time.monotonic()
    [parent] = await session.play(
        load_scenario(SQLITE / "scenario_silent.yaml"), agent, state=state, command=command(), telemetry=watched
    )
    played = time.monotonic() - began
    assert parent.record.stop is StopReason.NOTHING_PENDING, parent.record.failure
    points = session.fork_points(state, parent.record.run_id)
    assert [p.wake for p in points] == [0, 1, 2, 3, 3]
    assert all(isinstance(p.agent, Replayable) for p in points)
    after_ask = points[1]
    at_end = digest(server, name)
    assert watched.digests[after_ask.seq] != at_end  # the parent wrote on after the checkpoint

    world = session.run_dir(state, parent.record.run_id) / session.WORLD
    with session.reading(state, parent.record.run_id) as kept:
        held = records(kept)
    [base] = [r for r in held if isinstance(r, BaseTaken)]
    assert (base.base, base.how) == (base_name(name, parent.record.run_id), "template")
    committed = [r for r in held if isinstance(r, Committed)]
    statements = [s for c in committed for s in c.statements]
    # Only the writes: no statement is kept that changed nothing, and the reads the agent made are not there.
    assert all(
        any(t.split(" ")[0] in CHANGES for t in s.tags) or WRITE_IN_SELECT.search(s.sql) is not None for s in statements
    )
    assert not any(s.sql.startswith("SELECT * FROM") for s in statements)
    # The rolled-back draft of every wake is recorded nowhere; the ids it drew are.
    assert not any("a draft the agent thought better of" in (p or "") for s in statements for p in s.params)
    moved = [r for r in held if isinstance(r, SequencesMoved)]
    assert len(moved) == 3  # one draft per wake, three wakes
    notes = [s for s in statements if s.sql.startswith("INSERT INTO note")]
    assert len(notes) == 3 and all(s.rows is not None for s in notes)

    said: list[str] = []
    restored_digest: list[str] = []

    def heard(line: str) -> None:
        said.append(line)
        if line.startswith("database: database app made again"):
            restored_digest.append(digest(server, name))

    changes = load_fork(SQLITE / "fork_rosa_answers.yaml", parent_run=parent.record.run_id, at_seq=after_ask.seq)
    began = time.monotonic()
    [child] = await session.fork(parent.record.run_id, changes, state=state, command=command(), progress=heard)
    forked = time.monotonic() - began

    assert restored_digest == [watched.digests[after_ask.seq]], said
    assert child.record.stop is StopReason.AGENT_DONE, child.record.failure
    restored = session.restore_of(state, child.record.run_id)
    assert restored is not None and restored.verified, restored
    assert [s.step for s in restored.steps] == [
        RestoreStep.STOP,
        RestoreStep.DATABASE,
        RestoreStep.START,
        RestoreStep.ANSWER,
        RestoreStep.VERIFY,
    ]
    replayed_seconds = next(s.seconds for s in restored.steps if s.step is RestoreStep.DATABASE)

    recorded_bytes = sum(len(r.model_dump_json()) for r in held)
    with sqlite3.connect(world) as db:
        world_bytes = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
    with psycopg.connect(f"{server}/{name}") as db:
        size = db.execute("SELECT pg_database_size(current_database())").fetchone()
    assert size is not None
    print(
        f"\nmeasured: played {played:.1f} s, forked {forked:.1f} s; base taken in {base.seconds:.3f} s; "
        f"{len(committed)} committed transactions, {len(statements)} statements, {len(moved)} sequence moves, "
        f"{len(held)} records of {recorded_bytes} bytes as JSON (the whole world file: {world_bytes} bytes), against "
        f"a database of {size[0]} bytes; "
        f"put back and replayed in {replayed_seconds:.3f} s"
    )


async def test_a_replay_that_parts_from_the_record_is_refused_and_leaves_no_run(
    tmp_path: Path, server: str, fresh: tuple[str, int]
) -> None:
    name, port = fresh
    state = tmp_path / "state"
    agent = recipe_agent(port, free_port(), f"{server}/{name}")
    [parent] = await session.play(load_scenario(SQLITE / "scenario_silent.yaml"), agent, state=state, command=command())
    after_ask = session.fork_points(state, parent.record.run_id)[1]
    # The base is changed behind the record's back: the agent's first `INSERT ... ON CONFLICT DO NOTHING` into
    # `job` now inserts nothing, where it inserted a row when the agent ran it.
    base = base_name(name, parent.record.run_id)
    with psycopg.connect(f"{server}/{base}") as db:
        db.execute("CREATE TABLE job(id integer PRIMARY KEY, status text NOT NULL, next_wake timestamptz, "
                   "followed_up boolean NOT NULL, answer text)")  # fmt: skip
        db.execute("INSERT INTO job VALUES (1, 'tampered', NULL, false, NULL)")
    directories = sorted(p.name for p in (state / session.RUNS).iterdir())
    changes = load_fork(SQLITE / "fork_rosa_answers.yaml", parent_run=parent.record.run_id, at_seq=after_ask.seq)

    with pytest.raises(RunRefused) as refused:
        await session.fork(parent.record.run_id, changes, state=state, command=command())

    message = str(refused.value)
    assert "failed at step `database`" in message, message
    assert "parted from the record" in message
    assert "answered ['INSERT 0 0'] on replay; the agent's answered ['INSERT 0 1']" in message
    assert sorted(p.name for p in (state / session.RUNS).iterdir()) == directories
