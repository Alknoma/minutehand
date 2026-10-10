"""A small proactive agent: it gets answers from people and passes them on.

Its work is its own, a file it reads (AGENT_WORK, `work.json`): for each ask, who answers, the question, and who is
told. It asks in Slack, follows up at most twice, a working day apart, and tells the answer on, or that none came. The
run hands it nothing, and it is proactive: it decides when it next wakes. That one decision is the `wake` package: a
Pydantic AI sub-agent proposes the moment, and plain code makes it safe. Everything else here is plain code.

    POST /wake          {"now": ..., "reason": ...}: it is now `now`; do what is due
    GET  /report        {"status": "idle", "next_wake": the wake package's moment, or null}
    POST /slack/events  someone wrote: recorded, and read on the wake their message brings

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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from wake import Open, next_wake
from wake.guard import in_hours

from minutehand.agent import store

ANSWER_TAKES = timedelta(days=1)  # how long a person is given before a follow-up
FOLLOW_UPS = 2  # at most; then whoever asked to be told hears that there is no answer


class Ask(BaseModel):
    """One piece of the agent's own work: get an answer from someone, and pass it on."""

    ask: str  # who answers, by email
    question: str
    tell: str  # who is told the answer, or that none came, by email


ASKS = TypeAdapter(list[Ask]).validate_json(Path(os.environ.get("AGENT_WORK", "work.json")).read_bytes())

PORT = int(os.environ.get("AGENT_PORT") or os.environ.get("MINUTEHAND_RUN_PORT") or "8740")
SECRET = os.environ.get("AGENT_SLACK_SIGNING_SECRET", "")
slack = WebClient(token=os.environ.get("AGENT_SLACK_TOKEN", "xoxb-proactive-agent"))
lock = threading.Lock()
last_wake: list[datetime] = []


def remembered() -> dict[str, Any]:
    """Each ask's progress, by its place in the work: when it was asked, last followed up, how often, how it ended."""
    found = store.get("work")
    return found if isinstance(found, dict) else {}


def say(email: str, text: str) -> None:
    user = str(slack.users_lookupByEmail(email=email).get("user", {}).get("id"))
    channel = str(slack.conversations_open(users=[user]).get("channel", {}).get("id"))
    slack.chat_postMessage(channel=channel, text=text)


def open_items(work: dict[str, Any]) -> list[Open]:
    """What it is waiting on: each answer asked for and not yet given, until the asker is told none came."""
    found = []
    for n, ask in enumerate(ASKS):
        done = work.get("asks", {}).get(str(n), {})
        if "asked_at" in done and "ended" not in done:
            due = in_hours(datetime.fromisoformat(done["last_contact"]) + ANSWER_TAKES)
            since = datetime.fromisoformat(done["asked_at"])
            found.append(Open(what=f"{ask.ask}'s answer to: {ask.question}", since=since, due=due))
    return found


def wake(now: datetime) -> None:
    with lock:
        last_wake.append(now)
        work = remembered()
        progress: dict[str, dict[str, Any]] = work.setdefault("asks", {})
        inbox = work.pop("inbox", None) or []  # what people wrote since the last wake
        for n, ask in enumerate(ASKS):
            done = progress.setdefault(str(n), {})
            if "asked_at" not in done:
                say(ask.ask, ask.question)
                done |= {"asked_at": now.isoformat(), "last_contact": now.isoformat(), "follow_ups": 0}
                continue
            if "ended" in done:
                continue
            answer = next((said["text"] for said in inbox if said["from"] == ask.ask), None)
            if answer is not None:
                say(ask.tell, f'{ask.ask} answered "{ask.question}": {answer}')
                done["ended"] = "answered"
            elif now >= in_hours(datetime.fromisoformat(done["last_contact"]) + ANSWER_TAKES):
                if done["follow_ups"] < FOLLOW_UPS:
                    nth = done["follow_ups"] + 1  # each follow-up says which it is: none repeats the one before
                    say(
                        ask.ask,
                        f"Following up ({nth} of {FOLLOW_UPS}) on my question of {done['asked_at'][:10]}: {ask.question}",
                    )
                    done |= {"last_contact": now.isoformat(), "follow_ups": done["follow_ups"] + 1}
                else:
                    say(ask.tell, f'No answer from {ask.ask} to "{ask.question}" after {FOLLOW_UPS} follow-ups.')
                    done["ended"] = "no answer"
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
