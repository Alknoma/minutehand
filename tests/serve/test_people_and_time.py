"""A test speaking for a person, a scripted person answering when the clock passes the reply, and a world read
back as a run once it is closed."""

from __future__ import annotations

import json
import ssl
from datetime import timedelta

import httpx
from slack_sdk.signature import SignatureVerifier

from minutehand.adapters.control.wire import Claims, CreateWorld
from minutehand.domain.scenario import Seed, TicketState
from minutehand.domain.world import Actor, DocumentSnapshot, EntityKind, MessageSnapshot, Operation
from minutehand.testing.world import OpenWorld
from tests.serve.support import SECRET, Served, dm, event_receiver, seed, spec


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
            assert [f.what for f in late.fired] == ["sofia's reply (verbatim)"]
            assert receiver.texts() == ["Yes, Thursday works."]
            replies = world.events(actor=Actor.PERSON, operation=Operation.CREATE)
            # Her message, its push to the agent (recorded as the scenario's), the move it is (`transition`), then the
            # engine's record of it, closed.
            pushed = [e.seq for e in world.events() if e.entity.kind is EntityKind.PUSH]
            assert len(pushed) == 1
            assert [e.seq for e in replies] == [s for s in late.fired[0].events[:-1] if s not in pushed]
            assert [e.entity.kind for e in replies][-1] is EntityKind.TRANSITION
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


def _happening_world(served: Served, token: str, happening: dict[str, object], **seeded: object) -> OpenWorld:
    written = seed(("owen", "Owen Owner"), ("sofia", "Sofia Romano"))
    world_seed = Seed.model_validate({**written.model_dump(), **seeded, "happenings": [happening]})
    spec_ = CreateWorld(seed=world_seed, claims=Claims(tokens=[token]), scripted_people=False)
    return OpenWorld(served.client, served.client.create_world(spec_))


def test_a_ticket_happening_lands_when_the_clock_passes_it(served: Served) -> None:
    world = _happening_world(
        served,
        "pat-happens",
        {
            "kind": "ticket",
            "person": "sofia",
            "ticket": "Legal review",
            "after": "PT2H",
            "action": {"kind": "moves", "to": "done"},
        },
        tickets=[{"provider": "asana", "project": "Launch", "title": "Legal review", "assignee": "sofia"}],
    )
    try:
        assert world.advance(timedelta(hours=1)).fired == []
        late = world.advance(timedelta(hours=2))
        assert [f.what for f in late.fired] == ["happening 1: sofia moves the seeded ticket 'Legal review'"]
        world.assert_ticket(titled="legal review", state=TicketState.DONE, assignee="sofia@example.com")
        assert [e.actor for e in world.events(operation=Operation.UPDATE) if e.entity.kind is EntityKind.TICKET] == [
            Actor.PERSON
        ]
    finally:
        served.client.close_world(world.world_id)


def test_a_document_happening_lands_when_the_clock_passes_it(served: Served) -> None:
    world = _happening_world(
        served,
        "ya29.happens",
        {
            "kind": "document",
            "person": "sofia",
            "document": "Plan",
            "after": "PT2H",
            "action": {"kind": "renamed", "to": "Plan (final)"},
        },
        documents=[{"provider": "google_workspace", "title": "Plan", "text": "draft", "owner": "sofia"}],
    )
    try:
        assert world.advance(timedelta(hours=1)).fired == []
        late = world.advance(timedelta(hours=2))
        assert [f.what for f in late.fired] == ["happening 1: sofia renamed the seeded document 'Plan'"]
        renamed = [e for e in world.events(actor=Actor.PERSON) if isinstance(e.after, DocumentSnapshot)]
        assert [e.after.title for e in renamed if isinstance(e.after, DocumentSnapshot)] == ["Plan (final)"]
    finally:
        served.client.close_world(world.world_id)


def test_a_messaging_happening_is_pushed_when_the_clock_passes_it(served: Served) -> None:
    with event_receiver() as receiver:
        written = spec("xoxb-happens", inbound=receiver.url)
        world_seed = Seed.model_validate(
            {
                **written.seed.model_dump(),
                "happenings": [
                    {
                        "kind": "posts",
                        "provider": "slack",
                        "person": "sofia",
                        "text": "Booked it myself.",
                        "after": "PT2H",
                    }
                ],
            }
        )
        world = OpenWorld(served.client, served.client.create_world(written.model_copy(update={"seed": world_seed})))
        try:
            assert world.advance(timedelta(hours=1)).fired == [] and receiver.texts() == []
            late = world.advance(timedelta(hours=2))
            assert [f.what for f in late.fired] == ["happening 1: sofia posts"]
            assert receiver.texts() == ["Booked it myself."]
        finally:
            served.client.close_world(world.world_id)


def test_a_task_booked_on_a_scheduler_is_recorded_and_never_fires_however_far_the_clock_moves(served: Served) -> None:
    """A standing world fires no booking (docs/serve.md, "Booked wakes"): a Cloud Tasks task is created and kept as
    the provider's record, and moving the clock past its schedule delivers nothing."""
    queue = "projects/sim-project/locations/us-central1/queues/follow-ups"
    written = seed(("owen", "Owen Owner"))
    world_seed = Seed.model_validate(
        {
            **written.model_dump(),
            "provider_seeds": [{"provider": "google_cloud_tasks", "body": {"queues": [{"name": queue}]}}],
        }
    )
    spec_ = CreateWorld(seed=world_seed, claims=Claims(tokens=["ya29.books-a-task"]))
    world = OpenWorld(served.client, served.client.create_world(spec_))
    try:
        with httpx.Client(proxy=served.proxy, verify=ssl.create_default_context(cafile=served.bundle)) as http:
            task = {"httpRequest": {"url": "http://127.0.0.1:9/handler"}, "scheduleTime": "2026-09-01T10:00:00Z"}
            created = http.post(
                f"https://cloudtasks.googleapis.com/v2/{queue}/tasks",
                json={"task": task},
                headers={"Authorization": "Bearer ya29.books-a-task"},
            )
        assert created.status_code == 200, created.text
        name = created.json()["name"]
        assert world.advance(timedelta(days=1)).fired == []
        written_for = [e for e in world.events() if e.entity.external_id == name]
        assert [(e.actor, e.operation) for e in written_for] == [(Actor.AGENT, Operation.CREATE)]
    finally:
        served.client.close_world(world.world_id)
