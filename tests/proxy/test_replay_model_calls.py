"""`minutehand replay`'s model calls (`adapters/proxy/replay.py`): each of the agent's calls answered with what the
model answered in the recorded run, in the order alike calls were made, the model not called; from the first call
the recording never saw, and from `live_from`, every call goes to the model. The model API is a local HTTPS server
(`upstream.py`) that would now answer otherwise."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.replay import ModelReplay
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.run_clock import RunClock
from minutehand.domain.telemetry import StringValue
from tests.proxy.support import client
from tests.proxy.upstream import Answer, Authority, make_authority, model_api

HOST = "localhost"
PATH = "/v1/chat/completions"


def _asked(text: str) -> bytes:
    return json.dumps({"model": "model-luna", "messages": [{"role": "user", "content": text}]}).encode()


def _said(text: str) -> bytes:
    return json.dumps(
        {"object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": text}}]}
    ).encode()


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return make_authority(tmp_path / "upstream-ca")


async def _calls(
    registry: Registry,
    store: SqliteStore,
    clock: RunClock,
    tmp_path: Path,
    authority: Authority,
    said: str,
    asked: list[str],
    replay: ModelReplay | None = None,
) -> tuple[list[str], int]:
    """What the agent was answered for each question, and how many reached the model."""
    routing = Routing(registry, model_hosts=[HOST])
    answer = Answer("application/json", [_said(said)])
    async with (
        model_api(authority, answer) as upstream,
        Proxy(
            routing, store, clock, confdir=tmp_path / "ca", upstream_ca=authority.ca_cert, record_model_calls=True
        ) as proxy,
    ):
        proxy.addon.model_replay = replay
        got = []
        async with client(proxy, proxy.ca_cert) as http:
            for text in asked:
                response = await http.post(
                    f"https://{HOST}:{upstream.port}{PATH}",
                    content=_asked(text),
                    headers={"content-type": "application/json"},
                )
                got.append(response.json()["choices"][0]["message"]["content"])
        return got, len(upstream.received)


async def test_a_replay_answers_as_the_recording_did_until_the_agent_asks_something_new(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    await _calls(registry, store, clock, tmp_path, authority, "then", ["plan", "plan"])
    replay = ModelReplay.of("run1", store.spans())
    before = len(store.spans())

    got, reached = await _calls(
        registry, store, clock, tmp_path, authority, "now", ["plan", "plan", "new", "plan"], replay
    )

    assert got == ["then", "then", "now", "now"], "after the first call it never made, the model answers"
    assert reached == 2 and replay.answered == 2 and replay.parted is not None and "never made" in replay.parted
    marked = [s.span.attribute("minutehand.replayed_from") for s in store.spans()[before:]]
    assert marked == [StringValue(value="run1")] * 2 + [None] * 2


async def test_from_live_from_on_the_model_answers(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    await _calls(registry, store, clock, tmp_path, authority, "then", ["plan"])
    replay = ModelReplay.of("run1", store.spans(), live_from=clock.now() - timedelta(seconds=1))

    got, reached = await _calls(registry, store, clock, tmp_path, authority, "now", ["plan"], replay)

    assert (got, reached, replay.answered) == (["now"], 1, 0)
