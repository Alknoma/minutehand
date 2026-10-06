"""The viewer's list of what was said holds each item left waiting on a person in the agent's own product, and the
decision on it, in words."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import MessageChange, MessagesResponse
from minutehand.domain.scenario import ScriptedDecision
from tests.inboxes.product import Product, serving
from tests.inboxes.support import NADIA, TOKENS, deciding, inbox, play, scenario
from tests.inboxes.test_gated import agent


@pytest.fixture
def product() -> Iterator[Product]:
    with serving(Product(tokens=dict(TOKENS))) as served:
        yield served


async def test_the_ask_and_the_decision_are_said_in_words(
    tmp_path: Path, product: Product, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NADIA_TOKEN", TOKENS[NADIA])
    state = tmp_path / "state"
    run = state / "runs" / "root"
    run.mkdir(parents=True)
    scn = scenario(deciding(ScriptedDecision(decision="approve")))
    played = await play(run, scn, inbox(product), agent(product))
    played.store.close()
    (run / "scenario.json").write_text(scn.model_dump_json())
    (run / "record.json").write_text(played.record.model_dump_json())

    with TestClient(create_app(state)) as viewer:
        said = MessagesResponse.model_validate_json(viewer.get("/api/runs/root/messages").content).messages

    items = [(m.change, m.words) for m in said if m.words is not None]
    assert items == [
        (MessageChange.ASKED, "asked Nadia Ek to approve or reject: Send Owen the booking"),
        (MessageChange.DECIDED, "Nadia Ek approved: Send Owen the booking"),
    ]
