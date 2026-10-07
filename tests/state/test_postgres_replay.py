"""What a replay can and cannot put back, against a real PostgreSQL: several agent connections committing at once,
replayed in the order the database committed them; and a value the database made up itself, which a replay
makes up again, caught by the declared digest and by no statement's answer. Run with -m docker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import psycopg
import pytest

from minutehand.adapters.database.postgres.relay import PostgresFront
from minutehand.application.databases import base_name
from minutehand.domain.database import BaseTaken, Committed, Database, Digest
from tests.ports import free_port
from tests.state.postgres import Heard, digest

pytestmark = pytest.mark.docker


@pytest.fixture
async def relay(server: str, database: str) -> AsyncIterator[tuple[PostgresFront, Heard, BaseTaken]]:
    heard = Heard()
    front = PostgresFront(
        Database(name="app", listen=f"127.0.0.1:{free_port()}", upstream=f"{server}/{database}", digest=Digest()),
        heard,
    )
    await front.start()
    try:
        yield front, heard, await front.take_base(base_name(database, "replay"))
    finally:
        await front.stop()


async def connect(front: PostgresFront) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(front.agent_url(), autocommit=True)


async def test_two_connections_interleaving_their_transactions_are_replayed_in_commit_order_to_the_same_database(
    server: str, database: str, relay: tuple[PostgresFront, Heard, BaseTaken]
) -> None:
    front, heard, base = relay
    async with await connect(front) as setup:
        await setup.execute("CREATE TABLE account(id integer PRIMARY KEY, owner text NOT NULL, balance integer)")
        await setup.execute("CREATE TABLE counter(id integer PRIMARY KEY, n integer NOT NULL)")
        await setup.execute("INSERT INTO counter VALUES (1, 0)")
    one, two = await connect(front), await connect(front)
    try:
        # One begins first and commits last; what it writes depends on what two committed in between.
        await one.execute("BEGIN")
        await one.execute("INSERT INTO account VALUES (1, 'one', 0)")
        await two.execute("BEGIN")
        await two.execute("INSERT INTO account VALUES (2, 'two', 100)")
        await two.execute("COMMIT")
        await one.execute("UPDATE account SET balance = balance + 10 WHERE id = 2")
        await one.execute("COMMIT")

        # Then both at once: each transaction takes the next number from one counter row (whose lock orders them
        # as the database commits them) and returns it, so a replay in any other order returns other rows.
        async def writer(connection: psycopg.AsyncConnection, who: str, first: int) -> None:
            for k in range(first, first + 40):
                async with connection.transaction():
                    taken = await connection.execute("UPDATE counter SET n = n + 1 WHERE id = 1 RETURNING n")
                    [(n,)] = await taken.fetchall()
                    await connection.execute("INSERT INTO account VALUES (%s, %s, %s)", (k, who, n))
                await asyncio.sleep(0)

        await asyncio.gather(writer(one, "one", 100), writer(two, "two", 200))
    finally:
        await one.close()
        await two.close()

    committed = [r for r in heard.records if isinstance(r, Committed)]
    connections = [c.connection for c in committed]
    assert len(set(connections[-80:])) == 2  # both connections in the concurrent stretch
    switches = sum(1 for a, b in zip(connections[-80:], connections[-79:], strict=False) if a != b)
    assert switches > 4, connections  # and interleaved, not one after the other
    left = digest(server, database)
    replayed = await front.put_back(base, heard.history)
    assert replayed.diverged is None, replayed.diverged
    assert replayed.transactions == len(committed)
    assert digest(server, database) == left


async def test_interleaved_draws_from_one_sequence_are_refused_on_replay_rather_than_renumbered(
    server: str, database: str, relay: tuple[PostgresFront, Heard, BaseTaken]
) -> None:
    front, heard, base = relay
    async with await connect(front) as setup:
        await setup.execute("CREATE TABLE note(id serial PRIMARY KEY, body text NOT NULL)")
    one, two = await connect(front), await connect(front)
    try:
        await one.execute("BEGIN")
        await one.execute("INSERT INTO note(body) VALUES ('one')")  # draws 1
        await two.execute("INSERT INTO note(body) VALUES ('two')")  # draws 2, commits first
        await one.execute("COMMIT")
    finally:
        await one.close()
        await two.close()
    replayed = await front.put_back(base, heard.history)
    assert replayed.diverged is not None
    assert "sequence public.note_id_seq stands at 1 on replay; it stood at 2 when the agent committed" in (
        replayed.diverged
    )


async def test_a_value_the_database_made_up_is_caught_by_the_digest_where_every_statement_answered_the_same(
    server: str, database: str, relay: tuple[PostgresFront, Heard, BaseTaken]
) -> None:
    front, heard, base = relay
    async with await connect(front) as db:
        await db.execute("CREATE TABLE seen(id integer PRIMARY KEY, at timestamptz NOT NULL, token uuid NOT NULL)")
        await db.execute("INSERT INTO seen VALUES (1, clock_timestamp(), gen_random_uuid())")
    at_the_checkpoint = await front.digest()
    replayed = await front.put_back(base, heard.history)
    assert replayed.diverged is None  # INSERT 0 1 then and now: nothing a statement answered differs
    after = await front.digest()
    assert (after.tables, after.rows) == (at_the_checkpoint.tables, at_the_checkpoint.rows) == (1, 1)
    assert after.digest != at_the_checkpoint.digest


async def test_a_database_put_back_exactly_digests_as_it_did_before(
    server: str, database: str, relay: tuple[PostgresFront, Heard, BaseTaken]
) -> None:
    front, heard, base = relay
    async with await connect(front) as db:
        await db.execute("CREATE TABLE plain(id serial PRIMARY KEY, body text)")
        await db.execute("INSERT INTO plain(body) VALUES ('a'), ('b'), (NULL)")
        await db.execute("DELETE FROM plain WHERE body = 'a'")
    before = await front.digest()
    replayed = await front.put_back(base, heard.history)
    assert replayed.diverged is None
    assert await front.digest() == before
    async with await connect(front) as db:  # one row changed beside the record: the digest sees it
        await db.execute("UPDATE plain SET body = 'c' WHERE body IS NULL")
    assert (await front.digest()).digest != before.digest
