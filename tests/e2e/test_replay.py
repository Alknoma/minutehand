"""`minutehand replay`: a finished run played again from its start, the agent's model calls answered from its
record, so an agent that keeps its state only in its process (`tests/agents/remembers`) rebuilds it exactly; from
`live_from` on, its model answers, and what it says builds on the state the replay rebuilt. The model is a local
HTTPS server that answers otherwise the second time."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from minutehand import session
from minutehand.application.files import load_agent, load_scenario
from minutehand.application.model_export import batch_lines
from minutehand.domain.agent import Reported
from minutehand.domain.world import Actor, MessageSnapshot
from tests.e2e.support import free_port
from tests.proxy.upstream import Answer, Authority, make_authority, model_api

AGENT = Path(__file__).resolve().parents[1] / "agents" / "remembers"
PORT: dict[Path, int] = {}


def _said(text: str) -> Answer:
    body = {"object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": text}}]}
    return Answer("application/json", [json.dumps(body).encode()])


async def _play(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: Authority,
    says: str,
    replay_of: str | None = None,
    live_from: datetime | None = None,
) -> tuple[str, list[str], int]:
    """The run's id, what the agent posted at each wake, and how many calls reached the model. A replay plays the
    agent file its run kept, so every play of a test listens on the test's one port."""
    port = PORT.setdefault(tmp_path, free_port())
    base = f"http://127.0.0.1:{port}"
    agent = load_agent(AGENT / "agent.yaml").model_copy(
        update={"wakes": [Reported(wake_url=f"{base}/wake", report_url=f"{base}/report")]}
    )
    listen = session.Listen(upstream_ca=authority.ca_cert, receive_telemetry=False, model_hosts=["localhost"])
    state, command = tmp_path / "state", [sys.executable, str(AGENT / "agent.py")]
    async with model_api(authority, _said(says)) as model:
        monkeypatch.setenv("PORT", str(port))
        monkeypatch.setenv("MODEL_URL", f"https://localhost:{model.port}/v1/chat/completions")
        if replay_of is None:
            [outcome] = await session.play(
                load_scenario(AGENT / "scenario.yaml"), agent, state=state, command=command, listen=listen
            )
        else:
            outcome = await session.replay(replay_of, state=state, command=command, listen=listen, live_from=live_from)
        reached = len(model.received)
    run_id = outcome.record.run_id
    with session.reading(state, run_id) as kept:
        posted = [
            e.after.text for e in kept.events() if e.actor is Actor.AGENT and isinstance(e.after, MessageSnapshot)
        ]
    return run_id, posted, reached


async def test_a_replay_rebuilds_state_kept_only_in_the_agents_process_then_plays_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = make_authority(tmp_path / "upstream-ca")
    first, posted, reached = await _play(tmp_path, monkeypatch, authority, "then")
    assert posted == ["then", "then | then", "then | then | then"] and reached == 3

    again, replayed, reached = await _play(tmp_path, monkeypatch, authority, "now", replay_of=first)
    assert replayed == posted and reached == 0, "every model call answered from the record"
    assert session.replayed(tmp_path / "state", again) == {
        "replay_of": first,
        "model_calls_replayed": 3,
        "parted": None,
        "live_from": None,
    }

    live_from = datetime.fromisoformat("2026-08-26T00:00:00+00:00")
    _, onward, reached = await _play(tmp_path, monkeypatch, authority, "now", replay_of=first, live_from=live_from)
    assert onward == ["then", "then | then", "then | then | now"] and reached == 1, "live on the state it rebuilt"


async def test_the_model_calls_are_written_out_as_openai_batch_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = make_authority(tmp_path / "upstream-ca")
    first, _, _ = await _play(tmp_path, monkeypatch, authority, "then")

    with session.reading(tmp_path / "state", first) as kept:
        lines = [json.loads(line) for line in batch_lines(kept.spans())]

    assert [line["method"] for line in lines] == ["POST"] * 3
    assert {line["url"] for line in lines} == {"/v1/chat/completions"}
    assert lines[1]["body"]["messages"][0]["content"] == "note 2; so far: ['then']"
    assert lines[0]["response"] == {
        "status_code": 200,
        "body": {"object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "then"}}]},
    }
    assert [line["minutehand"]["at"][:10] for line in lines] == ["2026-08-24", "2026-08-25", "2026-08-26"]
