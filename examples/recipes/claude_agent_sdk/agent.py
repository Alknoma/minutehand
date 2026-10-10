"""A Claude Agent SDK agent that asks a colleague in Slack, remembers when it expects an answer, and follows up once.

It is proactive: it decides when it next wakes. Nothing wakes it on a schedule; its report names the moment it wants waking next (`next_wake`), or none.

The agent is a `query()` per wake or Slack event, with the situation as its prompt and three tools served in this
process by `create_sdk_mcp_server`. Claude Code's own tools are switched off (`tools=[]`); only these three are
allowed. The tools close over `Memory`, recalled from `minutehand.agent.store` on every wake, event and report and
kept back after, and write the waits into it (`remember_wait`, `close_wait`); the report Minutehand asks for after every wake reads it: `next_wake` is the
earliest moment a wait is expected by.

The SDK runs the Claude Code command line it bundles, which calls the messages API. ANTHROPIC_BASE_URL points it at
the recipes' fake model; Slack calls are made by the tools, in this process, so they go through Minutehand's proxy.

    POST /wake          {"now": ..., "reason": "start" | "due" | ...}: run the agent on what is due
    GET  /report        {"status": "idle" | "done", "next_wake": the earliest expected-by date, or null}
    POST /slack/events  Slack's Events API: a person's answer, run through the agent

It never reads the machine's clock: the time is the wake's `now`, or a Slack event's own timestamp.

    python agent.py

Environment:
    ANTHROPIC_BASE_URL          the messages API (default the recipes' fake model, http://127.0.0.1:8790)
    ANTHROPIC_API_KEY           its key (default a made-up one, which the fake model accepts)
    AGENT_SLACK_SIGNING_SECRET  the secret Slack signs its events with; Minutehand makes one per run
    PORT                        where to listen (default 8713, the port agent.yaml names)
"""

from __future__ import annotations

import asyncio
import json
import os
import socketserver
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

from minutehand.agent import store

OWNER = "owen@example.com"  # whom it tells the outcome
# Its work, as its own configuration says: the run hands it none.
WORK = "Confirm the venue for the team offsite with Rosa, and tell Owen what she said."
ASK = "rosa@example.com"  # who knows the answer
SYSTEM = (
    "You keep your work moving by asking a colleague in Slack. Whenever you ask, remember the wait with "
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
    """What the agent knows between runs: its work and who owes it an answer by when."""

    work: str | None = None
    waits: dict[str, Wait] = field(default_factory=dict)


# -- what it remembers, in `minutehand.agent.store` ---------------------------------------------------------------

# Production keeps the memory in this process (swap in `store.SqliteBackend(path)` to keep it across restarts).
# Under Minutehand the store is the run's own memory, which a fork starts from as it stood at its checkpoint.
store.configure(store.MemoryBackend())
waits_kept = store.collection("waits")
memory = Memory()


def recall() -> None:
    """Read what the agent remembers from the store: on every wake, event and report, never from the last one."""
    global memory
    work = store.get("work")
    waits: dict[str, Wait] = {}
    for email, kept in waits_kept.list():
        assert isinstance(kept, dict)
        waits[email] = Wait(datetime.fromisoformat(str(kept["expected_by"])), int(str(kept["asks"])))
    memory = Memory(work=work if isinstance(work, str) else None, waits=waits)


def keep() -> None:
    """Write what the agent remembers back to the store, all of it at once."""
    gone = {email for email, _ in waits_kept.list()} - memory.waits.keys()
    with store.batch() as kept:
        kept.put("work", memory.work)
        for email in gone:
            kept.delete(email, collection="waits")
        for email, wait in memory.waits.items():
            kept.put(email, {"expected_by": wait.expected_by.isoformat(), "asks": wait.asks}, collection="waits")


def remembering(handle: Callable[[dict[str, str]], None]) -> Callable[[dict[str, str]], None]:
    """Recall before handling a wake or an event, and keep what changed after it."""

    def handled(said: dict[str, str]) -> None:
        recall()
        try:
            handle(said)
        finally:
            keep()

    return handled


def said(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "send_slack_message",
    "Send a direct message in Slack to the person with this email address.",
    {"email": str, "text": str},
)
async def send_slack_message(args: dict[str, Any]) -> dict[str, Any]:
    user = slack.users_lookupByEmail(email=args["email"])["user"]["id"]
    channel = slack.conversations_open(users=[user])["channel"]["id"]
    slack.chat_postMessage(channel=channel, text=args["text"])
    return said(f"sent to {args['email']}")


@tool(
    "remember_wait",
    "Remember that `email` owes an answer, expected by `expected_by` (ISO 8601). Call it after every ask.",
    {"email": str, "expected_by": str},
)
async def remember_wait(args: dict[str, Any]) -> dict[str, Any]:
    email, waits = args["email"], memory.waits
    waits[email] = Wait(datetime.fromisoformat(args["expected_by"]), waits[email].asks + 1 if email in waits else 1)
    return said(f"waiting on {email} until {args['expected_by']}")


@tool("close_wait", "Nothing more is owed by `email`: they answered, or the owner has been told.", {"email": str})
async def close_wait(args: dict[str, Any]) -> dict[str, Any]:
    memory.waits.pop(args["email"], None)
    return said(f"closed {args['email']}")


TOOLS = create_sdk_mcp_server("follow_up", tools=[send_slack_message, remember_wait, close_wait])
WORKING_DIR = tempfile.mkdtemp(prefix="claude-agent-")  # the command line's sessions and settings stay in here
OPTIONS = ClaudeAgentOptions(
    system_prompt=SYSTEM,
    model="claude-sonnet-4-5",
    tools=[],  # none of Claude Code's own tools: no shell, no files
    mcp_servers={"follow_up": TOOLS},
    allowed_tools=[f"mcp__follow_up__{t.name}" for t in (send_slack_message, remember_wait, close_wait)],
    setting_sources=[],
    cwd=WORKING_DIR,
    max_turns=5,
    env={
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:8790"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "sk-ant-recipe"),
        "CLAUDE_CONFIG_DIR": WORKING_DIR,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no update checks or error reports from the command line
    },
)


async def ask_claude(situation: str) -> None:
    async for message in query(prompt=situation, options=OPTIONS):
        if isinstance(message, ResultMessage) and message.is_error:
            raise RuntimeError(f"the agent's run failed: {message.result}")


def run(now: datetime, happened: str) -> None:
    assert memory.work is not None
    situation = f"It is now {now.isoformat()}.\nWork: {memory.work}\nReport to: {OWNER}. Ask: {ASK}.\n{happened}"
    asyncio.run(ask_claude(situation))


# -- the Minutehand side: three endpoints ----------------------------------------------------------------------


@remembering
def wake(request: dict[str, str]) -> None:
    now = datetime.fromisoformat(request["now"])
    if request["reason"] == "start":
        memory.work = WORK
        run(now, "Nobody has been asked yet.")
        return
    for email, wait in list(memory.waits.items()):
        if wait.expected_by <= now:
            expected = wait.expected_by.isoformat()
            run(now, f"No answer yet from {email}, expected by {expected}. Follow-ups sent: {wait.asks - 1}.")


def report() -> dict[str, object]:
    recall()
    if memory.work is None:
        return {"status": "idle", "next_wake": None}
    if not memory.waits:
        return {"status": "done", "next_wake": None}
    return {"status": "idle", "next_wake": min(w.expected_by for w in memory.waits.values()).isoformat()}


@remembering
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
    port = int(os.environ.get("PORT", "8713"))
    print(f"listening on {port}", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
