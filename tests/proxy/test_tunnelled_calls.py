"""A call to a model API the proxy tunnels untouched is in the record: it cannot be opened, but that it happened
is kept, one record per burst on the connection, in the wake the burst began in.

Before, a tunnelled call was recorded nowhere: `Store.calls()` held nothing of it, so what passed through could not
be read from the record at all. The model API is a local HTTPS server under its own CA (`tests/proxy/upstream.py`);
the client trusts only that CA, so a call the proxy opened instead of tunnelling would fail.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from minutehand.adapters.proxy.addon import BURST_QUIET
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.outbound import described, outbound_uses
from minutehand.application.run_clock import RunClock
from minutehand.domain.world import RecordedCall, Tunnelled, TunnelRoute
from tests.proxy.support import client
from tests.proxy.upstream import Answer, Authority, make_authority, model_api

MODEL_HOST = "localhost"
URL = "https://localhost:{port}/v1/chat/completions"
QUIET = BURST_QUIET + 0.5


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return make_authority(tmp_path / "upstream-ca")


def _tunnelled(store: SqliteStore) -> list[tuple[RecordedCall, Tunnelled]]:
    return [(c, c.exchange.tunnelled) for c in store.calls() if c.exchange.tunnelled is not None]


async def test_a_tunnelled_model_call_is_recorded_with_its_bytes_and_wake(
    registry: Registry, store: SqliteStore, clock: RunClock, authority: Authority, tmp_path: Path
) -> None:
    routing = Routing(registry, model_hosts=[MODEL_HOST])
    wake = clock.begin_wake()
    async with (
        model_api(authority) as upstream,
        Proxy(routing, store, clock, confdir=tmp_path / "ca") as proxy,
        client(proxy, authority.ca_cert) as http,
    ):
        answered = await http.post(URL.format(port=upstream.port), content=b'{"model": "m"}')
        await asyncio.sleep(QUIET)
        [(call, tunnel)] = _tunnelled(store)

    assert answered.status_code == 200 and len(upstream.received) == 1
    assert call.wake == wake and call.provider is None and not call.refused
    assert call.exchange.method == "CONNECT" and call.exchange.path == f"{MODEL_HOST}:{upstream.port}"
    assert call.exchange.request_body is None and call.exchange.response_body is None
    assert tunnel.port == upstream.port and tunnel.burst == 1 and tunnel.route is TunnelRoute.RUN
    # the handshake both ways and the request and its answer, encrypted: more than the bodies alone
    assert tunnel.bytes_sent > len(b'{"model": "m"}') and tunnel.bytes_received > 0
    assert tunnel.opened <= tunnel.started <= tunnel.ended
    [use] = outbound_uses(store.calls())
    assert use.tunnelled == 1 and use.connections == 1 and use.refused == 0
    assert described(use).startswith(f"{MODEL_HOST}: 1 call on 1 tunnel relayed and never opened")


async def test_a_connection_reused_in_a_later_wake_is_a_call_in_each_wake(
    registry: Registry, store: SqliteStore, clock: RunClock, authority: Authority, tmp_path: Path
) -> None:
    """One keep-alive connection, one request in wake 1 and one in wake 2: two records, never one in wake 1."""
    routing = Routing(registry, model_hosts=[MODEL_HOST])
    async with (
        model_api(authority) as upstream,
        Proxy(routing, store, clock, confdir=tmp_path / "ca") as proxy,
        client(proxy, authority.ca_cert) as http,
    ):
        clock.begin_wake()
        assert (await http.post(URL.format(port=upstream.port), content=b"{}")).status_code == 200
        clock.begin_wake()  # no wait for the first burst to fall quiet: the wake's edge ends it
        assert (await http.post(URL.format(port=upstream.port), content=b"{}")).status_code == 200
        proxy.flush()  # as the run's end does
        found = _tunnelled(store)

    assert [(c.wake, t.burst) for c, t in found] == [(1, 1), (2, 2)]
    assert found[0][1].connection == found[1][1].connection  # the same connection, reused
    assert found[1][1].bytes_sent < found[0][1].bytes_sent  # no handshake the second time
    [use] = outbound_uses(store.calls())
    assert use.calls == 2 and use.connections == 1


async def test_a_burst_still_answering_when_the_wake_ends_belongs_to_the_wake_it_began_in(
    registry: Registry, store: SqliteStore, clock: RunClock, authority: Authority, tmp_path: Path
) -> None:
    """The answer's second chunk is held until the clock has moved on to wake 2: the burst is written only after
    that, and is still wake 1's."""
    routing = Routing(registry, model_hosts=[MODEL_HOST])
    hold = asyncio.Event()
    answer = Answer("text/event-stream", [b"data: one\n\n", b"data: two\n\n"], hold=hold)
    async with (
        model_api(authority, answer) as upstream,
        Proxy(routing, store, clock, confdir=tmp_path / "ca") as proxy,
        client(proxy, authority.ca_cert) as http,
    ):
        clock.begin_wake()
        sim_time = clock.now()
        call = asyncio.create_task(http.post(URL.format(port=upstream.port), content=b"{}"))
        await asyncio.sleep(0.3)
        clock.begin_wake()
        clock.jump(sim_time.replace(hour=sim_time.hour + 1))
        hold.set()
        assert (await call).status_code == 200
        await asyncio.sleep(QUIET)
        [(recorded, tunnel)] = _tunnelled(store)

    assert recorded.wake == 1 and recorded.sim_time == sim_time
    assert tunnel.bytes_received > len(b"data: one\n\ndata: two\n\n")


async def test_a_tunnel_that_closes_says_so_and_a_flush_writes_one_in_progress(
    registry: Registry, store: SqliteStore, clock: RunClock, authority: Authority, tmp_path: Path
) -> None:
    routing = Routing(registry, model_hosts=[MODEL_HOST])
    async with model_api(authority) as upstream, Proxy(routing, store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, authority.ca_cert) as http:
            assert (await http.post(URL.format(port=upstream.port), content=b"{}")).status_code == 200
        # the client is closed before the burst fell quiet: the close ends it
        await asyncio.sleep(0.2)
        [(_, closed)] = _tunnelled(store)
        async with client(proxy, authority.ca_cert) as http:
            assert (await http.post(URL.format(port=upstream.port), content=b"{}")).status_code == 200
            proxy.flush()  # the run ends before the burst fell quiet
            found = _tunnelled(store)
            await asyncio.sleep(QUIET)
            assert len(_tunnelled(store)) == 2  # nothing written twice once it falls quiet

    assert closed.closed is not None and closed.closed >= closed.ended
    assert len(found) == 2 and found[1][1].closed is None and found[1][1].connection != closed.connection
