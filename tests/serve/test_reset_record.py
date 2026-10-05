"""A reset returns a standing world's state to its seed and never discards its record: the calls, events and spans
from before it are kept, read since the last reset by default, and across every reset when asked."""

from __future__ import annotations

import httpx
import pytest

from minutehand.adapters.providers.slack.state import GENERAL, named_channel_id
from minutehand.domain.world import Actor, Operation
from minutehand.testing.client import Refused
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, answer, spec


def test_calls_made_before_a_reset_can_still_be_counted_after_it(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-record")))
    try:
        slack = served.slack("xoxb-record")
        slack.auth_test()
        posted = answer(slack.chat_postMessage(channel=named_channel_id(GENERAL), text="before the reset"))
        assert posted["ok"]
        assert len(world.calls()) == 2
        view = world.reset()
        assert view.resets == 1
        slack.auth_test()

        assert len(world.calls()) == 1, "since the last reset, as a caller who never asks sees it"
        page = served.client.calls(world.world_id, since_reset=False)
        assert [c.exchange.path.split("?")[0] for c in page.calls] == [
            "/api/auth.test",
            "/api/chat.postMessage",
            "/api/auth.test",
        ]
        assert page.resets == [2]
        assert len(world.calls(since_reset=False)) == 3

        sent = [e for e in world.events(actor=Actor.AGENT, since_reset=False) if e.operation is Operation.CREATE]
        assert len(sent) == 1 and not [e for e in world.events(actor=Actor.AGENT) if e.operation is Operation.CREATE]
        events = served.client.events(world.world_id, since_reset=False)
        assert len(events.resets) == 1 and 0 < events.resets[0] < len(events.events)

        world.reset()
        twice = served.client.calls(world.world_id, since_reset=False)
        assert twice.resets == [2, 3] and len(twice.calls) == 3
        assert served.client.spans(world.world_id, since_reset=False).resets == [0, 0]
    finally:
        served.client.close_world(world.world_id)


def test_a_since_reset_that_is_not_true_or_false_is_refused(served: Served) -> None:
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-record-refused")))
    try:
        answered = httpx.get(f"{served.url}/v1/worlds/{world.world_id}/calls?since_reset=yes", timeout=10)
        assert answered.status_code == 422 and "since_reset" in answered.text
        with pytest.raises(Refused):
            served.client.calls("no-such-world", since_reset=False)
    finally:
        served.client.close_world(world.world_id)


def test_a_query_parameter_that_is_not_what_its_route_takes_is_refused_422_not_409(served: Served) -> None:
    """docs/serve.md: 409 is what a world cannot do, 422 a request that is not the model; a `since` of `abc` or a
    `kind` that names no kind was answered 409, as if the world had refused."""
    world = OpenWorld(served.client, served.client.create_world(spec("xoxb-bad-query")))
    try:
        base = f"{served.url}/v1/worlds/{world.world_id}"
        for path in ("/events?since=abc", "/events?kind=nonsense", "/events?actor=robot", "/entities?kind=x"):
            answered = httpx.get(base + path, timeout=10)
            assert answered.status_code == 422, (path, answered.text)
        assert httpx.get(f"{served.url}/v1/unmatched?since=-1", timeout=10).status_code == 422
        assert httpx.get(base + "/events?since=0&kind=message", timeout=10).status_code == 200
    finally:
        served.client.close_world(world.world_id)
