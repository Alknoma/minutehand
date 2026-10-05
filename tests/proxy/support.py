"""What the proxy tests share besides fixtures."""

from __future__ import annotations

import sqlite3
import ssl
from datetime import UTC, datetime
from pathlib import Path

import httpx

from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.world import Exchange
from tests.support.stored import everything

PROVIDERS = "tests.proxy.providers"
START = datetime(2026, 8, 24, 10, 50, tzinfo=UTC)


def client(proxy: Proxy, trust: Path) -> httpx.AsyncClient:
    """A client that reaches everything through the proxy and trusts only `trust`."""
    return httpx.AsyncClient(proxy=proxy.url, verify=ssl.create_default_context(cafile=str(trust)), trust_env=False)


def exchanges(world_path: Path) -> list[tuple[int, int, Exchange]]:
    """Every attached exchange of the file's root run, including those that produced no event and so no
    `Store.events` row, read back through the store as any reader of the record would."""
    with sqlite3.connect(world_path) as db:
        [root] = db.execute("SELECT run_id FROM run WHERE parent IS NULL").fetchone()
    store = SqliteStore(world_path, root, RunClock(START))
    try:
        return [(call.first_seq, call.last_seq, call.exchange) for call in store.calls()]
    finally:
        store.close()


def stored_bytes(world_path: Path) -> bytes:
    """Everything the store keeps for the run beside `world_path`: the file, its write-ahead log, and every stored
    body and snapshot file decompressed."""
    return everything(world_path.parent)
