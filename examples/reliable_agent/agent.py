"""A reliable proactive agent: it runs one purchase (work.py), and keeps going on its own.

Its model does the language and the judgement; plain code gives it a structure it cannot break:

    planner.py   when it wakes next, from its state alone: every open wait's due moment, every held request's next look
    moves.py     what it may do now: only moves the world allows yet; the model picks among them and nothing else
    ledger.py    where every figure it states came from: one from nowhere is not sent
    sender.py    one message per purpose, so nothing is sent twice
    state.py     everything it knows, kept between wakes in `minutehand.agent.store`, never in the process

It is proactive: it decides when it next wakes, and nothing wakes it on a schedule. It answers three calls:

    POST /wake          {"now": ..., "reason": ...}: it is now `now`; do what is due
    GET  /report        {"status": "idle", "next_wake": the planner's moment, or null}
    POST /slack/events  a person wrote to it: recorded, and read on the next wake

An event is only recorded: the model never runs while Slack waits for an answer, so a retried event is never acted on
twice, and what was said is read on the wake that follows (a reply also wakes it).

    AGENT_PORT=8730 python agent.py
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from collections.abc import Mapping
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import ledger
import model
import planner
import sender
from moves import Move, legal
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from state import Request, State, Wait, load, save
from work import WORK

from minutehand.agent import store

PORT = int(os.environ.get("AGENT_PORT") or os.environ.get("MINUTEHAND_RUN_PORT") or "8730")
"""Where it listens: AGENT_PORT, else the port Minutehand gives a run's agent, else 8730. Never a bare PORT, which a
machine may hold for something else."""
SECRET = os.environ.get("AGENT_SLACK_SIGNING_SECRET", "")
MOST_MOVES = 8  # in one wake: a model that keeps choosing is stopped
slack = WebClient(token=os.environ.get("AGENT_SLACK_TOKEN", "xoxb-reliable-agent"))
lock = threading.Lock()


def _said(answer: object, *path: str) -> str:
    """A value out of a Slack answer: its data, key by key."""
    found = getattr(answer, "data", answer)
    for key in path:
        found = found[key] if isinstance(found, dict) else None
    return str(found)


def deliver(email: str, text: str) -> None:
    user = _said(slack.users_lookupByEmail(email=email), "user", "id")
    channel = _said(slack.conversations_open(users=[user]), "channel", "id")
    slack.chat_postMessage(channel=channel, text=text)


def call(method: str, url: str, body: Mapping[str, object] | None = None) -> dict[str, object]:
    """A call to a service, answered 2xx, or `httpx.HTTPStatusError`: a failed call is never taken for an answer. A read
    that failed is no read (its state is not the item's, and the backoff does not move), and a write that failed is not
    done; the wake ends, and the planner tries again shortly."""
    answer = httpx.request(method, url, json=body, timeout=30)
    answer.raise_for_status()
    found = answer.json()
    return found if isinstance(found, dict) else {}


def read_request(state: State, now: datetime) -> None:
    """The request as the service holds it now; the planner's next look moves on while it does not change."""
    assert state.request is not None
    body = call("GET", f"{WORK.approvals}/v1/requests/{state.request.id}")
    status = str(body.get("status", state.request.status))
    unchanged = state.request.unchanged_reads + 1 if status == state.request.status else 0
    said = json.dumps(body).lower()
    asks = [
        a
        for a, words in (("quote", ("quote", "breakdown", "per-unit", "per unit")), ("cost_centre", ("cost centre",)))
        if status == "needs_info" and any(w in said for w in words)
    ]
    asked_back = state.request.asked_back + (status == "needs_info" and state.request.status != "needs_info")
    state.request = Request(
        id=state.request.id,
        status=status,
        read_at=now.isoformat(),
        next_check=planner.next_check(now, unchanged).isoformat(),
        unchanged_reads=unchanged,
        asks_for=asks,
        asked_back=asked_back,
    )


def act(move: Move, state: State, now: datetime) -> None:
    stamp = now.isoformat()
    if move.to is not None:
        sent = sender.send(state, move, lambda: model.write(move, state, stamp), deliver, now)
        if sent and move.name == "ask" and move.about:
            state.waits[move.about] = Wait(move.to, move.about, stamp, planner.expected_by(now).isoformat())
        if sent and move.name == "chase" and move.about:
            wait = state.waits[move.about]
            wait.chases += 1
            wait.expected_by = planner.expected_by(now).isoformat()
        return
    if move.name == "file":
        body = {
            "po": WORK.po,
            "description": WORK.item,
            "amount": WORK.budget,
            "cost_centre": state.facts["cost_centre"]["value"],
        }
        filed = call("POST", f"{WORK.approvals}/v1/requests", body)
        state.request = Request(
            id=str(filed["id"]),
            status=str(filed.get("status", "pending")),
            read_at=stamp,
            next_check=planner.next_check(now, 0).isoformat(),
        )
    elif move.name == "resubmit" and state.request is not None:
        body = {a: state.facts[a]["value"] for a in state.request.asks_for}
        call("POST", f"{WORK.approvals}/v1/requests/{state.request.id}/resubmit", body)
        state.sent[move.purpose] = stamp
        state.request.status, state.request.unchanged_reads = "pending", 0
        state.request.next_check = planner.next_check(now, 0).isoformat()
    elif move.name == "order" and state.request is not None:
        read_request(state, now)  # the order rests on the request as it stands now, not as it stood
        if state.request.status != "approved":
            state.blocked.append(f"not ordered: {state.request.id} is {state.request.status}")
            return
        body = {
            "po": WORK.po,
            "item": WORK.item,
            "quantity": WORK.quantity,
            "amount": WORK.budget,
            "approval": state.request.id,
        }
        state.order = str(call("POST", f"{WORK.orders}/v1/orders", body)["id"])


def wake(now: datetime) -> None:
    """Do what is due now. A model that fails ends the wake, never the agent: what it had done stays done, nothing
    half-done is sent, and the planner wakes it again shortly to try once more."""
    with lock:
        LAST_WAKE.append(now)
        state = load()
        state.retry_at = None
        for name, fact in ledger.configured().items():
            state.facts.setdefault(name, fact)
        try:
            read_inbox(state)
            if state.request and state.order is None and now >= datetime.fromisoformat(state.request.next_check):
                read_request(state, now)
            for _ in range(MOST_MOVES):
                menu = legal(state, now)
                if not menu:
                    break
                move = None
                for _ in range(2):  # a pick off the menu is refused and asked once more
                    picked = model.choose(menu, state, now.isoformat())
                    move = on_menu(picked, menu)
                    if move is not None:
                        break
                    state.blocked.append(f"not on the menu: {picked!r}")
                if move is None:
                    break
                act(move, state, now)
            if legal(state, now):  # a move still open is never left to chance: the planner brings it back
                state.retry_at = planner.in_hours(now + planner.RETRY).isoformat()
        except httpx.HTTPError as failed:
            state.blocked.append(f"model or service failed at {now.isoformat()}: {type(failed).__name__}")
            state.retry_at = planner.in_hours(now + planner.RETRY).isoformat()
        save(state)


def on_menu(picked: str | None, menu: list[Move]) -> Move | None:
    """The move the model named, by its purpose, or, from a model that wrote a move's description or its kind
    instead, the one move that matches it: nothing that is not on the menu."""
    if picked is None:
        return None
    said = picked.strip().casefold()
    for match in (
        lambda m: m.purpose.casefold() == said,
        lambda m: m.purpose.casefold() in said,
        lambda m: m.why.casefold() == said,
        lambda m: m.name.casefold() == said,
    ):
        found = [m for m in menu if match(m)]
        if len(found) == 1:
            return found[0]
    return None


def read_inbox(state: State) -> None:
    """What people wrote since the last wake: each value it brings kept if their words hold it, and each message read
    only once it is, so a model that fails leaves it to be read on the next wake."""
    while state.inbox:
        said = state.inbox[0]
        for about, wait in list(state.waits.items()):
            if wait.person == said["from"] and ledger.heard(
                state, about, model.extract(about, said["text"]), said["text"], f"{said['from']} at {said['at']}"
            ):
                del state.waits[about]
        state.inbox.pop(0)


LAST_WAKE: list[datetime] = []


def report() -> dict[str, object]:
    with lock:
        due = planner.next_wake(load(), LAST_WAKE[-1] if LAST_WAKE else None)
    return {"status": "idle", "next_wake": due.isoformat() if due else None}


def heard(event: dict[str, object]) -> None:
    if event.get("type") != "message" or event.get("bot_id") or not event.get("user"):
        return
    email = _said(slack.users_info(user=str(event["user"])), "user", "profile", "email")
    at = datetime.fromtimestamp(float(str(event.get("ts", "0")))).isoformat()
    with lock:
        state = load()
        state.inbox.append({"from": email, "text": str(event.get("text", "")), "at": at})
        save(state)


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
    """The standard server, bound without its reverse-DNS lookup of its own address (`HTTPServer.server_bind` asks
    `socket.getfqdn`), which can stall for a minute on a machine whose resolver does not answer for 127.0.0.1."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    # In production its memory is a SQLite file; under a run, the run's own memory takes its place.
    store.configure(store.SqliteBackend(os.environ.get("AGENT_DB", "reliable_agent.db")))
    server = Server(("127.0.0.1", PORT), Handler)
    print(f"listening on 127.0.0.1:{PORT}", flush=True)
    server.serve_forever()
