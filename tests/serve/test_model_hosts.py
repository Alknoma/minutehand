"""Model APIs in the standing mode: a self-hosted one given to `minutehand serve` with `--model-host`, and one a
world declares, each treated as a model host (tunnelled, or opened and kept as a span) and never refused.

The model API is a local HTTPS server with its own CA (`tests/proxy/upstream.py`), at `localhost`. Each test runs
its own server, since the model hosts are the server's.
"""

from __future__ import annotations

import json
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.control.wire import ModelHost
from minutehand.domain.telemetry import SpanSource, StringValue
from minutehand.serve import ServeOptions
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient, Refused
from tests.proxy.upstream import Answer, Authority, make_authority, model_api
from tests.serve.support import spec

MODEL_HOST = "localhost"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
ANSWER = {
    "object": "chat.completion",
    "model": "model-luna-0801",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Asked."}, "finish_reason": "stop"}],
}
ASKED = {"model": "model-luna", "messages": [{"role": "user", "content": "Ask Sofia."}]}


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


async def _ask(client: MinutehandClient, port: int, trust: Path) -> httpx.Response:
    """A service's model call through the server's proxy, trusting only `trust`."""
    proxy = client.environment()["HTTPS_PROXY"]
    context = ssl.create_default_context(cafile=str(trust))
    async with httpx.AsyncClient(proxy=proxy, verify=context, trust_env=False) as http:
        return await http.post(
            f"https://{MODEL_HOST}:{port}/v1/chat/completions",
            json=ASKED,
            headers={"traceparent": f"00-{TRACE}-00f067aa0ba902b7-01"},
        )


async def test_a_model_host_given_to_serve_is_tunnelled_not_refused(tmp_path: Path, authority: Authority) -> None:
    """The client trusts ONLY the model API's own CA: a call refused, or opened by the proxy, would fail."""
    with _served(tmp_path, authority, model_hosts=[MODEL_HOST]) as client:
        world = client.create_world(spec("model-key-one"))
        async with model_api(authority, Answer("application/json", [json.dumps(ANSWER).encode()])) as upstream:
            answered = await _ask(client, upstream.port, authority.ca_cert)
        assert answered.status_code == 200 and answered.json() == ANSWER
        assert len(upstream.received) == 1
        assert client.calls(world.world_id).calls == [] and client.spans(world.world_id).spans == []


async def test_a_model_host_given_to_serve_with_record_model_calls_is_opened_and_sent_on(
    tmp_path: Path, authority: Authority
) -> None:
    """The client trusts ONLY the server's CA: the call reaches the model API only if the proxy opened it."""
    with _served(tmp_path, authority, model_hosts=[MODEL_HOST], record_model_calls=True) as client:
        bundle = Path(client.environment()["SSL_CERT_FILE"])
        async with model_api(authority, Answer("application/json", [json.dumps(ANSWER).encode()])) as upstream:
            answered = await _ask(client, upstream.port, bundle)
        assert answered.status_code == 200 and answered.json() == ANSWER
        assert json.loads(upstream.received[0].body) == ASKED


async def test_a_model_host_a_world_declares_recorded_is_kept_as_a_span_in_that_world(
    tmp_path: Path, authority: Authority
) -> None:
    with _served(tmp_path, authority) as client:
        bundle = Path(client.environment()["SSL_CERT_FILE"])
        declaring = spec("model-key-two").model_copy(update={"model_hosts": [ModelHost(host=MODEL_HOST, record=True)]})
        world = client.create_world(declaring)
        other = client.create_world(spec("model-key-three"))
        async with model_api(authority, Answer("application/json", [json.dumps(ANSWER).encode()])) as upstream:
            answered = await _ask(client, upstream.port, bundle)
        assert answered.status_code == 200 and answered.json() == ANSWER
        [kept] = client.spans(world.world_id).spans
        assert kept.source is SpanSource.WIRE and kept.span.trace_id == TRACE
        assert kept.span.attribute("gen_ai.request.model") == StringValue(value="model-luna")
        assert client.calls(world.world_id).calls == [] and client.spans(other.world_id).spans == []


async def test_a_model_host_a_world_declares_unrecorded_is_tunnelled_and_released_when_it_closes(
    tmp_path: Path, authority: Authority
) -> None:
    with _served(tmp_path, authority) as client:
        declaring = spec("model-key-four").model_copy(update={"model_hosts": [ModelHost(host=MODEL_HOST)]})
        world = client.create_world(declaring)
        async with model_api(authority, Answer("application/json", [json.dumps(ANSWER).encode()])) as upstream:
            assert (await _ask(client, upstream.port, authority.ca_cert)).status_code == 200
            client.close_world(world.world_id)
            # No longer a model host: refused by the proxy, which the client (trusting only the model API's CA)
            # sees as a certificate it does not trust.
            with pytest.raises(httpx.ConnectError):
                await _ask(client, upstream.port, authority.ca_cert)
        assert len(upstream.received) == 1


def test_a_second_world_declaring_a_model_host_another_holds_is_refused(tmp_path: Path, authority: Authority) -> None:
    with _served(tmp_path, authority) as client:
        client.create_world(spec("model-key-five").model_copy(update={"model_hosts": [ModelHost(host=MODEL_HOST)]}))
        second = spec("model-key-six").model_copy(update={"model_hosts": [ModelHost(host=MODEL_HOST, record=True)]})
        with pytest.raises(Refused, match="model host"):
            client.create_world(second)
        assert len(client.worlds()) == 1


def test_a_world_declaring_a_provider_host_as_a_model_host_is_refused(tmp_path: Path, authority: Authority) -> None:
    with _served(tmp_path, authority) as client:
        claimed = spec("model-key-seven").model_copy(update={"model_hosts": [ModelHost(host="slack.com")]})
        with pytest.raises(Refused, match="slack"):
            client.create_world(claimed)
        assert client.worlds() == []
