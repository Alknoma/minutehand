"""Under `minutehand serve`, a model call the proxy tunnels untouched is in the record: in the world that declared
its host, or, when no world declares it, among the unmatched calls.

Before, a tunnelled call was recorded nowhere, not in a world and not in the lobby. The client trusts only the model
API's own CA, so a call the proxy opened rather than tunnelled would fail.
"""

from __future__ import annotations

import json
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.control.wire import LobbyKind, ModelHost
from minutehand.adapters.proxy.addon import BURST_QUIET
from minutehand.domain.world import RecordedCall, TunnelRoute
from minutehand.serve import ServeOptions
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from tests.proxy.upstream import Authority, make_authority, model_api
from tests.serve.support import spec

MODEL_HOST = "model.localhost"


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return make_authority(tmp_path / "upstream-ca")


@contextmanager
def _served(tmp_path: Path, authority: Authority, **options: object) -> Iterator[MinutehandClient]:
    chosen = ServeOptions.model_validate(
        {
            "proxy_port": 0,
            "control_port": 0,
            "telemetry_port": 0,
            "receive_telemetry": False,
            "upstream_ca": authority.ca_cert,
            **options,
        }
    )
    with serve_in_background(tmp_path / "state", chosen) as url, MinutehandClient(url) as client:
        yield client


async def _ask(client: MinutehandClient, port: int, trust: Path) -> None:
    proxy = client.environment()["HTTPS_PROXY"]
    context = ssl.create_default_context(cafile=str(trust))
    async with httpx.AsyncClient(proxy=proxy, verify=context, trust_env=False) as http:
        answered = await http.post(f"https://{MODEL_HOST}:{port}/v1/chat/completions", json={"model": "m"})
    assert answered.status_code == 200 and json.loads(answered.content) == {"received": 1}


def _eventually(read: Callable[[], list[RecordedCall]]) -> list[RecordedCall]:
    """The calls `read` answers once a burst has fallen quiet and been written."""
    give_up = time.monotonic() + 10 * BURST_QUIET + 5
    while True:
        found = read()
        if found or time.monotonic() > give_up:
            return found
        time.sleep(0.1)


async def test_a_model_host_no_world_declares_is_kept_in_the_lobby_and_is_not_unclaimed(
    tmp_path: Path, authority: Authority
) -> None:
    """The burst is kept (the record stays complete) under `model_host`, counted, and left out of the default view
    a suite asserts empty."""
    with _served(tmp_path, authority, model_hosts=[MODEL_HOST]) as client:
        world = client.create_world(spec("tunnel-key-one"))
        async with model_api(authority) as upstream:
            await _ask(client, upstream.port, authority.ca_cert)
        [call] = _eventually(lambda: client.unmatched(kinds=[LobbyKind.MODEL_HOST]).calls)
        assert client.calls(world.world_id).calls == []
        lobby = client.unmatched()
        assert lobby.calls == [] and lobby.kinds[LobbyKind.MODEL_HOST] >= 1
        assert client.unmatched(kinds=()).calls == []  # the server's own default, no kind asked
        client.assert_nothing_unclaimed()

    tunnel = call.exchange.tunnelled
    assert tunnel is not None and tunnel.route is TunnelRoute.NONE and tunnel.port == upstream.port
    assert tunnel.bytes_sent > 0 and tunnel.bytes_received > 0 and not call.refused


async def test_a_model_host_a_world_declares_is_recorded_in_that_world(tmp_path: Path, authority: Authority) -> None:
    with _served(tmp_path, authority) as client:
        declaring = spec("tunnel-key-two").model_copy(update={"model_hosts": [ModelHost(host=MODEL_HOST)]})
        world = client.create_world(declaring)
        other = client.create_world(spec("tunnel-key-three"))
        async with model_api(authority) as upstream:
            await _ask(client, upstream.port, authority.ca_cert)
        [call] = _eventually(lambda: client.calls(world.world_id, tunnelled=True).calls)
        assert client.calls(world.world_id, unmatched=True).calls == []
        assert client.calls(other.world_id).calls == [] and client.unmatched().calls == []

    tunnel = call.exchange.tunnelled
    assert tunnel is not None and tunnel.route is TunnelRoute.HOST and call.exchange.host == MODEL_HOST
