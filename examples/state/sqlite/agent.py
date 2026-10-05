"""The follow-up agent of examples/follow_up, with everything it knows in a SQLite file instead of in memory.

It asks Rosa in Slack to confirm the offsite venue, says it is waiting on her (a commitment, expected in two
days), follows up once if she has not answered by then, and tells Owen when she does. Its report is read
from the database on every request, so a restore of the file is a restore of the agent:

    POST /wake          {"now": ..., "reason": ..., "goal": ...}
    GET  /report        {"status": ..., "next_wake": ..., "commitments": [...]}
    POST /slack/events  Slack's Events API, signed

    python agent.py

Environment:
    AGENT_DB                    the SQLite file (default agent.db)
    AGENT_SLACK_SIGNING_SECRET  the secret Slack signs its events with; Minutehand makes one per run
    PORT                        where to listen (default 8700, the port agent.yaml names)
"""

from __future__ import annotations

import json
import os
import socketserver
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

COLLEAGUE = "rosa@example.com"
OWNER = "owen@example.com"
FOLLOW_UP_AFTER = timedelta(days=2)
QUESTION = "Hi Rosa, could you confirm the venue for the team offsite, please?"
FOLLOW_UP = "Hi Rosa, following up on the offsite venue: could you confirm it?"
WAITING = "rosa_confirms_venue"

SCHEMA = """
CREATE TABLE IF NOT EXISTS job(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  status TEXT NOT NULL, next_wake TEXT, followed_up INTEGER NOT NULL, answer TEXT);
INSERT OR IGNORE INTO job VALUES (1, 'idle', NULL, 0, NULL);
CREATE TABLE IF NOT EXISTS commitment(
  key TEXT PRIMARY KEY, description TEXT NOT NULL, person_email TEXT,
  opened_at TEXT NOT NULL, expected_by TEXT, status TEXT NOT NULL);
"""


def database() -> sqlite3.Connection:
    """A connection for one request. Nothing is held between requests, so nothing survives outside the file."""
    db = sqlite3.connect(os.environ.get("AGENT_DB", "agent.db"))
    db.row_factory = sqlite3.Row
    return db


class Agent:
    def __init__(self) -> None:
        self.slack = WebClient(token="xoxb-example-agent")
        self.verifier = SignatureVerifier(os.environ["AGENT_SLACK_SIGNING_SECRET"])
        with closing(database()) as db:
            db.executescript(SCHEMA)
            db.commit()

    def send(self, email: str, text: str) -> None:
        user = self.slack.users_lookupByEmail(email=email)["user"]["id"]
        channel = self.slack.conversations_open(users=[user])["channel"]["id"]
        self.slack.chat_postMessage(channel=channel, text=text)

    def wake(self, request: dict) -> None:
        now = datetime.fromisoformat(request["now"])
        with closing(database()) as db:
            job = db.execute("SELECT * FROM job").fetchone()
            if request["reason"] == "start":
                self.send(COLLEAGUE, QUESTION)
                due = (now + FOLLOW_UP_AFTER).isoformat()
                db.execute("UPDATE job SET next_wake=?", (due,))
                db.execute(
                    "INSERT OR REPLACE INTO commitment VALUES (?, ?, ?, ?, ?, 'open')",
                    (WAITING, "Rosa confirms the offsite venue", COLLEAGUE, now.isoformat(), due),
                )
            elif request["reason"] == "due" and job["answer"] is None:
                if not job["followed_up"]:
                    self.send(COLLEAGUE, FOLLOW_UP)
                    due = (now + FOLLOW_UP_AFTER).isoformat()
                    db.execute("UPDATE job SET followed_up=1, next_wake=?", (due,))
                    db.execute("UPDATE commitment SET expected_by=? WHERE key=?", (due, WAITING))
                else:
                    db.execute("UPDATE job SET next_wake=NULL")
            db.commit()

    def report(self) -> dict:
        with closing(database()) as db:
            job = db.execute("SELECT * FROM job").fetchone()
            commitments = [
                {
                    "key": c["key"],
                    "description": c["description"],
                    "waiting_on": "person",
                    "person_email": c["person_email"],
                    "opened_at": c["opened_at"],
                    "expected_by": c["expected_by"],
                    "status": c["status"],
                }
                for c in db.execute("SELECT * FROM commitment ORDER BY key")
            ]
        return {"status": job["status"], "next_wake": job["next_wake"], "commitments": commitments}

    def message(self, event: dict) -> None:
        """Rosa's answer finishes the job."""
        rosa = self.slack.users_lookupByEmail(email=COLLEAGUE)["user"]["id"]
        with closing(database()) as db:
            if event["user"] != rosa or db.execute("SELECT answer FROM job").fetchone()["answer"] is not None:
                return
            self.slack.chat_postMessage(channel=event["channel"], text="Thank you!")
            self.send(OWNER, f"The offsite venue is confirmed: {event['text']}")
            db.execute("UPDATE job SET answer=?, status='done', next_wake=NULL", (event["text"],))
            db.execute("UPDATE commitment SET status='met' WHERE key=?", (WAITING,))
            db.commit()


agent = Agent()
one_at_a_time = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        with one_at_a_time:
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

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, flush=True)


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up this machine's name with a reverse DNS query, which can take
        # over 30 seconds on some machines. The name is never used, so skip it and listen at once.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8700"))
    server = Server(("127.0.0.1", port), Handler)
    print(f"listening on {port}, state in {os.environ.get('AGENT_DB', 'agent.db')}", flush=True)
    server.serve_forever(poll_interval=0.05)
