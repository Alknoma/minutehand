"""A small agent under test, run once per wake by the `Command` driver.

Reads a WakeRequest on stdin, acts on the test providers at $MH_BASE, keeps its own state in
$AGENT_STATE/state.json, and prints an AgentReport. argv[1] picks the behaviour. Standard library only, so it
is a program of its own and not a piece of the test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = os.environ.get("MH_BASE", "")
STATE = Path(os.environ.get("AGENT_STATE", ".")) / "state.json"


def call(method: str, path: str, body: object | None = None) -> object:
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read() or b"null")


def load() -> dict[str, object]:
    return json.loads(STATE.read_text()) if STATE.exists() else {"reasons": []}


def save(state: dict[str, object]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def report(status: str, next_wake: datetime | None = None) -> None:
    print(json.dumps({"status": status, "next_wake": next_wake.isoformat() if next_wake else None}))


def ask_and_file(reason: str, now: datetime, state: dict[str, object]) -> None:
    """START: ask sofia, file a ticket for tom, wake in four days. A reply: thank sofia and ask again.
    DUE: done when tom's ticket is done, otherwise idle with nothing more to wait for."""
    if reason == "start":
        call("POST", "/testchat/messages", {"to": "sofia@example.com", "text": "Can you confirm the pricing?"})
        ticket = call("POST", "/testchat/tickets", {"title": "Legal review", "assignee": "tom@example.com"})
        assert isinstance(ticket, dict)
        state["ticket"] = ticket["id"]
        state["check_at"] = (now + timedelta(days=4)).isoformat()
    elif reason == "person_replied":
        inbox = call("GET", "/testchat/inbox")
        assert isinstance(inbox, list)
        state["heard"] = [m["text"] for m in inbox]
        call("POST", "/testchat/messages", {"to": "sofia@example.com", "text": "Thanks. When can you sign?"})
    elif reason == "due":
        ticket = call("GET", f"/testchat/tickets/{state['ticket']}")
        assert isinstance(ticket, dict)
        save(state)
        report("done" if ticket["state"] == "done" else "idle")
        return
    save(state)
    report("idle", datetime.fromisoformat(str(state["check_at"])))


PLACEHOLDER = "Thinking..."
QUESTION = "Can you confirm the pricing?"


def placeholder(reason: str, now: datetime, state: dict[str, object]) -> None:
    """START: post a placeholder to sofia and edit it into the question before the wake ends. A reply: done."""
    if reason == "start":
        posted = call("POST", "/testchat/messages", {"to": "sofia@example.com", "text": PLACEHOLDER})
        assert isinstance(posted, dict)
        call("PUT", f"/testchat/messages/{posted['id']}", {"text": QUESTION})
        save(state)
        report("idle")
        return
    save(state)
    report("done")


def placeholder_later(reason: str, now: datetime, state: dict[str, object]) -> None:
    """START: post a placeholder to sofia and wake in an hour. DUE: edit it into the question, and once more
    to the same text. A reply: edit it to say it was answered, and done."""
    if reason == "start":
        posted = call("POST", "/testchat/messages", {"to": "sofia@example.com", "text": PLACEHOLDER})
        assert isinstance(posted, dict)
        state["message"] = posted["id"]
        save(state)
        report("idle", now + timedelta(hours=1))
        return
    if reason == "due":
        call("PUT", f"/testchat/messages/{state['message']}", {"text": QUESTION})
        call("PUT", f"/testchat/messages/{state['message']}", {"text": QUESTION})
        save(state)
        report("idle")
        return
    call("PUT", f"/testchat/messages/{state['message']}", {"text": "Answered, thank you."})
    save(state)
    report("done")


def ask_silent(reason: str, now: datetime, state: dict[str, object]) -> None:
    if reason == "start":
        call("POST", "/testchat/messages", {"to": "dania@example.com", "text": "Could you review the contract?"})
    after = os.environ.get("NEXT_WAKE_AFTER_HOURS")
    save(state)
    report("idle", now + timedelta(hours=float(after)) if after else None)


def keep_waking(reason: str, now: datetime, state: dict[str, object]) -> None:
    save(state)
    report("idle", now + timedelta(hours=1))


def book(reason: str, now: datetime, state: dict[str, object]) -> None:
    if reason == "start":
        call("POST", "/testsched/schedules", {"ref": "kept", "at": (now + timedelta(hours=5)).isoformat()})
        call("POST", "/testsched/schedules", {"ref": "dropped", "at": (now + timedelta(hours=2)).isoformat()})
        call("DELETE", "/testsched/schedules/dropped")
    save(state)
    report("idle")


def book_and_poll(reason: str, now: datetime, state: dict[str, object]) -> None:
    """START: book a wake-up in five hours, and leave a poller running that reads the scheduler's queue every
    POLL_SECONDS and, for each delivery, follows up with sofia. Every wake reports idle at once: the work a
    delivery asks for is done by the poller, after it."""
    if reason == "start":
        call("POST", "/testsched/schedules", {"ref": "kept", "at": (now + timedelta(hours=5)).isoformat()})
        subprocess.Popen(
            [sys.executable, __file__, "--poll", os.environ["POLL_SECONDS"], os.environ["BACKGROUND_SECONDS"]],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
        )
    save(state)
    report("idle")


def poll(every: float, seconds: float) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            found = call("GET", "/testsched/deliveries")
            assert isinstance(found, dict)
            for _ in found["deliveries"]:
                call("POST", "/testchat/messages", {"to": "sofia@example.com", "text": "Following up as booked."})
        except OSError:
            return
        time.sleep(every)


def ask_and_keep_calling(reason: str, now: datetime, state: dict[str, object]) -> None:
    """START: ask dania, then leave work running in the background that reads the inbox every 40 ms for
    BACKGROUND_SECONDS after this process has reported idle. Later wakes: idle."""
    if reason == "start":
        call("POST", "/testchat/messages", {"to": "dania@example.com", "text": "Could you review the contract?"})
        subprocess.Popen(
            [sys.executable, __file__, "--background", os.environ["BACKGROUND_SECONDS"]],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    save(state)
    report("idle")


def background(seconds: float) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            call("GET", "/testchat/inbox")
        except OSError:
            return  # the run is over and its server gone
        time.sleep(0.04)


def fail(reason: str, now: datetime, state: dict[str, object]) -> None:
    print("the agent fell over", file=sys.stderr)
    sys.exit(3)


BEHAVIOURS = {
    f.__name__: f
    for f in (
        ask_and_file,
        placeholder,
        placeholder_later,
        ask_silent,
        ask_and_keep_calling,
        keep_waking,
        book,
        book_and_poll,
        fail,
    )
}


def main() -> None:
    if sys.argv[1] == "--background":
        background(float(sys.argv[2]))
        return
    if sys.argv[1] == "--poll":
        poll(float(sys.argv[2]), float(sys.argv[3]))
        return
    request = json.loads(sys.stdin.read())
    state = load()
    reasons = state["reasons"]
    assert isinstance(reasons, list)
    reasons.append(request["reason"])
    BEHAVIOURS[sys.argv[1]](request["reason"], datetime.fromisoformat(request["now"]), state)


if __name__ == "__main__":
    main()
