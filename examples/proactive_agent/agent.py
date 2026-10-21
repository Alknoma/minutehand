"""A small proactive agent: it gets answers from people and passes them on.

Its work is its own, a file it reads (AGENT_WORK, `work.json`): for each ask, who answers, the question and who is
told. It asks in Slack, follows up at most twice, a working day apart, and tells the answer on, or that none came.
Nothing hands it this work and nothing outside it wakes it: its own scheduler (one thread waiting on a deadline)
decides when it next acts, and its state is its own SQLite file. It imports nothing of Minutehand's and has no
endpoint for it; under Minutehand it runs unchanged in a sandbox whose clock Minutehand owns, so its sleeps follow
simulated time and its next deadline is read from the sandbox as its next wake.

    POST /slack/events   someone wrote (Slack's Events API, signature checked): read, and acted on at once

    AGENT_SLACK_TOKEN=xoxb-... AGENT_SLACK_SIGNING_SECRET=... AGENT_PORT=8740 python agent.py
"""

from __future__ import annotations

import json
import os
import sqlite3
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
    """Asks, follows up, tells. `act` does everything that is due now and answers when it next has something to do;
    the scheduler thread sleeps until then, and a Slack event wakes it early."""

    def __init__(self, work: list[Ask], memory: Memory, slack: WebClient) -> None:
        self.work = work
        self.memory = memory
        self.slack = slack
        self._changed = threading.Condition()

    def say(self, email: str, text: str) -> None:
        user = str(self.slack.users_lookupByEmail(email=email).get("user", {}).get("id"))
        channel = str(self.slack.conversations_open(users=[user]).get("channel", {}).get("id"))
        self.slack.chat_postMessage(channel=channel, text=text)

    def act(self, now: datetime) -> datetime | None:
        """Do what is due at `now`; answer the earliest moment anything is due next, or None when nothing is open."""
        due_next: list[datetime] = []
        for n, ask in enumerate(self.work):
            done = self.memory.progress(n)
            if "asked_at" not in done:
                self.say(ask.ask, ask.question)
                done = {"asked_at": now.isoformat(), "last_contact": now.isoformat(), "follow_ups": 0}
                self.memory.keep(n, done)
            if "ended" in done:
                continue
            answer = self.memory.answer_from(ask.ask)
            follow_ups = int(str(done["follow_ups"]))
            due = in_hours(datetime.fromisoformat(str(done["last_contact"])) + ANSWER_TAKES)
            if answer is not None:
                self.say(ask.tell, f'{ask.ask} answered "{ask.question}": {answer}')
                self.memory.keep(n, {**done, "ended": "answered"})
            elif now >= due and follow_ups < FOLLOW_UPS:
                nth = follow_ups + 1  # each follow-up says which it is: none repeats the one before
                asked_on = str(done["asked_at"])[:10]
                self.say(ask.ask, f"Following up ({nth} of {FOLLOW_UPS}) on my question of {asked_on}: {ask.question}")
                self.memory.keep(n, {**done, "last_contact": now.isoformat(), "follow_ups": nth})
                due_next.append(in_hours(now + ANSWER_TAKES))
            elif now >= due:
                self.say(ask.tell, f'No answer from {ask.ask} to "{ask.question}" after {FOLLOW_UPS} follow-ups.')
                self.memory.keep(n, {**done, "ended": "no answer"})
            else:
                due_next.append(due)
        return min(due_next) if due_next else None

    def heard(self, sender: str, text: str) -> None:
        self.memory.heard(sender, text)
        with self._changed:
            self._changed.notify()

    def run(self) -> None:
        """The agent's own scheduler: act, sleep until the next moment anything is due (or until someone writes),
        act again. The sleep is a wait on a deadline, which is what the sandbox reports as its next wake."""
        while True:
            next_due = self.act(datetime.now(UTC))
            if next_due is None:
                with self._changed:
                    self._changed.wait()
                continue
            with self._changed:
                self._changed.wait(timeout=max(0.0, (next_due - datetime.now(UTC)).total_seconds()))


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
    port = int(os.environ.get("AGENT_PORT", "8740"))
    threading.Thread(
        target=serve, args=(agent, port, os.environ.get("AGENT_SLACK_SIGNING_SECRET", "")), daemon=True
    ).start()
    agent.run()
