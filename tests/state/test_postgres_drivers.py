"""Each PostgreSQL driver an agent may use, through the relay, against a real PostgreSQL: what it commits is recorded,
and the database made again from the base with those records replayed holds exactly what the agent left (every
table's rows and every sequence), with no driver-specific code in Minutehand.

The drivers differ on the wire. psycopg 3 sends unnamed statements, text parameters by default and binary ones when
asked. asyncpg prepares named statements (`__asyncpg_stmt_N__`) once per connection and reuses them across
transactions, describes each before it binds, sends every parameter and asks for every result in binary, and sets
its session through startup parameters rather than `SET`. node-postgres sends unnamed statements with text
parameters and describes each portal. Run with -m docker."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import psycopg
import pytest

from minutehand.adapters.database.postgres.relay import PostgresFront
from minutehand.application.databases import base_name
from minutehand.domain.database import (
    BaseTaken,
    Committed,
    Database,
    NotReplayable,
    SequencesMoved,
)
from tests.ports import free_port
from tests.state.postgres import Heard, digest

pytestmark = pytest.mark.docker

NODE = Path(__file__).parent / "node_pg"
MOMENT = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
SCHEMA = (
    "CREATE TABLE item(id serial PRIMARY KEY, at timestamptz NOT NULL, local timestamptz, body jsonb, "
    "price numeric(12, 2), blob bytea, tags text[], ref uuid, done boolean NOT NULL DEFAULT false)"
)


class Fronted:
    def __init__(self, front: PostgresFront, heard: Heard, base: BaseTaken) -> None:
        self.front = front
        self.heard = heard
        self.base = base

    @property
    def url(self) -> str:
        return self.front.agent_url()


@pytest.fixture
async def fronted(server: str, database: str) -> AsyncIterator[Fronted]:
    heard = Heard()
    front = PostgresFront(
        Database(name="app", listen=f"127.0.0.1:{free_port()}", upstream=f"{server}/{database}"), heard
    )
    await front.start()
    try:
        base = await front.take_base(base_name(database, "drivers"))
        yield Fronted(front, heard, base)
    finally:
        await front.stop()


async def put_back_and_compare(server: str, database: str, fronted: Fronted) -> None:
    left = digest(server, database)
    assert not [r for r in fronted.heard.records if isinstance(r, NotReplayable)]
    replayed = await fronted.front.put_back(fronted.base, fronted.heard.history)
    assert replayed.diverged is None, replayed.diverged
    assert replayed.transactions == sum(1 for r in fronted.heard.history if isinstance(r, Committed))
    assert digest(server, database) == left


async def asyncpg_writes(url: str) -> None:
    """An agent written against asyncpg: a pool, its statement cache, binary everything, a session time zone set as
    a startup parameter, savepoints, a rollback, executemany and a prepared statement reused across transactions."""
    pool = await asyncpg.create_pool(url, min_size=2, max_size=2, server_settings={"timezone": "Asia/Tokyo"})
    try:
        async with pool.acquire() as db:
            await db.execute(SCHEMA)
            first = await db.fetchval(
                "INSERT INTO item(at, body, price, blob, tags, ref) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
                MOMENT,
                json.dumps({"who": "rosa", "n": 1}),
                Decimal("12.50"),
                b"\x00\x01\xff",
                ["venue", "offsite"],
                uuid.UUID("00000000-0000-4000-8000-000000000001"),
            )
            # A literal read in the session's time zone, which only the startup parameter sets.
            await db.execute(f"UPDATE item SET local = '2026-08-24 18:00' WHERE id = {first}")
            async with db.transaction():
                await db.execute("INSERT INTO item(at, tags) VALUES ($1, $2)", MOMENT + timedelta(hours=1), ["a"])
                try:
                    async with db.transaction():  # a savepoint, rolled back to
                        await db.execute("INSERT INTO item(at) VALUES ($1)", MOMENT + timedelta(hours=2))
                        raise LookupError("thought better of it")
                except LookupError:
                    pass
            try:
                async with db.transaction():  # rolled back whole: the id it drew stays drawn
                    await db.execute("INSERT INTO item(at) VALUES ($1)", MOMENT + timedelta(hours=3))
                    raise LookupError("thought better of it")
            except LookupError:
                pass
            await db.executemany(
                "INSERT INTO item(at, price) VALUES ($1, $2)",
                [(MOMENT + timedelta(days=d), Decimal(d)) for d in range(1, 4)],
            )
            bump = await db.prepare("UPDATE item SET price = coalesce(price, 0) + $1 WHERE id = $2 RETURNING price")
            for n in range(3):
                async with db.transaction():
                    await bump.fetchval(Decimal("0.25"), first + n)
        async with pool.acquire() as db, pool.acquire() as other:  # the pool's two connections, one after the other
            await db.execute("UPDATE item SET done = true WHERE id = $1", first)
            await other.execute("DELETE FROM item WHERE price = $1", Decimal(3))
            await other.fetchval("SELECT nextval('item_id_seq')")
    finally:
        await pool.close()


async def psycopg_writes(url: str) -> None:
    """The same, by psycopg 3: text parameters, binary ones on a binary cursor, the time zone given in `options`."""
    async with await psycopg.AsyncConnection.connect(url + "?options=-c%20TimeZone%3DAsia/Tokyo") as db:
        await db.execute(SCHEMA)
        await db.commit()
        cursor = await db.execute(
            "INSERT INTO item(at, body, price, blob, tags, ref) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (MOMENT, json.dumps({"who": "rosa"}), Decimal("12.50"), b"\x00\x01", ["venue"], uuid.uuid4()),
        )
        row = await cursor.fetchone()
        assert row is not None
        await db.execute("UPDATE item SET local = '2026-08-24 18:00' WHERE id = %s", (row[0],))
        await db.commit()
        async with db.cursor(binary=True) as binary:
            await binary.execute("INSERT INTO item(at, price) VALUES (%s, %s) RETURNING id", (MOMENT, Decimal(2)))
            await binary.fetchone()
        await db.commit()
        async with db.cursor() as many:  # psycopg pipelines these: several Bind/Execute before one Sync
            await many.executemany(
                "INSERT INTO item(at) VALUES (%s)", [(MOMENT + timedelta(days=d),) for d in range(3)]
            )
        await db.commit()
        await db.execute("INSERT INTO item(at) VALUES (%s)", (MOMENT,))
        await db.rollback()


async def test_asyncpg_writes_are_recorded_and_replayed_into_the_same_database(
    server: str, database: str, fronted: Fronted
) -> None:
    await asyncpg_writes(fronted.url)
    committed = [r for r in fronted.heard.records if isinstance(r, Committed)]
    statements = [s for c in committed for s in c.statements]
    # fetchval asks for one row: the database answers PortalSuspended and no tag, though the insert has run.
    [returning] = [s for s in statements if s.sql.startswith("INSERT INTO item(at, body")]
    assert (returning.param_formats, returning.max_rows, returning.suspended) == ([1] * 6, 1, True)
    assert any(isinstance(r, SequencesMoved) for r in fronted.heard.records)
    assert any("timezone" in setting.lower() for c in committed for setting in c.settings)
    await put_back_and_compare(server, database, fronted)


async def test_psycopg_writes_are_recorded_and_replayed_into_the_same_database(
    server: str, database: str, fronted: Fronted
) -> None:
    await psycopg_writes(fronted.url)
    await put_back_and_compare(server, database, fronted)


async def test_node_postgres_writes_are_recorded_and_replayed_into_the_same_database(
    server: str, database: str, fronted: Fronted
) -> None:
    node, npm = shutil.which("node"), shutil.which("npm")
    if node is None or npm is None:
        pytest.skip("node or npm is not on the PATH")
    if not (NODE / "node_modules" / "pg").is_dir():
        done = await asyncio.to_thread(
            subprocess.run,
            [npm, "ci", "--no-audit", "--no-fund"],
            cwd=NODE,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if done.returncode != 0:
            pytest.skip(f"npm ci failed, so node-postgres cannot run here:\n{done.stderr[-2000:]}")
    process = await asyncio.create_subprocess_exec(
        node,
        str(NODE / "writes.mjs"),
        env={**os.environ, "DATABASE_URL": fronted.url},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, out.decode()
    assert len([r for r in fronted.heard.records if isinstance(r, Committed)]) >= 4
    await put_back_and_compare(server, database, fronted)
