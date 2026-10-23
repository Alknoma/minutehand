"""A small proactive agent: it gets answers from people and passes them on.

Its work is its own, a file it reads (AGENT_WORK, `work.json`): for each ask, who answers, the question and who is
told. It asks in Slack, follows up at most twice, a working day apart, and tells the answer on, or that none came.
Nothing hands it this work. What it will say later is booked when it asks, as Slack scheduled messages
(`chat.scheduleMessage`): the two follow-ups, and the word to whoever asked that no answer came. Slack keeps them and
posts each at its moment; when an answer comes, the agent deletes the ones not yet posted and passes the answer on.
No thread of it waits on a deadline: its plan is records, Slack's and its own SQLite file's, which anyone can read
and change. It imports nothing of Minutehand's and has no endpoint for it; under Minutehand it runs unchanged in a
sandbox whose clock Minutehand owns, and each scheduled message is a booking the run delivers at its moment.

    POST /slack/events   someone wrote (Slack's Events API, signature checked): read, and acted on at once

    AGENT_SLACK_TOKEN=xoxb-... AGENT_SLACK_SIGNING_SECRET=... AGENT_PORT=8740 python agent.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

ANSWER_TAKES = timedelta(days=1)  # how long a person is given before a follow-up
FOLLOW_UPS = 2  # at most; then whoever asked to be told hears that there is no answer
OPENS, CLOSES = time(9, 0), time(17, 0)  # the agent writes to people in working hours, UTC
UNDELETABLE = timedelta(seconds=60)  # Slack refuses to delete a scheduled message this close to its moment


@dataclass(frozen=True)
class Ask:
    """One piece of the agent's own work: get an answer from someone, and pass it on."""

    ask: str  # who answers, by email
    question: str
    tell: str  # who is told the answer, or that none came, by email


def read_work(path: Path) -> list[Ask]:
    return [Ask(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


def in_hours(moment: datetime) -> datetime:
    """`moment`, or the next moment in working hours on a weekday when it is outside them."""
    moment = moment.astimezone(UTC)
    if moment.timetz().replace(tzinfo=None) >= CLOSES:
        moment = datetime.combine(moment.date() + timedelta(days=1), OPENS, tzinfo=UTC)
    elif moment.timetz().replace(tzinfo=None) < OPENS:
        moment = datetime.combine(moment.date(), OPENS, tzinfo=UTC)
    while moment.weekday() >= 5:
        moment = datetime.combine(moment.date() + timedelta(days=1), OPENS, tzinfo=UTC)
    return moment


class Memory:
    """What the agent knows between acts, in its own SQLite file: each ask's progress, and what people wrote."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS progress(n INTEGER PRIMARY KEY, state TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS inbox(sender TEXT NOT NULL, text TEXT NOT NULL)")

    def progress(self, n: int) -> dict[str, object]:
        row = self._db.execute("SELECT state FROM progress WHERE n=?", (n,)).fetchone()
        return json.loads(row[0]) if row else {}

    def keep(self, n: int, state: dict[str, object]) -> None:
        self._db.execute("INSERT OR REPLACE INTO progress VALUES(?,?)", (n, json.dumps(state)))
        self._db.commit()

    def heard(self, sender: str, text: str) -> None:
        self._db.execute("INSERT INTO inbox VALUES(?,?)", (sender, text))
        self._db.commit()

    def answer_from(self, sender: str) -> str | None:
        row = self._db.execute("SELECT text FROM inbox WHERE sender=? ORDER BY rowid LIMIT 1", (sender,)).fetchone()
        return row[0] if row else None


class Agent:
    """Asks, books what it will say later, and passes answers on. `act` does everything there is to do now: it
    asks whoever has not been asked, booking the follow-ups and the word that none came; it passes on an answer
    that has come, deleting what it booked and has not been posted yet."""

    def __init__(self, work: list[Ask], memory: Memory, slack: WebClient) -> None:
        self.work = work
        self.memory = memory
        self.slack = slack
        self._acting = threading.Lock()

    def _channel(self, email: str) -> str:
        user = str(self.slack.users_lookupByEmail(email=email).get("user", {}).get("id"))
        return str(self.slack.conversations_open(users=[user]).get("channel", {}).get("id"))

    def say(self, email: str, text: str) -> None:
        print(f"[agent] {datetime.now(UTC).isoformat()} to {email}: {text}", file=sys.stderr, flush=True)
        self.slack.chat_postMessage(channel=self._channel(email), text=text)

    def book(self, email: str, at: datetime, text: str) -> dict[str, object]:
        """Have Slack post `text` to `email` at `at`: what it keeps of the booking to delete it later."""
        print(f"[agent] booked for {at.isoformat()} to {email}: {text}", file=sys.stderr, flush=True)
        channel = self._channel(email)
        booked = self.slack.chat_scheduleMessage(channel=channel, post_at=int(at.timestamp()), text=text)
        return {"channel": channel, "id": str(booked.get("scheduled_message_id")), "at": at.isoformat()}

    def act(self, now: datetime) -> None:
        with self._acting:
            for n, ask in enumerate(self.work):
                done = self.memory.progress(n)
                if "asked_at" not in done:
                    done = self._ask(ask, now)
                    self.memory.keep(n, done)
                if "ended" in done:
                    continue
                answer = self.memory.answer_from(ask.ask)
                if answer is not None:
                    self._unbook(done, now)
                    self.say(ask.tell, f'{ask.ask} answered "{ask.question}": {answer}')
                    self.memory.keep(n, {**done, "ended": "answered"})

    def _ask(self, ask: Ask, now: datetime) -> dict[str, object]:
        """Ask, and book the rest: each follow-up a working day after the message before it, then, a working day
        after the last, the word to whoever asked that none came."""
        self.say(ask.ask, ask.question)
        booked: list[dict[str, object]] = []
        at = now
        for nth in range(1, FOLLOW_UPS + 1):
            at = in_hours(at + ANSWER_TAKES)
            text = f"Following up ({nth} of {FOLLOW_UPS}) on my question of {now.date().isoformat()}: {ask.question}"
            booked.append(self.book(ask.ask, at, text))
        at = in_hours(at + ANSWER_TAKES)
        none_came = f'No answer from {ask.ask} to "{ask.question}" after {FOLLOW_UPS} follow-ups.'
        booked.append(self.book(ask.tell, at, none_came))
        return {"asked_at": now.isoformat(), "booked": booked}

    def _unbook(self, done: dict[str, object], now: datetime) -> None:
        """Delete what was booked for this ask and has not been posted yet; Slack posts what is too close to delete."""
        booked = done["booked"]
        assert isinstance(booked, list)
        for one in booked:
            assert isinstance(one, dict)
            if datetime.fromisoformat(str(one["at"])) > now + UNDELETABLE:
                self.slack.chat_deleteScheduledMessage(channel=str(one["channel"]), scheduled_message_id=str(one["id"]))

    def heard(self, sender: str, text: str) -> None:
        print(f"[agent] {datetime.now(UTC).isoformat()} heard from {sender}: {text}", file=sys.stderr, flush=True)
        self.memory.heard(sender, text)
        self.act(datetime.now(UTC))


def serve(agent: Agent, port: int, secret: str) -> None:
    """Slack's Events API at /slack/events: a person's message is what the agent hears."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("content-length", "0")))
            if self.path != "/slack/events":
                return self._answer(404)
            if secret and not SignatureVerifier(secret).is_valid_request(raw, dict(self.headers)):
                return self._answer(401)
            body = json.loads(raw)
            if body.get("type") == "url_verification":
                return self._answer(200, json.dumps({"challenge": body.get("challenge")}).encode())
            event = body.get("event", {})
            if event.get("type") == "message" and not event.get("bot_id") and event.get("user"):
                profile = agent.slack.users_info(user=str(event["user"])).get("user", {}).get("profile", {})
                agent.heard(str(profile.get("email", "")), str(event.get("text", "")))
            return self._answer(200)

        def _answer(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    work = read_work(Path(os.environ.get("AGENT_WORK", "work.json")))
    memory = Memory(Path(os.environ.get("AGENT_DB", "proactive_agent.db")))
    agent = Agent(work, memory, WebClient(token=os.environ.get("AGENT_SLACK_TOKEN", "xoxb-proactive-agent")))
    agent.act(datetime.now(UTC))  # ask whoever has not been asked, and book what follows
    serve(agent, int(os.environ.get("AGENT_PORT", "8740")), os.environ.get("AGENT_SLACK_SIGNING_SECRET", ""))
