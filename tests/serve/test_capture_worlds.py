"""Outbound hosts declared per world in the standing mode: each world answers and keeps its own calls to a host
no provider claims, and another world's declaration of the same host changes nothing for it."""

from __future__ import annotations

import requests

from minutehand.adapters.control.wire import Claims
from minutehand.domain.outbound import Acknowledge, Answer, MessageReading
from minutehand.domain.world import CaptureMode, MessageSnapshot
from minutehand.testing.client import Refused
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


def test_a_world_declaring_a_provider_host_is_refused_naming_both(served: Served) -> None:
    asked = spec("mail-key-five").model_copy(update={"outbound": [Acknowledge(host="slack.com")]})
    try:
        served.client.create_world(asked)
    except Refused as refused:
        assert refused.status == 409
        assert "'slack.com' is declared acknowledge, and provider 'slack' claims 'slack.com'" in refused.error
    else:
        raise AssertionError("a world declaring slack.com was opened")
    assert Claims(tokens=["mail-key-five"]) == asked.claims
