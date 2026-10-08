"""A LangGraph agent that asks a colleague in Slack, remembers when it expects an answer, and follows up once.

The graph is the usual model-and-tools loop: a chat model with tools bound, a `ToolNode`, and `tools_condition`
between them. What makes it proactive is one more key in the graph's state, `waits`: each person who owes an
answer and the moment it is expected by. The tools write it (`remember_wait`, `close_wait`). Between wakes the
goal and `waits` are kept in `minutehand.agent.store`: each wake or Slack event runs the graph once, from the goal and
waits the store holds, and writes the waits it ends with back; the report Minutehand asks for after every wake reads
the store: `next_wake` is the earliest moment in `waits`. A wake's conversation is not kept past it: the situation the
agent writes for each run carries what the model needs.

    POST /wake          {"now": ..., "reason": "start" | "due" | ..., "goal": ...}: run the graph on what is due
    GET  /report        {"status": "idle" | "done", "next_wake": the earliest expected-by date, or null}
    POST /slack/events  Slack's Events API: a person's answer, run through the graph

It never reads the machine's clock: the time is the wake's `now`, or a Slack event's own timestamp.

    python agent.py

Environment:
    MODEL_BASE_URL              the chat-completions API (default the recipes' fake model, http://127.0.0.1:8790/v1)
    OPENAI_API_KEY              its key (default a made-up one, which the fake model accepts)
    AGENT_SLACK_SIGNING_SECRET  the secret Slack signs its events with; Minutehand makes one per run
    PORT                        where to listen (default 8711, the port agent.yaml names)
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langchain_openai import ChatOpenAI
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import InjectedState, ToolNode, tools_condition
from langgraph.types import Command
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

from minutehand.agent import store

OWNER = "owen@example.com"  # who gives the agent its goal
ASK = "rosa@example.com"  # who knows the answer
SYSTEM = (
    "You carry one goal for its owner by asking a colleague in Slack. Whenever you ask, remember the wait with "
    "the date you expect an answer by. Follow up once; after that, tell the owner. When the answer comes, thank "
    "the colleague, tell the owner what they said, and close the wait."
)

slack = WebClient(token="xoxb-recipe-agent")


class Wait(TypedDict):
    expected_by: str
    asks: int


def merge_waits(old: dict[str, Wait | None], new: dict[str, Wait | None]) -> dict[str, Wait | None]:
    """A tool's update to `waits`: a person's wait set, or removed with None."""
    merged = {**old, **new}
    return {email: wait for email, wait in merged.items() if wait is not None}


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    waits: Annotated[dict[str, Wait | None], merge_waits]
    goal: str


@tool
def send_slack_message(email: str, text: str) -> str:
    """Send a direct message in Slack to the person with this email address."""
    user = slack.users_lookupByEmail(email=email)["user"]["id"]
    channel = slack.conversations_open(users=[user])["channel"]["id"]
    slack.chat_postMessage(channel=channel, text=text)
    return f"sent to {email}"


@tool
def remember_wait(
    email: str,
    expected_by: str,
    state: Annotated[State, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Remember that `email` owes an answer, expected by `expected_by` (ISO 8601). Call it after every ask."""
    asks = state["waits"][email]["asks"] + 1 if email in state["waits"] else 1
    datetime.fromisoformat(expected_by)  # a date the report can name, or the tool call fails here
    return Command(
        update={
            "waits": {email: Wait(expected_by=expected_by, asks=asks)},
            "messages": [ToolMessage(f"waiting on {email} until {expected_by}", tool_call_id=tool_call_id)],
        }
    )


@tool
def close_wait(email: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """Nothing more is owed by `email`: they answered, or the owner has been told they did not."""
    return Command(
        update={"waits": {email: None}, "messages": [ToolMessage(f"closed {email}", tool_call_id=tool_call_id)]}
    )


TOOLS = [send_slack_message, remember_wait, close_wait]
model = ChatOpenAI(
    model="gpt-4.1-mini",
    base_url=os.environ.get("MODEL_BASE_URL", "http://127.0.0.1:8790/v1"),
    api_key=os.environ.get("OPENAI_API_KEY", "sk-recipe"),  # pyright: ignore[reportArgumentType]
).bind_tools(TOOLS)


def call_model(state: State) -> dict[str, list[AnyMessage]]:
    return {"messages": [model.invoke([("system", SYSTEM), *state["messages"]])]}


graph = (
    StateGraph(State)
    .add_node("model", call_model)
    .add_node("tools", ToolNode(TOOLS))
    .add_edge(START, "model")
    .add_conditional_edges("model", tools_condition)
    .add_edge("tools", "model")
    .compile()
)

# What it remembers between wakes, in `minutehand.agent.store`: the goal, and each wait under its email. Production
# keeps it in this process (swap in `store.SqliteBackend(path)` to keep it across restarts); under Minutehand it is
# the run's own memory, which a fork starts from as it stood at its checkpoint.
store.configure(store.MemoryBackend())
waits_kept = store.collection("waits")


def situation(now: datetime, goal: str, happened: str) -> str:
    return f"It is now {now.isoformat()}.\nGoal: {goal}\nOwner: {OWNER}. Ask: {ASK}.\n{happened}"


def recalled() -> tuple[str | None, dict[str, Wait | None]]:
    """The goal and the waits, as the store holds them now: read on every wake, event and report."""
    goal = store.get("goal")
    waits: dict[str, Wait | None] = {}
    for email, kept in waits_kept.list():
        assert isinstance(kept, dict)
        waits[email] = Wait(expected_by=str(kept["expected_by"]), asks=int(str(kept["asks"])))
    return (goal if isinstance(goal, str) else None), waits


def run(goal: str, waits: dict[str, Wait | None], text: str) -> None:
    """One run of the graph from what the store holds, and the waits it ends with written back, all at once."""
    ended: State = graph.invoke({"messages": [HumanMessage(text)], "goal": goal, "waits": waits})  # type: ignore[assignment]
    held = {email for email, wait in ended["waits"].items() if wait is not None}
    with store.batch() as kept:
        kept.put("goal", goal)
        for email in {e for e, _ in waits_kept.list()} - held:
            kept.delete(email, collection="waits")
        for email, wait in ended["waits"].items():
            if wait is not None:
                kept.put(email, dict(wait), collection="waits")


# -- the Minutehand side: three endpoints ----------------------------------------------------------------------


def wake(request: dict[str, str]) -> None:
    now = datetime.fromisoformat(request["now"])
    if request["reason"] == "start":
        run(request["goal"], {}, situation(now, request["goal"], "Nobody has been asked yet."))
        return
    goal, waits = recalled()
    if goal is None:
        return
    for email, wait in waits.items():
        assert wait is not None
        if datetime.fromisoformat(wait["expected_by"]) <= now:
            overdue = (
                f"No answer yet from {email}, expected by {wait['expected_by']}. Follow-ups sent: {wait['asks'] - 1}."
            )
            run(goal, recalled()[1], situation(now, goal, overdue))


def report() -> dict[str, object]:
    goal, waits = recalled()
    if goal is None:
        return {"status": "idle", "next_wake": None}
    dates = [datetime.fromisoformat(w["expected_by"]) for w in waits.values() if w is not None]
    if not dates:
        return {"status": "done", "next_wake": None}
    return {"status": "idle", "next_wake": min(dates).isoformat()}


def message(event: dict[str, str]) -> None:
    """Someone wrote to the agent: an answer from a person it waits on goes through the graph."""
    goal, waits = recalled()
    if goal is None:
        return
    email = slack.users_info(user=event["user"])["user"]["profile"]["email"]
    if email not in waits:
        return
    now = datetime.fromtimestamp(float(event["ts"]), UTC)
    run(goal, waits, situation(now, goal, f"{email} answered: {event['text']}"))


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
    port = int(os.environ.get("PORT", "8711"))
    print(f"listening on {port}", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
