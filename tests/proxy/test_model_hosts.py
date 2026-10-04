"""Model APIs: tunnelled untouched, or opened and edited when the run changes the prompt or model.

The model API is a local HTTPS server with its own CA (`upstream.py`); the run lists
`localhost` as its model host. Nothing here reaches the public internet.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from minutehand.adapters.proxy.edit import apply_edits
from minutehand.adapters.proxy.policy import HostPolicy, ModelEdit, Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.experiment import CallMatch, ModelSwap, Override, PromptPatch
from tests.proxy.support import client, exchanges, stored_bytes
from tests.proxy.upstream import Authority, Upstream, make_authority, model_api

MODEL_HOST = "localhost"

EDITS: list[Override] = [
    PromptPatch(where=CallMatch(system_contains="CHAT"), text=" Confirm every name."),
    PromptPatch(where=CallMatch(system_contains="RESPONSES"), find="this week", text="this fortnight"),
    PromptPatch(where=CallMatch(system_contains="ANTHROPIC"), text=" Ask before filing."),
    ModelSwap(where=CallMatch(model="model-luna"), to="model-terra"),
    PromptPatch(where=CallMatch(host="api.openai.com"), text=" NEVER APPLIED"),
]


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return make_authority(tmp_path / "upstream-ca")


async def _send(proxy: Proxy, upstream: Upstream, body: bytes) -> bytes:
    async with client(proxy, proxy.ca_cert) as http:
        response = await http.post(
            f"https://{MODEL_HOST}:{upstream.port}/v1/call",
            content=body,
            headers={"content-type": "application/json", "authorization": "Bearer sk-model"},
        )
    assert response.status_code == 200, response.text
    return upstream.received[-1].body


async def _through_edits(
    body: object,
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
    edits: Sequence[Override] = EDITS,
) -> tuple[bytes, bytes]:
    raw = json.dumps(body).encode()
    routing = Routing(registry, model_hosts=[MODEL_HOST], overrides=edits)
    assert routing.policy(MODEL_HOST) is HostPolicy.EDIT
    async with model_api(authority) as upstream:
        async with Proxy(routing, store, clock, confdir=tmp_path / "ca", upstream_ca=authority.ca_cert) as proxy:
            arrived = await _send(proxy, upstream, raw)
    return raw, arrived


async def test_chat_completions_system_message_is_patched_and_model_swapped(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority, world_path: Path
) -> None:
    body = {
        "model": "model-luna",
        "messages": [{"role": "system", "content": "CHAT agent."}, {"role": "user", "content": "CHAT is not here"}],
        "temperature": 0.2,
        "tools": [{"type": "function", "function": {"name": "file", "parameters": {}}}],
    }
    _, arrived = await _through_edits(body, registry, store, clock, tmp_path, authority)
    expected = json.loads(json.dumps(body))
    expected["model"] = "model-terra"
    expected["messages"][0]["content"] = "CHAT agent. Confirm every name."
    assert json.loads(arrived) == expected
    assert exchanges(world_path) == [] and store.head() == 0
    assert b"CHAT agent" not in stored_bytes(world_path)


async def test_chat_completions_developer_message_with_text_parts_is_patched(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    body = {
        "model": "model-sol",
        "messages": [
            {"role": "developer", "content": [{"type": "text", "text": "CHAT one."}, {"type": "text", "text": "Two."}]},
            {"role": "user", "content": "hi"},
        ],
    }
    _, arrived = await _through_edits(body, registry, store, clock, tmp_path, authority)
    expected = json.loads(json.dumps(body))
    expected["messages"][0]["content"][1]["text"] = "Two. Confirm every name."
    assert json.loads(arrived) == expected


async def test_responses_instructions_are_patched_by_find_and_replace(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    body = {
        "model": "model-sol",
        "instructions": "RESPONSES: plan this week.",
        "input": "plan this week",
        "store": False,
    }
    _, arrived = await _through_edits(body, registry, store, clock, tmp_path, authority)
    assert json.loads(arrived) == {**body, "instructions": "RESPONSES: plan this fortnight."}


@pytest.mark.parametrize(
    "system",
    [
        "ANTHROPIC agent.",
        [{"type": "text", "text": "ANTHROPIC agent.", "cache_control": {"type": "ephemeral"}}],
    ],
)
async def test_anthropic_system_is_patched_and_model_swapped(
    system: object, registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    body = {
        "model": "model-luna",
        "max_tokens": 64,
        "system": system,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
    }
    _, arrived = await _through_edits(body, registry, store, clock, tmp_path, authority)
    expected = json.loads(json.dumps(body))
    expected["model"] = "model-terra"
    if isinstance(system, str):
        expected["system"] = "ANTHROPIC agent. Ask before filing."
    else:
        expected["system"][0]["text"] = "ANTHROPIC agent. Ask before filing."
    assert json.loads(arrived) == expected


async def test_a_call_no_edit_matches_goes_on_byte_for_byte(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    body = {
        "model": "model-sol",
        "messages": [{"role": "system", "content": "Some other prompt, é."}],
        "temperature": 1.0,
    }
    raw, arrived = await _through_edits(body, registry, store, clock, tmp_path, authority)
    assert arrived == raw


def test_an_edit_matches_on_the_model_the_agent_sent_not_the_swapped_one() -> None:
    edits: list[ModelEdit] = [ModelSwap(to="model-b"), PromptPatch(where=CallMatch(model="model-a"), text="!")]
    out = apply_edits(json.dumps({"model": "model-a", "instructions": "go"}).encode(), "api.openai.com", edits)
    assert out is not None and json.loads(out) == {"model": "model-b", "instructions": "go!"}


async def test_model_host_is_tunnelled_without_being_decrypted(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority, world_path: Path
) -> None:
    routing = Routing(registry, model_hosts=[MODEL_HOST])
    assert routing.policy(MODEL_HOST) is HostPolicy.TUNNEL
    body = b'{"model": "model-luna", "messages": [{"role": "system", "content": "CHAT agent."}]}'
    async with model_api(authority) as upstream, Proxy(routing, store, clock, confdir=tmp_path / "ca") as proxy:
        # The client trusts ONLY the model API's own CA: if the proxy had decrypted
        # the call, it would have presented its own certificate and this would fail.
        async with client(proxy, authority.ca_cert) as http:
            response = await http.post(f"https://{MODEL_HOST}:{upstream.port}/v1/call", content=body)
        async with client(proxy, proxy.ca_cert) as http:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await http.post(f"https://{MODEL_HOST}:{upstream.port}/v1/call", content=body)
    assert response.status_code == 200
    assert upstream.received[0].body == body
    assert exchanges(world_path) == [] and store.head() == 0


async def test_an_edited_call_verifies_the_model_api_certificate(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    """Without the upstream CA the proxy refuses to send the edited call on: it never skips verification."""
    routing = Routing(registry, model_hosts=[MODEL_HOST], overrides=EDITS)
    async with model_api(authority) as upstream, Proxy(routing, store, clock, confdir=tmp_path / "ca") as proxy:
        async with client(proxy, proxy.ca_cert) as http:
            response = await http.post(f"https://{MODEL_HOST}:{upstream.port}/v1/call", content=b"{}")
    assert response.status_code == 502
    assert upstream.received == []


async def test_edits_applied_for_a_run_reach_the_model_api_and_clearing_them_tunnels_again(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    routing = Routing(registry, model_hosts=[MODEL_HOST])
    assert routing.policy(MODEL_HOST) is HostPolicy.TUNNEL
    routing.apply("child", [ModelSwap(to="model-terra")])
    assert routing.policy(MODEL_HOST) is HostPolicy.EDIT
    async with model_api(authority) as upstream:
        async with Proxy(routing, store, clock, confdir=tmp_path / "ca", upstream_ca=authority.ca_cert) as proxy:
            arrived = await _send(proxy, upstream, json.dumps({"model": "model-luna", "messages": []}).encode())
    assert json.loads(arrived)["model"] == "model-terra"
    routing.apply("child", [])
    assert routing.policy(MODEL_HOST) is HostPolicy.TUNNEL
