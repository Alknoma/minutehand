"""An agent that asks a colleague a question in Slack and follows up when no answer comes.

This is an ordinary program. It knows Slack, through the stock `slack_sdk`, and it knows the wake
contract every agent under Minutehand answers to, and nothing else:

    POST /wake          {"now": ..., "reason": "start" | "due" | ..., "goal": ...}  "it is now `now`; go"
    GET  /report        -> {"status": ..., "next_wake": ... or null}   asked after every wake
                           "idle": this wake is finished; wake me again at next_wake, or never if null
                           "done": the goal is reached
                           ("working" means this wake is still going, and the question is asked again)
    POST /slack/events  Slack's Events API: a message someone sent the agent, signed by Slack

It never reads the machine's clock: the time is whatever the last wake said it is. Its outbound calls go
to slack.com as they would in production; Minutehand routes them to its fake Slack through the proxy
settings it puts in this process's environment.

    python agent.py

Environment:
    AGENT_BEHAVIOUR             "diligent" (the default) follows up once after two days without an answer;
                                "forgetful" asks once and never follows up
    AGENT_SLACK_SIGNING_SECRET  the secret Slack signs its events with; Minutehand makes one per run
    PORT                        where to listen (default 8700, the port agent.yaml names)

Everything it knows lives in this process and is gone when it exits, which is fine for one run.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

COLLEAGUE = "rosa@example.com"  # who knows the answer
OWNER = "owen@example.com"  # who gave the agent its goal
FOLLOW_UP_AFTER = timedelta(days=2)

QUESTION = "Hi Rosa, could you confirm the venue for the team offsite, please?"
FOLLOW_UP = "Hi Rosa, following up on the offsite venue: could you confirm it?"


class Agent:
    def __init__(self) -> None:
        self.follows_up = os.environ.get("AGENT_BEHAVIOUR", "diligent") != "forgetful"
        self.slack = WebClient(token="xoxb-example-agent")
        self.verifier = SignatureVerifier(os.environ["AGENT_SLACK_SIGNING_SECRET"])
        self.status = "idle"
        self.next_wake: datetime | None = None
        self.followed_up = False
        self.answer: str | None = None

    def send(self, email: str, text: str) -> None:
        """A direct message, the way any Slack app sends one."""
        user = self.slack.users_lookupByEmail(email=email)["user"]["id"]
        channel = self.slack.conversations_open(users=[user])["channel"]["id"]
        self.slack.chat_postMessage(channel=channel, text=text)

    def wake(self, request: dict) -> None:
        now = datetime.fromisoformat(request["now"])
        if request["reason"] == "start":
            print(f"goal: {request['goal']}", flush=True)
            self.send(COLLEAGUE, QUESTION)
            self.next_wake = now + FOLLOW_UP_AFTER if self.follows_up else None
        elif request["reason"] == "due" and self.answer is None:
            if not self.followed_up:
                self.send(COLLEAGUE, FOLLOW_UP)
                self.followed_up = True
                self.next_wake = now + FOLLOW_UP_AFTER
            else:
                self.next_wake = None

    def report(self) -> dict:
        return {"status": self.status, "next_wake": self.next_wake.isoformat() if self.next_wake else None}

    def message(self, event: dict) -> None:
        """Someone wrote to the agent. Rosa's answer finishes the job."""
        rosa = self.slack.users_lookupByEmail(email=COLLEAGUE)["user"]["id"]
        if event["user"] != rosa or self.answer is not None:
            return
        self.answer = event["text"]
        self.slack.chat_postMessage(channel=event["channel"], text="Thank you!")
        self.send(OWNER, f"The offsite venue is confirmed: {self.answer}")
        self.status = "done"
        self.next_wake = None


agent = Agent()
one_at_a_time = threading.Lock()  # a wake and a Slack event can arrive together; take them in turn


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        with one_at_a_time:
            self.handle_post(body)

    def handle_post(self, body: bytes) -> None:
        if self.path == "/wake":
            agent.wake(json.loads(body))
            self.answer(200, {"ok": True})
        elif self.path == "/slack/events":
            if not agent.verifier.is_valid_request(body, dict(self.headers)):
                self.answer(401, {"error": "invalid signature"})
                return
            agent.message(json.loads(body)["event"])
            self.answer(200, {"ok": True})
        else:
            self.answer(404, {"error": "no such endpoint"})

    def do_GET(self) -> None:
        if self.path == "/report":
            with one_at_a_time:
                self.answer(200, agent.report())
        else:
            self.answer(404, {"error": "no such endpoint"})

    def answer(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up this machine's name with a reverse DNS query, which can take
        # over 30 seconds on some machines. The name is never used, so skip it and listen at once.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8700"))
    server = Server(("127.0.0.1", port), Handler)
    print(f"listening on {port}, {'following up' if agent.follows_up else 'never following up'}", flush=True)
    server.serve_forever()
