"""How long a fork's replay takes on a long write history: 10,000 single-row transactions the agent committed
through the relay, recorded, then the database made again from its base and every one replayed and compared. The
numbers in docs/design.md ("The agent's database, recorded at the wire") are what this prints. Needs Docker, or
MINUTEHAND_TEST_POSTGRES; run with -m benchmark -s."""

from __future__ import annotations

import os
import time

import psycopg
import pytest

from minutehand.adapters.database.postgres.relay import PostgresFront
from minutehand.application.databases import base_name
from minutehand.domain.database import Committed, Database, SequencesMoved
from tests.ports import free_port
from tests.state.postgres import Heard, digest

pytestmark = pytest.mark.benchmark

TRANSACTIONS = int(os.environ.get("MINUTEHAND_REPLAY_TRANSACTIONS", "10000"))


@pytest.mark.timeout(900)
async def test_replaying_ten_thousand_recorded_transactions(server: str, database: str) -> None:
    heard = Heard()
    front = PostgresFront(
        Database(name="app", listen=f"127.0.0.1:{free_port()}", upstream=f"{server}/{database}"), heard
    )
    await front.start()
    try:
        base = await front.take_base(base_name(database, "replay_time"))
        began = time.monotonic()
        async with await psycopg.AsyncConnection.connect(front.agent_url(), autocommit=True) as db:
            await db.execute("CREATE TABLE note(id serial PRIMARY KEY, at timestamptz NOT NULL, body text NOT NULL)")
            for n in range(TRANSACTIONS):
                await db.execute(
                    "INSERT INTO note(at, body) VALUES (%s::timestamptz + %s * interval '1 minute', %s) RETURNING id",
                    ("2026-08-24T09:00:00Z", n, f"note {n}"),
                )
        recorded = time.monotonic() - began
        history = [r for r in heard.records if isinstance(r, Committed | SequencesMoved)]
        size = sum(len(r.model_dump_json()) for r in heard.records)
        left = digest(server, database)

        replayed = await front.put_back(base, history)

        assert replayed.diverged is None, replayed.diverged
        assert replayed.transactions == TRANSACTIONS + 1
        assert digest(server, database) == left
        print(
            f"\nmeasured: {replayed.transactions} transactions committed through the relay in {recorded:.1f} s; "
            f"{len(heard.records)} records of {size} bytes as JSON ({size / len(heard.records):.0f} a record); put "
            f"back and replayed in {replayed.seconds:.2f} s ({replayed.seconds / replayed.transactions * 1000:.2f} ms "
            "a transaction)"
        )
    finally:
        await front.stop()
