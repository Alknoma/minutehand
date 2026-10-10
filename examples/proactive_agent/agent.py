"""A small proactive agent: it needs the cost centre for a purchase from Sam, and tells Owen.

It asks Sam in Slack, follows up at most twice, a working day apart, and tells Owen the answer, or that Sam has not
answered. Its work is its own (the run hands it none), and it is proactive: it decides when it next wakes. That one
decision is the `wake` package: a Pydantic AI sub-agent proposes the moment, and plain code makes it safe. Everything
else here is plain code.

    POST /wake          {"now": ..., "reason": ...}: it is now `now`; do what is due
    GET  /report        {"status": "idle", "next_wake": the wake package's moment, or null}
    POST /slack/events  Sam wrote: recorded, and read on the wake his reply brings

Its memory is `minutehand.agent.store`: a SQLite file in production, the run's own memory under a run.

    AGENT_MODEL=openai:gpt-6-luna OPENAI_API_KEY=... AGENT_PORT=8740 python agent.py
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from wake import Open, next_wake
from wake.guard import in_hours

from minutehand.agent import store

SAM, OWEN = "sam@example.com", "owen@example.com"
PO = "PO-7731 (40 laptops for the new starters)"
ANSWER_TAKES = timedelta(days=1)  # how long Sam is given before a follow-up
FOLLOW_UPS = 2  # at most; then Owen is told

PORT = int(os.environ.get("AGENT_PORT") or os.environ.get("MINUTEHAND_RUN_PORT") or "8740")
SECRET = os.environ.get("AGENT_SLACK_SIGNING_SECRET", "")
slack = WebClient(token=os.environ.get("AGENT_SLACK_TOKEN", "xoxb-proactive-agent"))
lock = threading.Lock()
last_wake: list[datetime] = []


def remembered() -> dict[str, Any]:
    found = store.get("work")
    return found if isinstance(found, dict) else {}


def say(email: str, text: str) -> None:
    user = str(slack.users_lookupByEmail(email=email).get("user", {}).get("id"))
    channel = str(slack.conversations_open(users=[user]).get("channel", {}).get("id"))
    slack.chat_postMessage(channel=channel, text=text)


def open_items(work: dict[str, Any]) -> list[Open]:
    """What it is waiting on: Sam's answer, until he gives it or Owen is told he has not."""
    if "asked_at" not in work or "answer" in work or work.get("told") == "stuck":
        return []
    last = datetime.fromisoformat(str(work.get("last_contact", work["asked_at"])))
    due = in_hours(last + ANSWER_TAKES)
    return [
        Open(
            what=f"Sam's answer: the cost centre for {PO}", since=datetime.fromisoformat(str(work["asked_at"])), due=due
        )
    ]


def wake(now: datetime) -> None:
    with lock:
        last_wake.append(now)
        work = remembered()
        if "asked_at" not in work:
            say(SAM, f"Hi Sam, could you tell me the cost centre for {PO}?")
            work |= {"asked_at": now.isoformat(), "last_contact": now.isoformat(), "follow_ups": 0}
        for said in work.pop("inbox", []) or []:  # what Sam wrote since the last wake
            if said["from"] == SAM and "answer" not in work:
                work["answer"] = said["text"]
                say(OWEN, f"Sam answered about {PO}: {said['text']}")
                work["told"] = "answer"
        due = open_items(work)
        if due and now >= due[0].due:
            follow_ups = int(work.get("follow_ups", 0))
            if follow_ups < FOLLOW_UPS:
                asked = str(work["asked_at"])[:10]
                say(SAM, f"Following up ({follow_ups + 1}) on my question of {asked}: the cost centre for {PO}?")
                work |= {"last_contact": now.isoformat(), "follow_ups": follow_ups + 1}
            else:
                say(OWEN, f"Sam has not answered about the cost centre for {PO}, after {FOLLOW_UPS} follow-ups.")
                work["told"] = "stuck"
        store.put("work", work)


def report() -> dict[str, object]:
    with lock:
        now = last_wake[-1] if last_wake else datetime.now().astimezone()
        moment = next_wake(open_items(remembered()), now)
    return {"status": "idle", "next_wake": moment.isoformat() if moment else None}


def heard(event: dict[str, object]) -> None:
    if event.get("type") != "message" or event.get("bot_id") or not event.get("user"):
        return
    email = slack.users_info(user=str(event["user"])).get("user", {}).get("profile", {}).get("email")
    with lock:
        work = remembered()
        work["inbox"] = [*(work.get("inbox") or []), {"from": email, "text": str(event.get("text", ""))}]
        store.put("work", work)


class Handler(BaseHTTPRequestHandler):
    def _answer(self, status: int, body: object = None) -> None:
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._answer(200, report()) if self.path == "/report" else self._answer(404)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length", "0")))
        if self.path == "/wake":
            wake(datetime.fromisoformat(json.loads(raw)["now"]))
            self._answer(200, {})
        elif self.path == "/slack/events":
            if SECRET and not SignatureVerifier(SECRET).is_valid_request(raw, dict(self.headers)):
                self._answer(401)
                return
            body = json.loads(raw)
            if body.get("type") == "url_verification":
                self._answer(200, {"challenge": body.get("challenge")})
                return
            heard(body.get("event", {}))
            self._answer(200, {})
        else:
            self._answer(404)

    def log_message(self, format: str, *args: object) -> None:
        pass


class Server(ThreadingHTTPServer):
    """Bound without the reverse-DNS lookup of its own address that `HTTPServer.server_bind` makes."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    store.configure(store.SqliteBackend(os.environ.get("AGENT_DB", "proactive_agent.db")))
    Server(("127.0.0.1", PORT), Handler).serve_forever()
