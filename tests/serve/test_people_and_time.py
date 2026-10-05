"""A test speaking for a person, a scripted person answering when the clock passes the reply, and a world read
back as a run once it is closed."""

from __future__ import annotations

import json
from datetime import timedelta

from slack_sdk.signature import SignatureVerifier

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed, TicketState
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation
from minutehand.testing.world import OpenWorld
from tests.serve.support import SECRET, Served, dm, event_receiver, spec


def test_a_person_speaking_reaches_the_services_inbound_endpoint_signed(served: Served) -> None:
    with event_receiver() as receiver:
        world = OpenWorld(served.client, served.client.create_world(spec("xoxb-speaks", inbound=receiver.url)))
        try:
            said = world.say("sofia", "The venue is booked.")
            assert said.actor is Actor.PERSON and isinstance(said.after, MessageSnapshot)
            assert receiver.texts() == ["The venue is booked."]
            pushed = receiver.pushed[0]
            verifier = SignatureVerifier(SECRET)
            assert verifier.is_valid(pushed.body, pushed.timestamp, pushed.signature)
            assert not SignatureVerifier("another-secret").is_valid(pushed.body, pushed.timestamp, pushed.signature)
            event = json.loads(pushed.body)["event"]
            assert event["channel"] == said.after.channel and event["channel_type"] == "im"
        finally:
            served.client.close_world(world.world_id)


def test_advancing_the_clock_past_a_scripted_reply_delivers_it_and_not_before(served: Served) -> None:
    with event_receiver() as receiver:
        world = OpenWorld(
            served.client,
            served.client.create_world(
                spec("xoxb-scripted", inbound=receiver.url, scripted={"sofia": "Yes, Thursday works."})
            ),
        )
        try:
            channel = dm(served, "xoxb-scripted", "sofia@example.com")
            served.slack("xoxb-scripted").chat_postMessage(channel=channel, text="Does Thursday work?")

            early = world.advance(timedelta(minutes=59))
            assert early.fired == [] and receiver.texts() == []

            late = world.advance(timedelta(minutes=2))
            assert [f.what for f in late.fired] == ["sofia's scripted reply"]
            assert receiver.texts() == ["Yes, Thursday works."]
            replies = world.events(actor=Actor.PERSON, operation=Operation.CREATE)
            assert [e.seq for e in replies] == late.fired[0].events
        finally:
            served.client.close_world(world.world_id)


def test_with_scripted_people_off_nobody_answers_however_far_the_clock_moves(served: Served) -> None:
    with event_receiver() as receiver:
        quiet = spec("xoxb-quiet", inbound=receiver.url, scripted={"sofia": "Yes."}).model_copy(
            update={"scripted_people": False}
        )
        world = OpenWorld(served.client, served.client.create_world(quiet))
        try:
            channel = dm(served, "xoxb-quiet", "sofia@example.com")
            served.slack("xoxb-quiet").chat_postMessage(channel=channel, text="Does Thursday work?")
            assert world.advance(timedelta(days=30)).fired == [] and receiver.texts() == []
        finally:
            served.client.close_world(world.world_id)


def test_a_person_finishes_a_ticket_without_the_agent(served: Served) -> None:
    seeded = Seed.model_validate(
        {
            "people": [
                {"key": "owen", "name": "Owen", "email": "owen@example.com"},
                {"key": "dania", "name": "Dania", "email": "dania@example.com"},
            ],
            "tickets": [{"provider": "asana", "project": "Launch", "title": "Legal review", "assignee": "dania"}],
        }
    )
    world = OpenWorld(
        served.client, served.client.create_world(CreateWorld(seed=seeded, claims=Claims(tokens=["pat-1"])))
    )
    try:
        ticket = world.entities(provider="asana", kind=EntityKind.TICKET)[0].entity
        moved = world.move_ticket(ticket, TicketState.DONE)
        assert moved.actor is Actor.PERSON
        world.assert_ticket(titled="legal review", state=TicketState.DONE, assignee="dania@example.com")
        reassigned = world.edit_ticket(ticket, assignee="owen")
        assert reassigned.actor is Actor.SCENARIO
        world.assert_ticket(titled="legal review", assignee="owen@example.com")
    finally:
        served.client.close_world(world.world_id)
