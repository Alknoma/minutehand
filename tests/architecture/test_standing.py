"""The standing mode on the reference agent: two copies of the agent, each with its own keys, driven by a plain
pytest test through `minutehand serve` and the shipped plugin's fixtures, at the same time, each in a world of its
own. Each world answers its agent's email as declared and passes its search and model calls through, and holds
nothing of the other's."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld
from tests.architecture.support import MAIL, SEARCH, free_port, post, settled, started

MODEL = {"host": "model.localhost", "name": "model", "kind": "pass_through"}
NOW = "2026-08-24T09:00:00+00:00"


def _seed() -> Seed:
    return Seed.model_validate(
        {
            "people": [
                {"key": "owen", "name": "Owen Hart", "email": "owen@example.com", "reply": {"kind": "silent"}},
                {"key": "rosa", "name": "Rosa Lind", "email": "rosa@lakeside.example", "reply": {"kind": "silent"}},
            ],
            "starts_at": "2026-08-24T09:00:00Z",
        }
    )


def _spec(tag: str) -> CreateWorld:
    return CreateWorld.model_validate(
        {
            "seed": _seed().model_dump(mode="json"),
            "claims": Claims(tokens=[f"mail-{tag}", f"model-{tag}", f"search-{tag}"]).model_dump(),
            "outbound": [MAIL, SEARCH, MODEL],
        }
    )


@pytest.fixture
def minutehand_spec() -> CreateWorld:
    return _spec("a")


def _drive(env: dict[str, str], port: int, reports: dict[int, dict[str, object]]) -> None:
    with started(env, port):
        post(f"http://127.0.0.1:{port}/wake", {"run_id": "t", "now": NOW, "reason": "start", "goal": "Book a venue."})
        time.sleep(0.2)
        reports[port] = settled(port)


@pytest.mark.timeout(600)
def test_two_copies_of_the_agent_each_land_in_their_own_world_at_once(
    minutehand: MinutehandClient, minutehand_world: OpenWorld, outside: object, tmp_path: Path
) -> None:
    second = minutehand.create_world(_spec("b"))
    try:
        environment = minutehand.environment()
        envs: list[tuple[dict[str, str], int]] = []
        for tag in ("a", "b"):
            port = free_port()
            home = tmp_path / tag
            home.mkdir()
            envs.append(
                (
                    {
                        **{k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")},
                        **environment,
                        "REFERENCE_PORT": str(port),
                        "REFERENCE_HOME": str(home),
                        "REFERENCE_MODEL_URL": outside.model,  # type: ignore[attr-defined]
                        "REFERENCE_SEARCH_URL": outside.search,  # type: ignore[attr-defined]
                        "REFERENCE_MAIL_KEY": f"mail-{tag}",
                        "REFERENCE_MODEL_KEY": f"model-{tag}",
                        "REFERENCE_SEARCH_KEY": f"search-{tag}",
                    },
                    port,
                )
            )
        reports: dict[int, dict[str, object]] = {}
        threads = [threading.Thread(target=_drive, args=(env, port, reports)) for env, port in envs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)

        assert all(r["status"] == "idle" for r in reports.values()), reports
        for world_id in (minutehand_world.world_id, second.world_id):
            calls = minutehand.calls(world_id, captured=True).calls
            hosts = sorted(c.exchange.host for c in calls)
            assert hosts == ["api.mail.example", "api.mail.example", "model.localhost", "search.localhost"], hosts
        assert minutehand.unmatched().calls == []
    finally:
        minutehand.close_world(second.world_id)
