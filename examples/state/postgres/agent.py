"""The follow-up agent of examples/state/sqlite, with everything it knows in a PostgreSQL database that Minutehand
fronts: DATABASE_URL points at Minutehand's relay, which records every write the agent commits, so the agent file
declares no state hooks at all.

It asks Rosa in Slack to confirm the offsite venue, says it is waiting on her (a commitment, expected in two days),
follows up once if she has not answered by then, and tells Owen when she does. On every wake it also keeps a note
of what it did (a row with a serial id, returned to it), and drafts a note it thinks better of: a transaction it
rolls back, which draws an id from the same sequence and records nothing.

    POST /wake          {"now": ..., "reason": ..., "goal": ...}
    GET  /report        {"status": ..., "next_wake": ..., "commitments": [...]}
    POST /slack/events  Slack's Events API, signed

    python agent.py

Environment:
    DATABASE_URL                where the agent's database is (Minutehand sets it to its relay)
    AGENT_SLACK_SIGNING_SECRET  the secret Slack signs its events with; Minutehand makes one per run
    PORT                        where to listen (default 8701, the port agent.yaml names)

Every moment it writes is the wake's `now`, sent as a parameter: the database's own clock (`now()`) is the
machine's, and a replay would write another moment.
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg.rows import dict_row
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

COLLEAGUE = "rosa@example.com"
OWNER = "owen@example.com"
FOLLOW_UP_AFTER = timedelta(days=2)
QUESTION = "Hi Rosa, could you confirm the venue for the team offsite, please?"
FOLLOW_UP = "Hi Rosa, following up on the offsite venue: could you confirm it?"
WAITING = "rosa_confirms_venue"
DRAFT = "a draft the agent thought better of"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS job(
         id integer PRIMARY KEY CHECK (id = 1),
         status text NOT NULL, next_wake timestamptz, followed_up boolean NOT NULL, answer text)""",
    "INSERT INTO job VALUES (1, 'idle', NULL, false, NULL) ON CONFLICT DO NOTHING",
    """CREATE TABLE IF NOT EXISTS commitment(
         key text PRIMARY KEY, description text NOT NULL, person_email text,
         opened_at timestamptz NOT NULL, expected_by timestamptz, status text NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS note(
         id serial PRIMARY KEY, at timestamptz NOT NULL, reason text NOT NULL, body text NOT NULL)""",
]


def database() -> psycopg.Connection:
    """A connection for one request. Nothing is held between requests, so nothing survives outside the database."""
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


class Agent:
    def __init__(self) -> None:
        self.slack = WebClient(token="xoxb-example-agent")
        self.verifier = SignatureVerifier(os.environ["AGENT_SLACK_SIGNING_SECRET"])
        with database() as db:
            for statement in SCHEMA:
                db.execute(statement)

    def send(self, email: str, text: str) -> None:
        user = self.slack.users_lookupByEmail(email=email)["user"]["id"]
        channel = self.slack.conversations_open(users=[user])["channel"]["id"]
        self.slack.chat_postMessage(channel=channel, text=text)

    def wake(self, request: dict) -> None:
        now = datetime.fromisoformat(request["now"])
        with database() as db:
            with db.transaction():  # drafted, thought better of, rolled back: its id stays drawn
                db.execute("INSERT INTO note(at, reason, body) VALUES (%s, %s, %s)", (now, request["reason"], DRAFT))
                raise psycopg.Rollback()
            job = db.execute("SELECT * FROM job").fetchone()
            assert job is not None
            if request["reason"] == "start":
                self.send(COLLEAGUE, QUESTION)
                due = now + FOLLOW_UP_AFTER
                db.execute("UPDATE job SET next_wake=%s", (due,))
                db.execute(
                    "INSERT INTO commitment VALUES (%s, %s, %s, %s, %s, 'open') "
                    "ON CONFLICT (key) DO UPDATE SET expected_by = excluded.expected_by, status = 'open'",
                    (WAITING, "Rosa confirms the offsite venue", COLLEAGUE, now, due),
                )
                said = "asked Rosa"
            elif request["reason"] == "due" and job["answer"] is None:
                if not job["followed_up"]:
                    self.send(COLLEAGUE, FOLLOW_UP)
                    due = now + FOLLOW_UP_AFTER
                    db.execute("UPDATE job SET followed_up=true, next_wake=%s", (due,))
                    db.execute("UPDATE commitment SET expected_by=%s WHERE key=%s", (due, WAITING))
                    said = "followed up with Rosa"
                else:
                    db.execute("UPDATE job SET next_wake=NULL")
                    said = "gave up on Rosa"
            else:
                said = "nothing to do"
            db.execute(
                "INSERT INTO note(at, reason, body) VALUES (%s, %s, %s) RETURNING id", (now, request["reason"], said)
            ).fetchone()

    def report(self) -> dict:
        with database() as db:
            job = db.execute("SELECT * FROM job").fetchone()
            assert job is not None
            commitments = [
                {
                    "key": c["key"],
                    "description": c["description"],
                    "waiting_on": "person",
                    "person_email": c["person_email"],
                    "opened_at": c["opened_at"].isoformat(),
                    "expected_by": c["expected_by"].isoformat() if c["expected_by"] else None,
                    "status": c["status"],
                }
                for c in db.execute("SELECT * FROM commitment ORDER BY key")
            ]
        next_wake = job["next_wake"].isoformat() if job["next_wake"] else None
        return {"status": job["status"], "next_wake": next_wake, "commitments": commitments}

    def message(self, event: dict) -> None:
        """Rosa's answer finishes the job."""
        rosa = self.slack.users_lookupByEmail(email=COLLEAGUE)["user"]["id"]
        with database() as db:
            job = db.execute("SELECT answer FROM job").fetchone()
            if event["user"] != rosa or job is None or job["answer"] is not None:
                return
            self.slack.chat_postMessage(channel=event["channel"], text="Thank you!")
            self.send(OWNER, f"The offsite venue is confirmed: {event['text']}")
            db.execute("UPDATE job SET answer=%s, status='done', next_wake=NULL", (event["text"],))
            db.execute("UPDATE commitment SET status='met' WHERE key=%s", (WAITING,))


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
    port = int(os.environ.get("PORT", "8701"))
    server = Server(("127.0.0.1", port), Handler)
    print(f"listening on {port}", flush=True)
    server.serve_forever(poll_interval=0.05)
