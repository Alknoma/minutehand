"""What the proxy tests share besides fixtures."""

from __future__ import annotations

import sqlite3
import ssl
from datetime import UTC, datetime
from pathlib import Path

import httpx

from minutehand.adapters.proxy.server import Proxy
from minutehand.domain.world import Exchange

PROVIDERS = "tests.proxy.providers"
START = datetime(2026, 8, 24, 10, 50, tzinfo=UTC)


def client(proxy: Proxy, trust: Path) -> httpx.AsyncClient:
    """A client that reaches everything through the proxy and trusts only `trust`."""
    return httpx.AsyncClient(proxy=proxy.url, verify=ssl.create_default_context(cafile=str(trust)), trust_env=False)


def exchanges(world_path: Path) -> list[tuple[int, int, Exchange]]:
    """Every attached exchange, including those that produced no event and so no `Store.events` row."""
    with sqlite3.connect(world_path) as db:
        rows = db.execute("SELECT first_seq, last_seq, exchange FROM exchange ORDER BY rowid").fetchall()
    return [(r[0], r[1], Exchange.model_validate_json(r[2])) for r in rows]


def stored_bytes(world_path: Path) -> bytes:
    """Everything SQLite holds for the run, the write-ahead log included."""
    return b"".join(p.read_bytes() for p in world_path.parent.glob(world_path.name + "*"))
