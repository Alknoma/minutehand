"""What goes in is what comes out: a call Minutehand passes on to a real host arrives there byte for byte as the
agent sent it, and the answer reaches the agent byte for byte as the host gave it. Keeping a copy changes neither.

The bodies here are chosen to show any re-encoding: JSON with spacing and escapes a parser would normalise, and
bytes that are not text at all."""

from __future__ import annotations

from pathlib import Path

import pytest

from minutehand.adapters.proxy.capture import Capturing
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.outbound import PassThrough
from tests.proxy.support import client
from tests.proxy.upstream import Answer as UpstreamAnswer
from tests.proxy.upstream import Authority, model_api

ODD_JSON = b'{ "b" :2,\n\t"a":  "caf\\u00e9 \xc3\xa9",   "n": 1.0e2 ,"z":[ ]}\r\n'
NOT_TEXT = bytes(range(256)) * 3
NOT_ITS_CHARSET = b'{"name": "Ren\xe9e", "bad": "\xff\xfe"}'
"""JSON that says it is UTF-8 and is not: mitmproxy reads it with lone surrogates, which nothing can serialise."""
# What a proxy may rightly change: headers that describe one hop of the connection, not the message.
ONE_HOP = {"connection", "proxy-connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}


def _carried(headers: dict[str, str]) -> dict[str, str]:
    # The local host's own reader leaves an empty name behind for the blank line that ends the head.
    return {name.lower(): value for name, value in headers.items() if name and name.lower() not in ONE_HOP}


@pytest.mark.parametrize(
    ("sent", "content_type"),
    [
        (ODD_JSON, "application/json"),
        (NOT_TEXT, "application/octet-stream"),
        (NOT_ITS_CHARSET, "application/json; charset=utf-8"),
    ],
    ids=["json a parser would tidy", "bytes that are not text", "json invalid in its declared charset"],
)
async def test_a_passed_through_call_and_its_answer_are_the_same_bytes_on_both_sides(
    sent: bytes,
    content_type: str,
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
) -> None:
    answered = [ODD_JSON[:11], NOT_TEXT, ODD_JSON[11:]]
    proxy = Proxy(
        Routing(registry),
        store,
        clock,
        confdir=tmp_path / "ca",
        upstream_ca=authority.ca_cert,
        capturing=Capturing([PassThrough(host="localhost")]),
    )
    async with (
        model_api(authority, UpstreamAnswer("application/octet-stream", answered)) as real,
        proxy,
        client(proxy, proxy.ca_cert) as http,
    ):
        got = await http.post(
            f"https://localhost:{real.port}/v1/lookup?q=a%20b&Empty=&q=again",
            content=sent,
            headers={
                "content-type": content_type,
                "authorization": "Bearer kept-on-the-wire-9c41",
                "x-mixed-case": "VaLuE  with  two spaces",
                "accept-encoding": "identity",
            },
        )

    [received] = real.received
    assert received.body == sent
    assert received.path == "/v1/lookup?q=a%20b&Empty=&q=again"
    assert _carried(real.headers[0]) == _carried(dict(got.request.headers))

    assert got.content == b"".join(answered)
    assert got.headers["content-type"] == "application/octet-stream"
    assert set(_carried(dict(got.headers))) == {"content-type"}

    # The copy kept with the run is the same bytes, both ways, read back from the store: the call is recorded
    # whatever its bodies hold. Before, a body that was not UTF-8 text was kept as nothing at all.
    [kept] = store.calls()
    assert (kept.exchange.request_body or "").encode() == sent or kept.exchange.request_bytes == sent
    assert kept.exchange.response_bytes == b"".join(answered)

    # The copy kept with the run is a copy: the secret went to the host and stayed out of the store.
    assert real.headers[0]["authorization"] == "Bearer kept-on-the-wire-9c41"
    assert b"kept-on-the-wire-9c41" not in b"".join(p.read_bytes() for p in (tmp_path / "world").glob("world.db*"))
