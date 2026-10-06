"""Base-URL mode under `minutehand serve`: a call made by base URL reaches the world that claims its credential,
exactly as one made through the proxy does."""

from __future__ import annotations

from slack_sdk import WebClient

from minutehand.adapters.proxy.base_url import base_url
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, spec


def _general(world: OpenWorld) -> str:
    [channel] = [s for s in world.entities(provider="slack", kind=EntityKind.CHANNEL) if '"is_general":true' in s.body]
    return channel.entity.external_id


def test_a_base_url_call_lands_in_the_world_its_token_names(served: Served) -> None:
    first = OpenWorld(served.client, served.client.create_world(spec("xoxb-base-one")))
    second = OpenWorld(served.client, served.client.create_world(spec("xoxb-base-two")))
    try:
        slack = WebClient(token="xoxb-base-two", base_url=f"{base_url(served.proxy, 'slack.com')}/api/")
        slack.chat_postMessage(channel=_general(second), text="by base URL")
        posted = [
            e.after.text
            for e in second.events(provider="slack", actor=Actor.AGENT, operation=Operation.CREATE)
            if isinstance(e.after, MessageSnapshot)
        ]
        assert posted == ["by base URL"]
        assert first.calls() == []
        assert [(c.exchange.host, c.exchange.path) for c in second.calls()] == [("slack.com", "/api/chat.postMessage")]
    finally:
        served.client.close_world(first.world_id)
        served.client.close_world(second.world_id)
