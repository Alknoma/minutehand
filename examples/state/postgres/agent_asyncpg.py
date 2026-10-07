"""agent.py, written against asyncpg instead of psycopg: the same follow-up agent, the same database, the same
endpoints, so the same agent file fronts it (`minutehand run ... -- python agent_asyncpg.py`).

asyncpg looks different on the wire, and Minutehand records and replays it with no change: it keeps a pool of
connections whose statements it prepares once, by name (`__asyncpg_stmt_N__`), and reuses across transactions; it
describes each statement before binding it; it sends every parameter and asks for every result in binary; it asks
for one row for `fetchval`, so the database answers its `INSERT ... RETURNING` with no tag (PortalSuspended); and it
sets the session's time zone as a startup parameter, never by `SET`.

    python agent_asyncpg.py

Environment: as agent.py's (DATABASE_URL, AGENT_SLACK_SIGNING_SECRET, PORT).
"""

from __future__ import annotations

import asyncio
import json
import os
import socketserver
import threading
from collections.abc import Coroutine
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import asyncpg
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


class ThoughtBetter(Exception):
    """Raised inside a transaction to roll it back."""


class Agent:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.slack = WebClient(token="xoxb-example-agent")
        self.verifier = SignatureVerifier(os.environ["AGENT_SLACK_SIGNING_SECRET"])
        self.pool: asyncpg.Pool = self.run(self.connect())
        self.run(self.migrate())

    def run[T](self, work: Coroutine[object, object, T]) -> T:
        """Run `work` on the agent's event loop, from a request's thread, and wait for it."""
        return asyncio.run_coroutine_threadsafe(work, self.loop).result()

    async def connect(self) -> asyncpg.Pool:
        return await asyncpg.create_pool(
            os.environ["DATABASE_URL"], min_size=1, max_size=2, server_settings={"timezone": "Europe/Lisbon"}
        )

    async def migrate(self) -> None:
        async with self.pool.acquire() as db:
            for statement in SCHEMA:
                await db.execute(statement)

    def send(self, email: str, text: str) -> None:
        user = self.slack.users_lookupByEmail(email=email)["user"]["id"]
        channel = self.slack.conversations_open(users=[user])["channel"]["id"]
        self.slack.chat_postMessage(channel=channel, text=text)

    async def wake(self, request: dict) -> None:
        now = datetime.fromisoformat(request["now"])
        async with self.pool.acquire() as db:
            try:
                async with db.transaction():  # drafted, thought better of, rolled back: its id stays drawn
                    await db.execute(
                        "INSERT INTO note(at, reason, body) VALUES ($1, $2, $3)", now, request["reason"], DRAFT
                    )
                    raise ThoughtBetter
            except ThoughtBetter:
                pass
            job = await db.fetchrow("SELECT * FROM job")
            assert job is not None
            if request["reason"] == "start":
                await asyncio.to_thread(self.send, COLLEAGUE, QUESTION)
                due = now + FOLLOW_UP_AFTER
                async with db.transaction():
                    await db.execute("UPDATE job SET next_wake=$1", due)
                    await db.execute(
                        "INSERT INTO commitment VALUES ($1, $2, $3, $4, $5, 'open') "
                        "ON CONFLICT (key) DO UPDATE SET expected_by = excluded.expected_by, status = 'open'",
                        WAITING,
                        "Rosa confirms the offsite venue",
                        COLLEAGUE,
                        now,
                        due,
                    )
                said = "asked Rosa"
            elif request["reason"] == "due" and job["answer"] is None:
                if not job["followed_up"]:
                    await asyncio.to_thread(self.send, COLLEAGUE, FOLLOW_UP)
                    due = now + FOLLOW_UP_AFTER
                    async with db.transaction():
                        await db.execute("UPDATE job SET followed_up=true, next_wake=$1", due)
                        await db.execute("UPDATE commitment SET expected_by=$1 WHERE key=$2", due, WAITING)
                    said = "followed up with Rosa"
                else:
                    await db.execute("UPDATE job SET next_wake=NULL")
                    said = "gave up on Rosa"
            else:
                said = "nothing to do"
            await db.fetchval(
                "INSERT INTO note(at, reason, body) VALUES ($1, $2, $3) RETURNING id", now, request["reason"], said
            )

    async def report(self) -> dict:
        async with self.pool.acquire() as db:
            job = await db.fetchrow("SELECT * FROM job")
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
                for c in await db.fetch("SELECT * FROM commitment ORDER BY key")
            ]
        next_wake = job["next_wake"].isoformat() if job["next_wake"] else None
        return {"status": job["status"], "next_wake": next_wake, "commitments": commitments}

    async def message(self, event: dict) -> None:
        """Rosa's answer finishes the job."""
        rosa = (await asyncio.to_thread(self.slack.users_lookupByEmail, email=COLLEAGUE))["user"]["id"]
        async with self.pool.acquire() as db:
            answer = await db.fetchval("SELECT answer FROM job")
            if event["user"] != rosa or answer is not None:
                return
            await asyncio.to_thread(self.slack.chat_postMessage, channel=event["channel"], text="Thank you!")
            await asyncio.to_thread(self.send, OWNER, f"The offsite venue is confirmed: {event['text']}")
            async with db.transaction():
                await db.execute("UPDATE job SET answer=$1, status='done', next_wake=NULL", event["text"])
                await db.execute("UPDATE commitment SET status='met' WHERE key=$1", WAITING)


loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
agent = Agent(loop)
one_at_a_time = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        with one_at_a_time:
            if self.path == "/wake":
                agent.run(agent.wake(json.loads(body)))
                self.answer(200, {"ok": True})
            elif self.path == "/slack/events":
                if not agent.verifier.is_valid_request(body, dict(self.headers)):
                    self.answer(401, {"error": "invalid signature"})
                    return
                agent.run(agent.message(json.loads(body)["event"]))
                self.answer(200, {"ok": True})
            else:
                self.answer(404, {"error": "no such endpoint"})

    def do_GET(self) -> None:
        if self.path == "/report":
            with one_at_a_time:
                self.answer(200, agent.run(agent.report()))
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
        # As agent.py: skip HTTPServer's reverse DNS lookup of this machine's name, which can take 30 seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8701"))
    server = Server(("127.0.0.1", port), Handler)
    print(f"listening on {port}", flush=True)
    server.serve_forever(poll_interval=0.05)
