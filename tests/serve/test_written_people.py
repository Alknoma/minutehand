"""A standing world's people are as complete as a run's: what they say and decide is written by the model the server
is configured with (from its own environment), owed at a moment drawn as in a run, written when `advance` passes
it, kept with the world and replayed after a reset. A world whose people a model speaks for, on a server with no
model, is refused when it is created."""

from __future__ import annotations

import json
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from examples.recipes import fake_model
from minutehand.adapters.control.wire import Claims, CreateWorld, Inbound
from minutehand.adapters.model.openai_compatible import VARIABLES
from minutehand.domain.conversation import Wrote
from minutehand.domain.scenario import Seed
from minutehand.testing.background import serve_in_background
from minutehand.testing.client import MinutehandClient
from minutehand.testing.world import OpenWorld
from tests.inboxes.product import Product, serving
from tests.inboxes.support import NADIA, TOKENS, inbox
from tests.serve.support import SECRET, Served, dm, event_receiver
from tests.support.people import people_environment, people_requests

FACTS = ["Thursday works for me", "the room is booked from 10"]
SAID = "Thursday works for me. The room is booked from 10."


def served_with(environment: dict[str, str], tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    """A server whose own environment holds exactly `environment` of the model's variables."""
    with pytest.MonkeyPatch.context() as patched:
        for name in VARIABLES:
            patched.delenv(name, raising=False)
        for name, value in environment.items():
            patched.setenv(name, value)
        with serve_in_background(tmp_path_factory.mktemp("serve")) as url, MinutehandClient(url) as client:
            yield Served(url=url, client=client, environment=client.environment())


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Served]:
    yield from served_with(people_environment(), tmp_path_factory)


def sofia(**more: object) -> dict[str, object]:
    return {
        "key": "sofia",
        "name": "Sofia Romano",
        "email": "sofia@example.com",
        "facts": FACTS,
        "reply": {"kind": "answers", "delay": {"shortest": "PT1H", "longest": "PT1H"}},
        **more,
    }


def spec(token: str, receiver: str, *people: dict[str, object]) -> CreateWorld:
    owen = {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": {"kind": "silent"}}
    return CreateWorld(
        seed=Seed.model_validate({"starts_at": "2026-09-01T09:00:00Z", "people": [owen, *people]}),
        claims=Claims(tokens=[token]),
        inbound=[Inbound(provider="slack", url=receiver, secret=SECRET)],
        scripted_people=True,
    )


def test_a_person_a_model_writes_for_answers_only_once_advance_passes_their_moment(written: Served) -> None:
    with event_receiver() as receiver:
        world = OpenWorld(written.client, written.client.create_world(spec("xoxb-written", receiver.url, sofia())))
        try:
            channel = dm(written, "xoxb-written", "sofia@example.com")
            written.slack("xoxb-written").chat_postMessage(channel=channel, text="Does Thursday work?")

            early = world.advance(timedelta(minutes=59))
            assert early.fired == [] and receiver.texts() == []
            [owed] = written.client.world(world.world_id).people_owe
            assert (owed.person, owed.writing, owed.decision) == ("sofia", "conversing", False)
            assert owed.at == written.client.world(world.world_id).now + timedelta(minutes=1)

            late = world.advance(timedelta(minutes=2))
            assert [f.what for f in late.fired] == ["sofia's reply (conversing)"]
            assert receiver.texts() == [SAID]
            view = written.client.world(world.world_id)
            assert view.people_owe == []
            [call] = view.person_calls
            assert call.wrote is Wrote.REPLY and call.model == "people-fake" and call.prompt_version == "person-reply/3"
            assert call.input_tokens and call.output_tokens and not call.replayed
        finally:
            written.client.close_world(world.world_id)


def test_a_reply_is_replayed_after_a_reset_and_asks_the_model_nothing(written: Served) -> None:
    with event_receiver() as receiver:
        world = OpenWorld(written.client, written.client.create_world(spec("xoxb-replayed", receiver.url, sofia())))
        try:
            asked = len(people_requests())
            for _ in range(2):
                channel = dm(written, "xoxb-replayed", "sofia@example.com")
                written.slack("xoxb-replayed").chat_postMessage(channel=channel, text="Does Thursday work?")
                asked = len(people_requests())
                world.advance(timedelta(hours=2))
                written.client.reset(world.world_id)
            assert receiver.texts() == [SAID, SAID]
            assert len(people_requests()) == asked, "the second time, the same ask in the same context was replayed"
        finally:
            written.client.close_world(world.world_id)


def test_a_person_away_while_someone_covers_sends_the_automatic_reply_at_once_and_nothing_else(
    written: Served,
) -> None:
    away = sofia(
        reply={"kind": "scripted", "then": "silent"},
        absences=[{"lasts": "P3D", "delegate": "owen", "reason": "on leave"}],
    )
    with event_receiver() as receiver:
        world = OpenWorld(written.client, written.client.create_world(spec("xoxb-away", receiver.url, away)))
        try:
            channel = dm(written, "xoxb-away", "sofia@example.com")
            written.slack("xoxb-away").chat_postMessage(channel=channel, text="Does Thursday work?")
            world.advance(timedelta(seconds=1))
            assert receiver.texts() == [
                "Automatic reply: Sofia Romano is away (on leave) until Friday 4 September 2026, 09:00 UTC. For "
                "anything urgent, please contact Owen Owner (owen@example.com)."
            ]
            world.advance(timedelta(days=5))
            assert len(receiver.texts()) == 1
        finally:
            written.client.close_world(world.world_id)


def test_a_person_a_model_writes_for_decides_an_item_in_the_services_own_product(written: Served) -> None:
    nadia = {
        "key": "nadia",
        "name": "Nadia Ek",
        "email": NADIA,
        "facts": ["The office has no room that day"],
        "reply": {"kind": "answers", "delay": {"shortest": "PT2H", "longest": "PT2H"}},
    }
    with serving(Product(tokens=TOKENS)) as product, event_receiver() as receiver:
        declared = spec("xoxb-decides", receiver.url, nadia).model_copy(
            update={"inboxes": [inbox(product)], "credentials": {"nadia": TOKENS[NADIA]}}
        )
        world = OpenWorld(written.client, written.client.create_world(declared))
        try:
            product.raise_approval("a1", NADIA, "Book the offsite room", "op-1")
            [due] = world.inboxes().due
            assert (due.person, due.decision) == ("nadia", None), "a model decides when it falls due"
            world.advance(timedelta(hours=1))
            assert product.state("a1") == "pending"
            world.advance(timedelta(hours=2))
            assert product.state("a1") == "approved"
            [call] = written.client.world(world.world_id).person_calls
            assert call.wrote is Wrote.TRANSITION and call.prompt_version == "person-transition/1"
        finally:
            written.client.close_world(world.world_id)


class _FailsFirst(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


@contextmanager
def model_that_fails_first() -> Iterator[str]:
    """A model API whose first answer is a 503 and every later one the recipes' stand-in's: its base URL."""
    asked: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            asked.append(1)
            schema = fake_model.people_schema(body)
            if len(asked) == 1 or schema is None:
                payload, status = b'{"error": {"message": "overloaded"}}', 503
            else:
                payload, status = json.dumps(fake_model.people_completion(body, schema, len(asked))).encode(), 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = _FailsFirst(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
