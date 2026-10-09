"""A standing world's people act through the people engine as a run's do (docs/design-transitions.md): what waits on
them is listed with when they act on `GET /v1/worlds/{id}/transitions`, and moving the clock past that moment has
them act, the move listed beside every other move of any item's state."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed
from minutehand.domain.world import Actor, EntityKind, PendingStatus
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld

START = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[MinutehandClient]:
    with serve_in_background(tmp_path_factory.mktemp("serve")) as url, MinutehandClient(url) as found:
        yield found


def _seed() -> Seed:
    return Seed.model_validate(
        {
            "starts_at": START.isoformat(),
            "transitions_on": ["jira"],
            "people": [
                {
                    "key": "owen",
                    "name": "Owen Owner",
                    "email": "owen@example.com",
                    "reply": {"kind": "scripted", "then": "silent"},
                    "takes": [{"provider": "jira", "take": "Done", "after": "PT1H", "verbatim": "Shipped."}],
                }
            ],
            "tickets": [{"provider": "jira", "project": "Ops", "title": "Ship it", "assignee": "owen"}],
            "provider_seeds": [{"provider": "jira", "body": {"site": "routeworks", "agent_email": "a@routeworks.example",
                                                            "credentials": [{"account": "agent", "api_token": "t-route"}]}}],
        }
    )  # fmt: skip


def test_the_route_lists_what_waits_on_people_and_the_moves_after_the_clock_passes_them(
    client: MinutehandClient,
) -> None:
    world = OpenWorld(
        client,
        client.create_world(CreateWorld(seed=_seed(), claims=Claims(tokens=["t-route"]), scripted_people=True)),
    )
    try:
        before = world.transitions()
        [held] = before.items
        assert (held.person, held.state, held.status, held.take) == ("owen", "To Do", PendingStatus.PENDING, "Done")
        assert held.item.kind is EntityKind.TICKET and held.due_at == START + timedelta(hours=1)
        assert before.transitions == []

        advanced = world.advance(timedelta(hours=2))
        assert [f.what for f in advanced.fired] == [f"owen takes 'Done' on jira {held.item.external_id}"]

        after = world.transitions()
        [moved] = after.transitions
        assert (moved.name, moved.from_state, moved.to_state, moved.by, moved.who) == (
            "Done", "To Do", "Done", Actor.PERSON, "owen"
        )  # fmt: skip
        assert moved.at == START + timedelta(hours=1)
        assert [(i.status, i.transition) for i in after.items] == [(PendingStatus.ACTED, moved.seq)]
    finally:
        world.close()
