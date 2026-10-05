"""A stand-in agent: an ordinary program that knows Slack (stock `slack_sdk`) and the wake contract, nothing else.

    python standin.py          listens on STANDIN_PORT

Its whole behaviour is chosen by its environment, so a test states the story it plays in one place:

    STANDIN_PORT          where it listens (required)
    STANDIN_SECRET        the secret Slack signs pushed requests with (required)
    STANDIN_ASK           the email of the person it asks on its first wake; nobody when unset
    STANDIN_QUESTION      what it asks (default "Could you confirm the venue, please?")
    STANDIN_BUTTON        a button label put on the question; a press of it is the answer
    STANDIN_FORM          with STANDIN_BUTTON: a press opens a form with one text field, and what is typed is the answer
    STANDIN_RELAY_TO      the email it tells the answer to, prefixed "Relaying: "
    STANDIN_FOLLOW_UPS    how many times it follows up an unanswered question (default 0), then looks once more
    STANDIN_EVERY_HOURS   how long it waits before each follow-up (default 24)
    STANDIN_DONE_AT_START "1": reports done straight after its first wake, whatever it asked
    STANDIN_FETCH         URLs fetched once on the first wake, separated by spaces (any answer is accepted)

It never reads the machine's clock: "now" is what the last wake said.
"""

from __future__ import annotations

import contextlib
import json
import os
import socketserver
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web import SlackResponse

ASK = os.environ.get("STANDIN_ASK")
QUESTION = os.environ.get("STANDIN_QUESTION", "Could you confirm the venue, please?")
BUTTON = os.environ.get("STANDIN_BUTTON")
FORM = os.environ.get("STANDIN_FORM") == "1"
RELAY_TO = os.environ.get("STANDIN_RELAY_TO")
FOLLOW_UPS = int(os.environ.get("STANDIN_FOLLOW_UPS", "0"))
EVERY = timedelta(hours=float(os.environ.get("STANDIN_EVERY_HOURS", "24")))
DONE_AT_START = os.environ.get("STANDIN_DONE_AT_START") == "1"
FETCH = os.environ.get("STANDIN_FETCH", "").split()


def answered(response: SlackResponse) -> dict[str, Any]:
    """Slack's JSON for one call; the SDK has already raised on `ok: false`."""
    assert isinstance(response.data, dict)
    return response.data


class Agent:
    def __init__(self) -> None:
        self.slack = WebClient(token="xoxb-standin")
        self.verifier = SignatureVerifier(os.environ["STANDIN_SECRET"])
        self.status = "idle"
        self.next_wake: datetime | None = None
        self.followed_up = 0
        self.answered = False
        self.asked: str | None = None  # the asked person's Slack id

    def user(self, email: str) -> str:
        return answered(self.slack.users_lookupByEmail(email=email))["user"]["id"]

    def dm(self, email: str, text: str, *, button: str | None = None) -> None:
        user = self.user(email)
        channel = answered(self.slack.conversations_open(users=[user]))["channel"]["id"]
        blocks = None
        if button is not None:
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "actions",
                    "elements": [
                        {"type": "button", "action_id": "decide", "text": {"type": "plain_text", "text": button}}
                    ],
                },
            ]
        self.slack.chat_postMessage(channel=channel, text=text, blocks=blocks)

    def wake(self, asked: dict[str, str]) -> None:
        now = datetime.fromisoformat(asked["now"])
        if asked["reason"] == "start":
            for url in FETCH:
                with contextlib.suppress(urllib.error.HTTPError):
                    urllib.request.urlopen(url, timeout=10).close()
            if ASK:
                self.asked = self.user(ASK)
                self.dm(ASK, QUESTION, button=BUTTON)
                self.next_wake = now + EVERY if FOLLOW_UPS else None
            if DONE_AT_START:
                self.status, self.next_wake = "done", None
        elif not self.answered and ASK and self.followed_up < FOLLOW_UPS:
            self.followed_up += 1
            self.dm(ASK, f"Following up ({self.followed_up}): {QUESTION}")
            self.next_wake = now + EVERY  # one more look after the last follow-up
        else:
            self.next_wake = None

    def answer(self, text: str) -> None:
        if self.answered:
            return
        self.answered = True
        if RELAY_TO:
            self.dm(RELAY_TO, f"Relaying: {text}")
        self.status, self.next_wake = "done", None

    def event(self, event: dict[str, str]) -> None:
        if event.get("type") == "message" and event.get("user") == self.asked and "bot_id" not in event:
            self.answer(event["text"])

    def interaction(self, payload: dict[str, object]) -> None:
        kind = payload["type"]
        if kind == "block_actions":
            if FORM:
                self.slack.views_open(
                    trigger_id=str(payload["trigger_id"]),
                    view={
                        "type": "modal",
                        "callback_id": "answer",
                        "title": {"type": "plain_text", "text": "Answer"},
                        "submit": {"type": "plain_text", "text": "Send"},
                        "blocks": [
                            {
                                "type": "input",
                                "block_id": "answer",
                                "label": {"type": "plain_text", "text": "Your answer"},
                                "element": {"type": "plain_text_input", "action_id": "text"},
                            }
                        ],
                    },
                )
            else:
                self.answer(f"pressed {BUTTON}")
        elif kind == "view_submission":
            view = payload["view"]
            assert isinstance(view, dict)
            values = view["state"]["values"]
            typed = [field["value"] for block in values.values() for field in block.values()]
            self.answer(f"typed {typed[0]}")


agent = Agent()
one_at_a_time = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        with one_at_a_time:
            if self.path == "/wake":
                agent.wake(json.loads(body))
                self.reply(200, {"ok": True})
            elif self.path == "/slack/events":
                if not agent.verifier.is_valid_request(body, dict(self.headers)):
                    self.reply(401, {"error": "invalid signature"})
                elif body.startswith(b"payload="):
                    self.reply(200, None)
                    agent.interaction(json.loads(urllib.parse.parse_qs(body.decode())["payload"][0]))
                else:
                    pushed = json.loads(body)
                    if pushed.get("type") == "url_verification":
                        self.reply(200, {"challenge": pushed["challenge"]})
                        return
                    agent.event(pushed["event"])
                    self.reply(200, {"ok": True})
            else:
                self.reply(404, {"error": "no such endpoint"})

    def do_GET(self) -> None:
        if self.path == "/report":
            with one_at_a_time:
                next_wake = agent.next_wake.isoformat() if agent.next_wake else None
                self.reply(200, {"status": agent.status, "next_wake": next_wake})
        else:
            self.reply(404, {"error": "no such endpoint"})

    def reply(self, status: int, payload: object) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # http.server looks this machine's name up by reverse DNS, which can take 30 seconds; skip it.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    Server(("127.0.0.1", int(os.environ["STANDIN_PORT"])), Handler).serve_forever()
