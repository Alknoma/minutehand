"""What the standing-mode tests share: a server in this process, clients configured the way a service is,
and an endpoint standing in for a service's Slack event receiver."""

from __future__ import annotations

import json
import socketserver
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import yaml
from slack_sdk import WebClient
from slack_sdk.web import SlackResponse

from minutehand.adapters.control.wire import Claims, CreateWorld, Inbound
from minutehand.domain.scenario import Seed
from minutehand.testing.client import MinutehandClient

SECRET = "signing-secret-of-the-service"


@dataclass(frozen=True)
class Served:
    url: str
    client: MinutehandClient
    environment: dict[str, str]

    @property
    def proxy(self) -> str:
        return self.environment["HTTPS_PROXY"]

    @property
    def bundle(self) -> str:
        return self.environment["SSL_CERT_FILE"]

    def slack(self, token: str) -> WebClient:
        """slack_sdk as a service runs it: the proxy and the CA from the environment Minutehand hands out."""
        return WebClient(token=token, proxy=self.proxy, ssl=ssl.create_default_context(cafile=self.bundle))


def answer(response: SlackResponse) -> dict[str, Any]:
    """A Slack answer's JSON, which slack_sdk types as possibly not a dict."""
    found = response.data
    assert isinstance(found, dict)
    return found


def dm(served: Served, token: str, email: str) -> str:
    """The agent's DM with the person, opened through the API as a service would."""
    slack = served.slack(token)
    user = answer(slack.users_lookupByEmail(email=email))["user"]["id"]
    return answer(slack.conversations_open(users=[user]))["channel"]["id"]


ASKS_SOFIA = "[{id: asks_sofia, count: {messages: {to: [sofia]}}, at_least: 1}]"
"""A team's rule a test world is judged by, so its verdict is about how the agent finished, not "not assessed"."""


def seed(*people: tuple[str, str], scripted: dict[str, str] | None = None, assess: str | None = None) -> Seed:
    """People by key and name; `scripted` gives a person one scripted reply to their first ask, an hour later;
    `assess`, the team's rules in YAML."""
    replies = scripted or {}
    return Seed.model_validate(
        {
            "starts_at": "2026-09-01T09:00:00Z",
            "assess": yaml.safe_load(assess) if assess is not None else [],
            "people": [
                {
                    "key": key,
                    "name": name,
                    "email": f"{key}@example.com",
                    "reply": (
                        {
                            "kind": "scripted",
                            "delay": {"shortest": "PT1H", "longest": "PT1H"},
                            "replies": [{"to_ask": 1, "text": replies[key]}],
                        }
                        if key in replies
                        else {"kind": "silent"}
                    ),
                }
                for key, name in people
            ],
        }
    )


def spec(
    token: str, *, inbound: str | None = None, scripted: dict[str, str] | None = None, assess: str | None = None
) -> CreateWorld:
    return CreateWorld(
        seed=seed(("owen", "Owen Owner"), ("sofia", "Sofia Romano"), scripted=scripted, assess=assess),
        claims=Claims(tokens=[token]),
        inbound=[Inbound(provider="slack", url=inbound, secret=SECRET)] if inbound is not None else [],
        scripted_people=scripted is not None,
    )


@dataclass
class Pushed:
    body: bytes
    timestamp: str
    signature: str


@dataclass
class Receiver:
    url: str
    pushed: list[Pushed] = field(default_factory=list)

    def texts(self) -> list[str]:
        return [json.loads(p.body)["event"]["text"] for p in self.pushed]


class _Loopback(ThreadingHTTPServer):
    def server_bind(self) -> None:
        """Skip the reverse DNS lookup of this machine's name `HTTPServer.server_bind` makes, slow on macOS."""
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


@contextmanager
def event_receiver(*, answers_after: threading.Event | None = None) -> Iterator[Receiver]:
    """A service's Slack events endpoint on loopback, keeping each push with its signature headers. With
    `answers_after`, it answers a push only once that event is set: a service still working on what it was sent."""
    found: list[Pushed] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            found.append(Pushed(body, self.headers["X-Slack-Request-Timestamp"], self.headers["X-Slack-Signature"]))
            if answers_after is not None:
                answers_after.wait(30)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = _Loopback(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Receiver(url=f"http://127.0.0.1:{server.server_address[1]}/slack/events", pushed=found)
    finally:
        server.shutdown()
        server.server_close()
