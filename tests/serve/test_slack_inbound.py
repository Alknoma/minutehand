"""Every shape a person pushes to a Slack app reaches it through the control API, signed so that the app's own
verifier (`slack_sdk.signature.SignatureVerifier`, as Bolt runs it) accepts it; and a request a test builds itself is
signed by `POST /inbound-credential` the same way."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from slack_sdk.signature import SignatureVerifier

from minutehand.adapters.control.wire import Claims, CreateWorld, Inbound
from minutehand.domain.people import InboundCredentialAsk, Press
from minutehand.domain.scenario import (
    FormInput,
    PersonAddsAgent,
    PersonCommands,
    PersonDeletes,
    PersonEdits,
    PersonJoins,
    PersonOpensAgent,
    PersonPosts,
    PersonReacts,
    Seed,
)
from minutehand.domain.world import Actor, EntityKind, MessageSnapshot, Operation
from minutehand.testing.client import Refused
from minutehand.testing.world import OpenWorld
from tests.serve.support import Served, answer, dm

SECRET = "the-apps-signing-secret"


@dataclass
class Verified:
    """What a Slack app's endpoint received: each body it accepted, and how many it refused as unsigned."""

    url: str
    accepted: list[bytes] = field(default_factory=list)
    refused: int = 0

    def events(self) -> list[dict[str, object]]:
        return [json.loads(b) for b in self.accepted if b.startswith(b"{")]

    def payloads(self) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for body in self.accepted:
            form = parse_qs(body.decode())
            if "payload" in form:
                found.append(json.loads(form["payload"][0]))
        return found

    def commands(self) -> list[dict[str, list[str]]]:
        return [parse_qs(b.decode()) for b in self.accepted if b"command=" in b and not b.startswith(b"payload=")]


@contextmanager
def slack_app(secret: str, on_payload: Callable[[dict[str, object]], None] | None = None) -> Iterator[Verified]:
    """A Slack app's request URL: each request verified as Bolt verifies it, 401 when the signature is not Slack's."""
    verifier = SignatureVerifier(secret)
    found = Verified(url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            stamp, signature = self.headers["X-Slack-Request-Timestamp"], self.headers["X-Slack-Signature"]
            if not verifier.is_valid(body=body, timestamp=stamp, signature=signature):
                found.refused += 1
                self.send_response(401)
                self.end_headers()
                return
            found.accepted.append(body)
            form = parse_qs(body.decode()) if not body.startswith(b"{") else {}
            if on_payload is not None and "payload" in form:
                on_payload(json.loads(form["payload"][0]))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    found.url = f"http://127.0.0.1:{server.server_address[1]}/slack/events"
    try:
        yield found
    finally:
        server.shutdown()
        server.server_close()


def _seed() -> Seed:
    silent = {"kind": "silent"}
    return Seed.model_validate(
        {
            "starts_at": "2026-09-01T09:00:00Z",
            "people": [
                {"key": "owen", "name": "Owen Owner", "email": "owen@example.com", "reply": silent},
                {"key": "sofia", "name": "Sofia Romano", "email": "sofia@example.com", "reply": silent},
            ],
            "channels": [
                {"provider": "slack", "name": "launch", "members": ["owen", "sofia"],
                 "history": [{"by": "sofia", "text": "Kickoff is Monday.", "ago": "PT1H", "key": "kickoff"}]},
                {"provider": "slack", "name": "lounge", "members": ["sofia"], "agent_member": False},
                {"provider": "slack", "name": "random", "members": ["owen"]},
            ],
        }
    )  # fmt: skip


@contextmanager
def _world(served: Served, token: str, app: Verified) -> Iterator[OpenWorld]:
    spec = CreateWorld(
        seed=_seed(), claims=Claims(tokens=[token]), inbound=[Inbound(provider="slack", url=app.url, secret=SECRET)]
    )
    world = OpenWorld(served.client, served.client.create_world(spec))
    try:
        yield world
    finally:
        served.client.close_world(world.world_id)


def _event_types(app: Verified) -> list[str]:
    return [str(e["event"]["type"]) for e in app.events()]  # type: ignore[index]


def test_every_event_shape_a_person_pushes_is_accepted_by_the_apps_own_verifier(served: Served) -> None:
    with slack_app(SECRET) as app, _world(served, "xoxb-inbound-events", app) as world:
        world.say("sofia", "Can you chase the venue?")
        world.happen(PersonPosts(provider="slack", person="sofia", channel="launch", text="Venue is late", key="late"))
        world.happen(PersonPosts(provider="slack", person="owen", channel="launch", text="On it", in_thread_of="late"))
        world.happen(PersonPosts(provider="slack", person="owen", channel="launch", text="please look",
                                 mentions_agent=True))  # fmt: skip
        world.happen(PersonEdits(provider="slack", person="sofia", post="late", text="Venue is very late"))
        world.happen(PersonReacts(provider="slack", person="owen", post="kickoff", reaction="eyes"))
        world.happen(PersonDeletes(provider="slack", person="sofia", post="late"))
        world.happen(PersonJoins(provider="slack", person="sofia", channel="random"))
        world.happen(PersonAddsAgent(provider="slack", person="sofia", channel="lounge"))
        opened = world.happen(PersonOpensAgent(provider="slack", person="owen"))
        assert opened.actor is Actor.PERSON and opened.operation is Operation.READ
        sent = answer(served.slack("xoxb-inbound-events").chat_postMessage(
            channel=dm(served, "xoxb-inbound-events", "sofia@example.com"), text="Which venue?"))  # fmt: skip
        agents = next(
            e.entity for e in world.events(provider="slack", actor=Actor.AGENT) if e.entity.kind is EntityKind.MESSAGE
        )
        assert agents.external_id == sent["ts"]
        world.reply("sofia", "The one on Main Street.", to=agents)
        assert app.refused == 0
        types = _event_types(app)
        assert types == [
            "message", "message", "message", "message", "app_mention", "message", "reaction_added", "message",
            "member_joined_channel", "member_joined_channel", "app_home_opened", "message",
        ]  # fmt: skip
        subtypes = [e["event"].get("subtype") for e in app.events()]  # type: ignore[union-attr]
        assert subtypes[5] == "message_changed" and subtypes[7] == "message_deleted"
        threaded = app.events()[2]["event"]
        assert threaded["thread_ts"] == app.events()[1]["event"]["ts"]  # type: ignore[index]
        assert {e["team_id"] for e in app.events()} == {"T0WORKSPACE"}


def test_a_slash_command_a_button_and_a_modal_submitted_are_accepted_by_the_apps_own_verifier(served: Served) -> None:
    token = "xoxb-inbound-interactive"

    def open_modal(payload: dict[str, object]) -> None:
        if payload["type"] != "block_actions":
            return
        view = {"type": "modal", "callback_id": "venue_form", "title": {"type": "plain_text", "text": "Venue"},
                "submit": {"type": "plain_text", "text": "Send"},
                "blocks": [{"type": "input", "block_id": "where", "label": {"type": "plain_text", "text": "Where"},
                            "element": {"type": "plain_text_input", "action_id": "where_text"}}]}  # fmt: skip
        served.slack(token).views_open(trigger_id=str(payload["trigger_id"]), view=view)

    with slack_app(SECRET, open_modal) as app, _world(served, token, app) as world:
        world.happen(PersonCommands(provider="slack", person="owen", command="/remind", text="venue friday"))
        blocks = [{"type": "actions", "elements": [{"type": "button", "action_id": "choose", "value": "venue",
                                                    "text": {"type": "plain_text", "text": "Choose"}}]}]  # fmt: skip
        answer(served.slack(token).chat_postMessage(
            channel=dm(served, token, "sofia@example.com"), text="Choose a venue", blocks=blocks))  # fmt: skip
        message = next(
            e.entity
            for e in world.events(provider="slack", actor=Actor.AGENT)
            if isinstance(e.after, MessageSnapshot) and e.after.actions
        )
        world.press("sofia", message, Press(action_id="choose", label="Choose", value="venue"))
        world.press(
            "sofia",
            message,
            Press(action_id="choose", label="Choose", value="venue", form=[FormInput(value="Main Street hall")]),
        )
        assert app.refused == 0
        assert app.commands()[0]["command"] == ["/remind"]
        kinds = [p["type"] for p in app.payloads()]
        assert kinds == ["block_actions", "block_actions", "view_submission"]
        submitted = app.payloads()[-1]["view"]
        assert submitted["state"]["values"]["where"]["where_text"]["value"] == "Main Street hall"  # type: ignore[index]


def test_a_minted_signature_is_accepted_by_the_apps_own_verifier(served: Served) -> None:
    with slack_app(SECRET) as app, _world(served, "xoxb-inbound-minted", app) as world:
        body = json.dumps({"type": "event_callback", "event": {"type": "message", "text": "built by the test"}})
        stamp = int(time.time())
        minted = world.inbound_credential("slack", InboundCredentialAsk(body=body, timestamp=stamp))
        headers = {h.name: h.value for h in minted.headers}
        verifier = SignatureVerifier(SECRET)
        assert verifier.is_valid(body=body, timestamp=headers["X-Slack-Request-Timestamp"],
                                 signature=headers["X-Slack-Signature"])  # fmt: skip
        assert not SignatureVerifier("another-secret").is_valid(
            body=body, timestamp=headers["X-Slack-Request-Timestamp"], signature=headers["X-Slack-Signature"]
        )
        assert not verifier.is_valid(body=body + " ", timestamp=str(stamp), signature=headers["X-Slack-Signature"])


def test_a_minted_signature_without_a_timestamp_is_refused(served: Served) -> None:
    with slack_app(SECRET) as app, _world(served, "xoxb-inbound-no-stamp", app) as world:
        with pytest.raises(Refused) as refused:
            world.inbound_credential("slack", InboundCredentialAsk(body="{}"))
        assert refused.value.status == 409 and "timestamp" in refused.value.error


def test_a_minted_signature_in_a_world_with_no_slack_inbound_is_refused(served: Served) -> None:
    world = OpenWorld(
        served.client, served.client.create_world(CreateWorld(seed=_seed(), claims=Claims(tokens=["xoxb-no-in"])))
    )
    try:
        with pytest.raises(Refused) as refused:
            world.inbound_credential("slack", InboundCredentialAsk(body="{}", timestamp=int(time.time())))
        assert refused.value.status == 409 and "no inbound target" in refused.value.error
    finally:
        served.client.close_world(world.world_id)


def test_an_event_signed_with_another_secret_is_refused_by_the_apps_verifier(served: Served) -> None:
    """The receiver above refuses a bad signature: shown so its acceptances mean something."""
    with slack_app("not-the-secret-the-world-signs-with") as app, _world(served, "xoxb-inbound-wrong", app) as world:
        with pytest.raises(Refused) as refused:
            world.say("sofia", "hello")
        assert refused.value.status == 502 and app.refused >= 1 and not app.accepted
