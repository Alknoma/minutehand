"""Outbound hosts declared per world in the standing mode: each world answers and keeps its own calls to a host
no provider claims, and another world's declaration of the same host changes nothing for it."""

from __future__ import annotations

import requests

from minutehand.domain.outbound import Acknowledge, Answer, Collection, DeclaredStore, MessageReading
from minutehand.domain.world import CaptureMode, MessageSnapshot, StoredSnapshot
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, spec

MAIL = "https://api.mail.test/v1/send"


def _mailer(served: Served, token: str) -> requests.Session:
    """A service's email client as Minutehand's environment configures it, sending with its world's key."""
    configured = requests.Session()
    configured.proxies = {"https": served.proxy}
    configured.verify = served.bundle
    configured.trust_env = False
    configured.headers["authorization"] = f"Bearer {token}"
    return configured


def _declaring(token: str, world: str) -> Acknowledge:
    return Acknowledge(
        host="api.mail.test",
        name="mail",
        answer=Answer(status=202, json_body={"world": world}),
        message=MessageReading(recipients=["to"], text=["text"]),
    )


def test_two_worlds_declaring_one_host_each_answer_and_keep_only_their_own_calls(served: Served) -> None:
    first = OpenWorld(
        served.client,
        served.client.create_world(spec("mail-key-one").model_copy(update={"outbound": [_declaring("one", "one")]})),
    )
    second = OpenWorld(
        served.client,
        served.client.create_world(spec("mail-key-two").model_copy(update={"outbound": [_declaring("two", "two")]})),
    )
    try:
        to_first = _mailer(served, "mail-key-one").post(MAIL, json={"to": "sofia@example.com", "text": "one"})
        to_second = _mailer(served, "mail-key-two").post(MAIL, json={"to": "owen@example.com", "text": "two"})
        assert (to_first.status_code, to_first.json()) == (202, {"world": "one"})
        assert (to_second.status_code, to_second.json()) == (202, {"world": "two"})
        for world, text, to in ((first, "one", "sofia@example.com"), (second, "two", "owen@example.com")):
            [call] = world.captured_calls()
            assert call.exchange.captured is not None and call.exchange.captured.mode is CaptureMode.ACKNOWLEDGE
            assert world.unmatched_calls() == []
            [sent] = world.events(provider="mail")
            assert isinstance(sent.after, MessageSnapshot)
            assert (sent.after.text, sent.after.recipient_emails) == (text, [to])
    finally:
        served.client.close_world(first.world_id)
        served.client.close_world(second.world_id)


def test_a_world_that_declares_nothing_refuses_the_host_another_world_declares(served: Served) -> None:
    declaring = OpenWorld(
        served.client,
        served.client.create_world(spec("mail-key-three").model_copy(update={"outbound": [_declaring("x", "x")]})),
    )
    plain = OpenWorld(served.client, served.client.create_world(spec("mail-key-four")))
    try:
        refused = _mailer(served, "mail-key-four").post(MAIL, json={"to": "sofia@example.com", "text": "hi"})
        assert refused.status_code == 502
        [call] = plain.unmatched_calls()
        assert call.exchange.host == "api.mail.test" and plain.captured_calls() == []
        assert declaring.calls() == []
    finally:
        served.client.close_world(declaring.world_id)
        served.client.close_world(plain.world_id)


def test_a_world_declaring_a_provider_host_keeps_what_the_provider_does_not_serve(served: Served) -> None:
    """Slack's fake answers what it serves in the world; `reminders.add`, which it says it does not serve, falls
    through to the world's own declaration for slack.com and is kept there, credentials unchecked."""
    declared = DeclaredStore(host="slack.com", name="slack_extra", collections=[Collection(path="/api/reminders.add")])
    world = OpenWorld(
        served.client, served.client.create_world(spec("xoxb-six").model_copy(update={"outbound": [declared]}))
    )
    try:
        slack = _mailer(served, "xoxb-six")
        added = slack.post("https://slack.com/api/reminders.add", json={"text": "call Sofia", "time": "in 1 hour"})
        served_here = slack.post("https://slack.com/api/auth.test")
        assert added.status_code == 201
        reminder = added.json()
        assert {k: v for k, v in reminder.items() if k != "id"} == {"text": "call Sofia", "time": "in 1 hour"}
        assert served_here.json()["ok"] is True
        [kept] = world.captured_calls()
        assert kept.exchange.captured is not None and kept.provider is None
        assert (kept.exchange.captured.mode, kept.exchange.captured.not_served_by) == (CaptureMode.STORE, "slack")
        [item] = world.events(provider="slack_extra")
        assert isinstance(item.after, StoredSnapshot) and item.after.id == reminder["id"]
        assert [c.provider for c in world.calls() if c.exchange.path == "/api/auth.test"] == ["slack"]
    finally:
        served.client.close_world(world.world_id)
