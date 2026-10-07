"""An OpenAI Agents SDK agent that asks a colleague in Slack, remembers when it expects an answer, and follows up once.

The agent is an `Agent` with three `function_tool`s and a typed run context, `Memory`, that lives as long as the
process. Each wake or Slack event is one `Runner.run` with the situation as its input and the same `Memory` as its
context. The tools write the waits into it (`remember_wait`, `close_wait`), and the report Minutehand asks for after
every wake reads it: `next_wake` is the earliest moment a wait is expected by.

    POST /wake          {"now": ..., "reason": "start" | "due" | ..., "goal": ...}: run the agent on what is due
    GET  /report        {"status": "idle" | "done", "next_wake": the earliest expected-by date, or null}
    POST /slack/events  Slack's Events API: a person's answer, run through the agent

It never reads the machine's clock: the time is the wake's `now`, or a Slack event's own timestamp.

    python agent.py

Environment:
    MODEL_BASE_URL              the chat-completions API (default the recipes' fake model, http://127.0.0.1:8790/v1)
    OPENAI_API_KEY              its key (default a made-up one, which the fake model accepts)
    AGENT_SLACK_SIGNING_SECRET  the secret Slack signs its events with; Minutehand makes one per run
    PORT                        where to listen (default 8712, the port agent.yaml names)
"""

from __future__ import annotations

import asyncio
import json
import os
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agents import Agent, OpenAIChatCompletionsModel, RunContextWrapper, Runner, function_tool, set_tracing_disabled
from openai import AsyncOpenAI
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

OWNER = "owen@example.com"  # who gives the agent its goal
ASK = "rosa@example.com"  # who knows the answer
SYSTEM = (
    "You carry one goal for its owner by asking a colleague in Slack. Whenever you ask, remember the wait with "
    "the date you expect an answer by. Follow up once; after that, tell the owner. When the answer comes, thank "
    "the colleague, tell the owner what they said, and close the wait."
)

slack = WebClient(token="xoxb-recipe-agent")


@dataclass
class Wait:
    expected_by: datetime
    asks: int


@dataclass
class Memory:
    """What the agent knows between runs: its goal and who owes it an answer by when."""

    goal: str | None = None
    waits: dict[str, Wait] = field(default_factory=dict)


@function_tool
def send_slack_message(email: str, text: str) -> str:
    """Send a direct message in Slack to the person with this email address."""
    user = slack.users_lookupByEmail(email=email)["user"]["id"]
    channel = slack.conversations_open(users=[user])["channel"]["id"]
    slack.chat_postMessage(channel=channel, text=text)
    return f"sent to {email}"


@function_tool
def remember_wait(memory: RunContextWrapper[Memory], email: str, expected_by: str) -> str:
    """Remember that `email` owes an answer, expected by `expected_by` (ISO 8601). Call it after every ask."""
    waits = memory.context.waits
    waits[email] = Wait(datetime.fromisoformat(expected_by), waits[email].asks + 1 if email in waits else 1)
    return f"waiting on {email} until {expected_by}"


@function_tool
def close_wait(memory: RunContextWrapper[Memory], email: str) -> str:
    """Nothing more is owed by `email`: they answered, or the owner has been told they did not."""
    memory.context.waits.pop(email, None)
    return f"closed {email}"


set_tracing_disabled(True)  # its traces go to OpenAI's own tracing API, not to Minutehand; see the README
agent = Agent[Memory](
    name="follow-up",
    instructions=SYSTEM,
    tools=[send_slack_message, remember_wait, close_wait],
    model=OpenAIChatCompletionsModel(
        model="gpt-4.1-mini",
        openai_client=AsyncOpenAI(
            base_url=os.environ.get("MODEL_BASE_URL", "http://127.0.0.1:8790/v1"),
            api_key=os.environ.get("OPENAI_API_KEY", "sk-recipe"),
        ),
    ),
)
memory = Memory()


def run(now: datetime, happened: str) -> None:
    assert memory.goal is not None
    situation = f"It is now {now.isoformat()}.\nGoal: {memory.goal}\nOwner: {OWNER}. Ask: {ASK}.\n{happened}"
    asyncio.run(Runner.run(agent, situation, context=memory))


# -- the Minutehand side: three endpoints ----------------------------------------------------------------------


def wake(request: dict[str, str]) -> None:
    now = datetime.fromisoformat(request["now"])
    if request["reason"] == "start":
        memory.goal = request["goal"]
        run(now, "Nobody has been asked yet.")
        return
    for email, wait in list(memory.waits.items()):
        if wait.expected_by <= now:
            expected = wait.expected_by.isoformat()
            run(now, f"No answer yet from {email}, expected by {expected}. Follow-ups sent: {wait.asks - 1}.")


def report() -> dict[str, object]:
    if memory.goal is None:
        return {"status": "idle", "next_wake": None}
    if not memory.waits:
        return {"status": "done", "next_wake": None}
    return {"status": "idle", "next_wake": min(w.expected_by for w in memory.waits.values()).isoformat()}


def message(event: dict[str, str]) -> None:
    """Someone wrote to the agent: an answer from a person it waits on goes through the agent."""
    email = slack.users_info(user=event["user"])["user"]["profile"]["email"]
    if email in memory.waits:
        run(datetime.fromtimestamp(float(event["ts"]), UTC), f"{email} answered: {event['text']}")


verifier = SignatureVerifier(os.environ["AGENT_SLACK_SIGNING_SECRET"])
one_at_a_time = threading.Lock()  # a wake and a Slack event can arrive together; take them in turn


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        with one_at_a_time:
            if self.path == "/wake":
                wake(json.loads(body))
                self.answer(200, {"ok": True})
            elif self.path == "/slack/events" and verifier.is_valid_request(body, dict(self.headers)):
                message(json.loads(body)["event"])
                self.answer(200, {"ok": True})
            else:
                self.answer(404, {"error": "no such endpoint, or a Slack event that is not signed"})

    def do_GET(self) -> None:
        with one_at_a_time:
            self.answer(200, report()) if self.path == "/report" else self.answer(404, {"error": "no such endpoint"})

    def answer(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Skip HTTPServer's reverse DNS lookup of this machine's name, which can take seconds and is never used.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8712"))
    print(f"listening on {port}", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
