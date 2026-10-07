"""A message sent, rewritten in place and deleted is readable at every step: each event carries what the message said,
to whom and in which thread; the scorecard counts the rewrite and the delete; the viewer says "rewrote the message
to Sofia from '...' to '...'".

Before, a delete carried nothing of what was deleted, and a rewrite was counted nowhere."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import MessageChange, MessagesResponse
from minutehand.application.forks import scorecard_lines
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, answer, dm, spec


@pytest.fixture(scope="module")
def state(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("edits")


@pytest.fixture(scope="module")
def served(state: Path) -> Iterator[Served]:
    with serve_in_background(state) as url, MinutehandClient(url) as client:
        yield Served(url=url, client=client, environment=client.environment())


def test_a_rewrite_and_a_delete_carry_what_the_message_said_and_are_counted(served: Served, state: Path) -> None:
    token = "xoxb-edits"
    world = OpenWorld(served.client, served.client.create_world(spec(token)))
    slack = served.slack(token)
    channel = dm(served, token, "sofia@example.com")
    with world.step(reason="the only step"):
        sent = answer(slack.chat_postMessage(channel=channel, text="Is the venue booked?"))
        slack.chat_update(channel=channel, ts=sent["ts"], text="Is the venue for Thursday booked?")
        gone = answer(slack.chat_postMessage(channel=channel, text="Ignore that last one."))
        slack.chat_delete(channel=channel, ts=gone["ts"])
    written = [
        e
        for e in world.events(provider="slack", kind=EntityKind.MESSAGE)
        if e.operation is not Operation.READ and e.actor is Actor.AGENT
    ]
    closed = world.close()

    said = [(e.operation, e.after) for e in written]
    assert [(op, a.text if isinstance(a, MessageSnapshot) else None) for op, a in said] == [
        (Operation.CREATE, "Is the venue booked?"),
        (Operation.UPDATE, "Is the venue for Thursday booked?"),
        (Operation.CREATE, "Ignore that last one."),
        (Operation.DELETE, "Ignore that last one."),
    ]
    assert all(isinstance(a, MessageSnapshot) and a.recipient_emails == ["sofia@example.com"] for _, a in said)

    card = closed.result.effectiveness
    assert (card.messages_to_people, card.messages_edited, card.messages_deleted) == (2, 1, 1)
    [line] = [x for x in scorecard_lines(card) if x.label == "messages to people"]
    assert line.value == "2, edited in place: 1, deleted: 1"

    with TestClient(create_app(state)) as viewer:
        page = MessagesResponse.model_validate_json(viewer.get(f"/api/runs/{world.world_id}/messages").content)
    assert [(m.change, m.to, m.text, m.before) for m in page.messages] == [
        (MessageChange.SENT, ["Sofia Romano"], "Is the venue booked?", None),
        (MessageChange.EDITED, ["Sofia Romano"], "Is the venue for Thursday booked?", "Is the venue booked?"),
        (MessageChange.SENT, ["Sofia Romano"], "Ignore that last one.", None),
        (MessageChange.DELETED, ["Sofia Romano"], "Ignore that last one.", None),
    ]
