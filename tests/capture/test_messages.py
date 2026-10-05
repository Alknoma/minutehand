"""A send through an acknowledged host, read by the declaration's paths as a message from the agent to a person,
lands in the world log beside the call that carried it; an address nobody in the scenario has stays as written."""

from __future__ import annotations

import json
from pathlib import Path

from minutehand.adapters.proxy.capture import Capturing, read_message
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.outbound import outbound_uses
from minutehand.application.run_clock import RunClock
from minutehand.domain.outbound import Acknowledge, HtmlAt, MessageReading
from minutehand.domain.scenario import Person, Silent
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation, Recipient
from tests.capture.support import Call, by_environment
from tests.proxy.upstream import Authority

SOFIA = Person(key="sofia", name="Sofia Romano", email="sofia@example.com", reply=Silent())
OWEN = Person(key="owen", name="Owen Owner", email="owen@example.com", reply=Silent())
PEOPLE = [OWEN, SOFIA]

SENDGRID = MessageReading(
    recipients=["personalizations[*].to[*].email", "personalizations[*].cc[*].email"],
    text=["text", HtmlAt(html="content[0].value")],
    subject=["subject"],
    handles={"+15550100": "owen"},
)
JSON = {"content-type": "application/json"}


def _mail(*to: str, html: str = "<p>Could you <b>confirm</b> the pricing?</p><script>x()</script>") -> str:
    return json.dumps(
        {
            "personalizations": [{"to": [{"email": a} for a in to]}],
            "subject": "Partner pricing",
            "content": [{"type": "text/html", "value": html}],
        }
    )


async def _send(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority, *bodies: str
) -> None:
    declared = Acknowledge(host="api.mail.test", name="mail", message=SENDGRID)
    capturing = Capturing([declared]).for_people(PEOPLE)
    async with Proxy(
        Routing(registry), store, clock, confdir=tmp_path / "ca", upstream_ca=authority.ca_cert, capturing=capturing
    ) as proxy:
        await by_environment(proxy, [Call("POST", "https://api.mail.test/v3/mail/send", b, JSON) for b in bodies])


async def test_a_send_is_a_message_from_the_agent_to_the_person_it_names(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    clock.begin_wake()
    await _send(registry, store, clock, tmp_path, authority, _mail("Sofia@Example.com"))
    [event] = store.events()
    assert (event.actor, event.operation, event.entity.provider, event.entity.kind, event.wake) == (
        Actor.AGENT,
        Operation.CREATE,
        "mail",
        EntityKind.MESSAGE,
        1,
    )
    assert event.after == MessageSnapshot(
        text="Partner pricing\n\nCould you confirm the pricing?",
        channel="to:sofia@example.com",
        recipient_emails=["sofia@example.com"],
        answerable=False,
    )
    # The call that carried it is tied to it, as a provider's call is to what it wrote.
    assert event.exchange is not None and event.exchange.captured is not None
    assert event.exchange.captured.recipients == [Recipient(address="Sofia@Example.com", person="sofia")]


async def test_an_unknown_recipient_is_kept_as_written_and_said_to_be_unknown(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    await _send(registry, store, clock, tmp_path, authority, _mail("sofia@example.com", "dana@elsewhere.example"))
    [event] = store.events()
    assert isinstance(event.after, MessageSnapshot)
    assert event.after.recipient_emails == ["sofia@example.com", "dana@elsewhere.example"]
    [use] = outbound_uses(store.calls())
    assert use.unknown_recipients == ["dana@elsewhere.example"]


async def test_a_send_with_nothing_to_read_is_kept_as_a_call_and_says_why_it_is_no_message(
    registry: Registry, store: SqliteStore, clock: RunClock, tmp_path: Path, authority: Authority
) -> None:
    await _send(registry, store, clock, tmp_path, authority, json.dumps({"subject": "no one"}))
    assert store.events() == []
    [call] = store.calls()
    assert call.exchange.captured is not None
    assert call.exchange.captured.note == (
        "not read as a message: no recipient at personalizations[*].to[*].email, personalizations[*].cc[*].email"
    )


def test_recipients_are_read_from_strings_lists_and_objects_and_matched_by_email_or_handle() -> None:
    reading = MessageReading(recipients=["to", "sms"], text=["body"], handles={"+15550100": "owen"})
    sent = json.dumps(
        {"to": "Sofia Romano <SOFIA@example.com>, nobody@example.com", "sms": ["+15550100"], "body": "hi"}
    )
    read = read_message(reading, sent, "application/json", PEOPLE)
    assert read.recipients == [
        Recipient(address="SOFIA@example.com", person="sofia"),
        Recipient(address="nobody@example.com", person=None),
        Recipient(address="+15550100", person="owen"),
    ]


def test_a_form_body_is_read_by_its_fields_and_the_first_text_present_wins() -> None:
    reading = MessageReading(recipients=["to"], text=["text", HtmlAt(html="html")], subject=["subject"])
    form = "to=sofia%40example.com&to=owen%40example.com&subject=Hi&html=%3Cp%3EOnly+html%3C%2Fp%3E"
    read = read_message(reading, form, "application/x-www-form-urlencoded", PEOPLE)
    assert [r.person for r in read.recipients] == ["sofia", "owen"]
    assert (read.subject, read.text) == ("Hi", "Only html")
