"""The record is complete and unchanged.

Through `minutehand serve`, a client of the test's own configured the way the server says (its proxy, its CA):
every call it makes is kept once, in order, its bodies byte for byte; no secret it carried reaches a stored file; a
forwarded or passed-through call reaches its upstream as sent, apart from the headers the documentation names; a
model call on a tunnel is kept as a connection and never read; and only a call nobody claimed is unclaimed.
"""

from __future__ import annotations

import base64
import ssl
import time
from pathlib import Path

import httpx

from minutehand.adapters.control.wire import CreateWorld, LobbyKind
from minutehand.domain.world import RecordedCall
from minutehand.testing.client import MinutehandClient
from tests.acceptance.support import (
    EMULATOR,
    OWEN,
    PYTHON,
    TOLD_ONLY,
    authority,
    echo_server,
    installed_ledger_fake,
    person,
    served,
    stored_bytes,
    through_proxy,
)

SEED = {"people": [person("owen", OWEN, TOLD_ONLY)]}
FORWARDED_ADDED = {"x-minutehand-world", "x-minutehand-wake", "x-minutehand-time", "traceparent"}
"""What `docs/capture.md` and `docs/external-emulators.md` say a forwarded copy carries that the call did not."""


def world(**spec: object) -> CreateWorld:
    return CreateWorld.model_validate({"seed": SEED, **spec})


def kept_request(call: RecordedCall) -> bytes:
    exchange = call.exchange
    return exchange.request_body.encode() if exchange.request_body is not None else exchange.request_bytes or b""


def kept_response(call: RecordedCall) -> bytes:
    exchange = call.exchange
    return exchange.response_body.encode() if exchange.response_body is not None else exchange.response_bytes or b""


def echoed(answered: httpx.Response) -> tuple[str, dict[str, str], bytes]:
    """What the echo upstream received: the path, its headers by lowercase name, and the body."""
    said = answered.json()
    return said["path"], {k.lower(): v for k, v in said["headers"]}, base64.b64decode(said["body"])


def test_every_call_is_kept_once_in_order_with_text_binary_and_invalid_charset_bodies_byte_identical(
    tmp_path: Path,
) -> None:
    bodies = [
        ("application/json; charset=utf-8", '{"note": "café ✓ — naïve", "n": 1}'.encode()),
        ("application/octet-stream", bytes(range(256)) * 3),
        ("text/plain; charset=utf-8", b"caf\xe9 is not UTF-8 \xff\xfe and neither is this: \xc3"),
        ("text/plain; charset=utf-8", "x" * 700 + " stored apart once it passes 512 bytes"),
    ]
    with served(tmp_path, env=installed_ledger_fake(tmp_path)) as server:
        opened = server.client.create_world(world(claims={"default": True}))
        with through_proxy(server, tmp_path / "ca.pem") as http:
            for content_type, body in bodies:
                sent = body if isinstance(body, bytes) else body.encode()
                answered = http.post(
                    "https://ledger.example/echo", content=sent, headers={"content-type": content_type}
                )
                assert answered.content == sent, "the fake answers what it was sent"
            assert http.get("https://ledger.example/entries").status_code == 200
        calls = server.client.calls(opened.world_id).calls

    assert [(c.exchange.method, c.exchange.host, c.exchange.path) for c in calls] == [
        *[("POST", "ledger.example", "/echo")] * len(bodies),
        ("GET", "ledger.example", "/entries"),
    ], "every call once, in the order made, and nothing else"
    for call, (_, body) in zip(calls, bodies, strict=False):
        sent = body if isinstance(body, bytes) else body.encode()
        assert kept_request(call) == sent
        assert kept_response(call) == sent


def test_no_secret_a_call_carried_reaches_any_stored_file(tmp_path: Path) -> None:
    """Credentials in a header, a cookie, the query, a form body and a JSON body, to a fake and to a captured host;
    every file under the state directory is searched, each compressed body decompressed."""
    secrets = {
        "header": "xoxb-secret-header-7q2k",
        "cookie": "secret-cookie-7q2k",
        "form": "xoxb-secret-form-7q2k",
        "query": "secret-query-key-7q2k",
        "json": "secret-json-password-7q2k",
    }
    mail = {"host": "api.mail.example", "kind": "acknowledge", "answer": {"status": 202, "json_body": {}}}
    with served(tmp_path) as server:
        opened = server.client.create_world(world(claims={"default": True}, outbound=[mail]))
        with through_proxy(server, tmp_path / "ca.pem") as http:
            headers = {"Authorization": f"Bearer {secrets['header']}", "Cookie": f"d={secrets['cookie']}"}
            assert http.post("https://slack.com/api/auth.test", headers=headers).json()["ok"] is True
            assert http.post("https://slack.com/api/auth.test", data={"token": secrets["form"]}).json()["ok"] is True
            sent = http.post(
                f"https://api.mail.example/v3/send?api_key={secrets['query']}",
                json={"to": OWEN, "text": "hello", "password": secrets["json"]},
            )
            assert sent.status_code == 202
        assert len(server.client.calls(opened.world_id).calls) == 3
        server.client.close_world(opened.world_id)
        kept = stored_bytes(server.state)

    assert kept, "the state directory holds the world"
    assert b"hello" in kept, "the search reads the bodies it is meant to"
    leaked = [where for where, secret in secrets.items() if secret.encode() in kept]
    assert leaked == []


def test_a_forwarded_call_reaches_its_emulator_unchanged_but_for_the_documented_headers(tmp_path: Path) -> None:
    emulator = {
        "name": "payments",
        "upstream": {"url": "http://127.0.0.1:{port}"},
        "command": [PYTHON, str(EMULATOR), "{port}"],
        "ready": {"kind": "http", "path": "/health", "status": 200},
        "ready_within": "PT20S",
    }
    forward = {"host": "api.payments.example", "kind": "forward", "emulator": "payments"}
    body = bytes(range(256))
    with served(tmp_path) as server:
        opened = server.client.create_world(
            world(claims={"tokens": ["pay-token-31"]}, outbound=[forward], emulators=[emulator])
        )
        with through_proxy(server, tmp_path / "ca.pem") as http:
            request = http.build_request(
                "POST",
                "https://api.payments.example/v1/charges?amount=2000&currency=eur",
                content=body,
                headers={"Authorization": "Bearer pay-token-31", "X-Idempotency": "one", "Content-Type": "x/y"},
            )
            sent = {k.lower(): v for k, v in request.headers.items()}
            path, received, received_body = echoed(http.send(request))

    assert path == "/v1/charges?amount=2000&currency=eur"
    assert received_body == body
    assert received["x-minutehand-world"] == opened.world_id
    assert {k: v for k, v in received.items() if k not in FORWARDED_ADDED} == {
        k: v for k, v in sent.items() if k not in FORWARDED_ADDED
    }


def test_a_passed_through_call_reaches_its_upstream_unchanged(tmp_path: Path) -> None:
    upstream = authority(tmp_path / "upstream", "echo.localhost")
    body = "a lookup, ünïcode and all".encode()
    with echo_server(tmp_path, tls=upstream) as port, served(tmp_path, "--upstream-ca", str(upstream.ca)) as server:
        lookup = {"host": "echo.localhost", "kind": "pass_through"}
        opened = server.client.create_world(world(claims={"default": True}, outbound=[lookup]))
        with through_proxy(server, tmp_path / "ca.pem") as http:
            request = http.build_request(
                "PUT", f"https://echo.localhost:{port}/search?q=hall", content=body, headers={"X-Asked-By": "test"}
            )
            sent = {k.lower(): v for k, v in request.headers.items()}
            path, received, received_body = echoed(http.send(request))
        captured = server.client.calls(opened.world_id, captured=True).calls

    assert (path, received_body) == ("/search?q=hall", body)
    assert received == sent
    assert len(captured) == 1 and kept_request(captured[0]) == body


def _model_call(server_port: int, proxy_port: int, ca: Path, host: str) -> None:
    """A model API call on a tunnel: the client trusts the model API's own CA, since a tunnel is never opened."""
    trusted = ssl.create_default_context(cafile=str(ca))
    with httpx.Client(proxy=f"http://127.0.0.1:{proxy_port}", verify=trusted, trust_env=False, timeout=30) as http:
        answered = http.post(f"https://{host}:{server_port}/v1/chat/completions", json={"model": "m", "messages": []})
        assert answered.status_code == 200


def _tunnelled(server: MinutehandClient, world_id: str) -> list[RecordedCall]:
    deadline = time.monotonic() + 15
    while True:  # a burst is written once its connection has been quiet for half a second
        found = server.calls(world_id, tunnelled=True).calls
        if found or time.monotonic() > deadline:
            return found
        time.sleep(0.1)


def test_a_tunnelled_model_call_is_kept_as_a_connection_and_never_read(tmp_path: Path) -> None:
    model = authority(tmp_path / "model", "model.localhost")
    with echo_server(tmp_path, tls=model) as port, served(tmp_path) as server:
        opened = server.client.create_world(
            world(claims={"tokens": ["unused-31"]}, model_hosts=[{"host": "model.localhost"}])
        )
        _model_call(port, server.proxy_port, model.ca, "model.localhost")
        tunnelled = _tunnelled(server.client, opened.world_id)

    assert len(tunnelled) == 1, tunnelled
    exchange = tunnelled[0].exchange
    assert exchange.host == "model.localhost" and exchange.tunnelled is not None
    assert exchange.tunnelled.bytes_sent > 0 and exchange.tunnelled.bytes_received > 0
    assert (exchange.request_body, exchange.response_body, exchange.request_bytes, exchange.response_bytes) == (
        None,
        None,
        None,
        None,
    ), "what was said on a tunnel is never kept"


def test_only_a_call_to_an_undeclared_host_is_unclaimed_and_model_traffic_is_not(tmp_path: Path) -> None:
    """The world's own undeclared call is refused 502 and listed unmatched; a model call to a server-wide model host
    no world declared is kept in the lobby and is not unclaimed."""
    model = authority(tmp_path / "model", "llm.localhost")
    with (
        echo_server(tmp_path, tls=model) as port,
        served(tmp_path, "--model-host", "llm.localhost") as server,
    ):
        opened = server.client.create_world(world(claims={"tokens": ["tok-unclaimed-31"]}))
        with through_proxy(server, tmp_path / "ca.pem") as http:
            refused = http.get("https://undeclared.example/v1/x", headers={"Authorization": "Bearer tok-unclaimed-31"})
        assert refused.status_code == 502
        _model_call(port, server.proxy_port, model.ca, "llm.localhost")

        unmatched = server.client.calls(opened.world_id, unmatched=True).calls
        assert [(c.exchange.host, c.exchange.path, c.exchange.status) for c in unmatched] == [
            ("undeclared.example", "/v1/x", 502)
        ]
        deadline = time.monotonic() + 15
        lobby = server.client.unmatched(kinds=())
        while not any(lobby.kinds.values()) and time.monotonic() < deadline:
            time.sleep(0.1)
            lobby = server.client.unmatched(kinds=())
        server.client.assert_nothing_unclaimed()
        assert {kind: n for kind, n in lobby.kinds.items() if n} == {LobbyKind.MODEL_HOST: 1}
