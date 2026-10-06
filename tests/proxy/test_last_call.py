"""The proxy sees every outbound call of the agent's, answered or refused, so a checkpoint can wait for quiet."""

from __future__ import annotations

import time
from pathlib import Path

from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from tests.proxy.support import client


async def test_every_call_answered_or_refused_is_the_latest_seen_until_the_next(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path
) -> None:
    async with Proxy(Routing(registry), store, clock, confdir=tmp_path / "ca") as proxy:
        assert proxy.last_call() is None
        async with client(proxy, proxy.ca_cert) as http:
            before = time.monotonic()
            await http.post("https://acme.ledger.test/api/v2/entries?token=hush", json={"text": "filed"})
            answered = proxy.last_call()
            await http.get("https://api.unclaimed.example/v1/ping")
            refused = proxy.last_call()

    assert answered is not None and answered.at >= before
    assert answered.what == "POST acme.ledger.test/api/v2/entries?token=%5Bredacted%5D"
    assert refused is not None and refused.at >= answered.at
    assert refused.what == "GET api.unclaimed.example/v1/ping"
