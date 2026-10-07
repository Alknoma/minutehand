"""An agent that chases people for their budgets, written as a team's real one was: it asks each lead once, wakes every
hour, follows up a lead who has not answered at 24 and 48 hours, escalates to its owner at 72 hours and stops chasing
that lead, never acknowledges an answer, and sends its owner one summary once no lead is left to chase.

Run once per wake by the `Command` driver: reads a WakeRequest on stdin, acts on the test chat at $MH_BASE, keeps its
state in $AGENT_STATE/state.json, prints an AgentReport. argv[1] picks the behaviour:

    correct          the policy above
    nags             follows up every hour once a lead is a day late, instead of at 24 and 48 hours
    never_escalates  follows up at 24 and 48 hours, then waits for the lead forever

The leads are $LEADS, comma-separated keys whose address is <key>@example.com. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE = os.environ.get("MH_BASE", "")
STATE = Path(os.environ.get("AGENT_STATE", ".")) / "state.json"
OWNER = "owner@example.com"
HOUR = timedelta(hours=1)


def call(method: str, path: str, body: object | None = None) -> object:
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read() or b"null")


def send(to: str, text: str) -> None:
    call("POST", "/testchat/messages", {"to": to, "text": text})


def main() -> None:
    behaviour = sys.argv[1]
    request = json.loads(sys.stdin.read())
    now = datetime.fromisoformat(request["now"])
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    leads: dict[str, dict[str, object]] = state.setdefault("leads", {})
    if not leads:
        for key in os.environ["LEADS"].split(","):
            send(f"{key}@example.com", "What is your team's budget for next quarter?")
            leads[key] = {"asked": now.isoformat(), "follow_ups": 0, "answer": None, "escalated": False}
    inbox = call("GET", "/testchat/inbox")
    assert isinstance(inbox, list)
    for said in inbox:
        if said["from"] in leads and leads[said["from"]]["answer"] is None:
            leads[said["from"]]["answer"] = said["text"]  # never acknowledged: the team's choice
    for key, lead in leads.items():
        if lead["answer"] is not None or lead["escalated"]:
            continue
        waited = now - datetime.fromisoformat(str(lead["asked"]))
        made = int(str(lead["follow_ups"]))
        due = waited >= timedelta(hours=24) if behaviour == "nags" else waited >= timedelta(hours=24 * (made + 1))
        if (made < 2 or behaviour == "nags") and due and waited < timedelta(hours=72):
            send(f"{key}@example.com", "Following up: what is your budget for next quarter?")
            lead["follow_ups"] = made + 1
        if waited >= timedelta(hours=72) and behaviour != "never_escalates":
            send(OWNER, f"{key} has not answered about their budget after two reminders; over to you.")
            lead["escalated"] = True
    pending = [k for k, lead in leads.items() if lead["answer"] is None and not lead["escalated"]]
    if not pending and not state.get("summarised"):
        answers = "; ".join(f"{k}: {lead['answer']}" for k, lead in leads.items() if lead["answer"] is not None)
        send(OWNER, f"Budgets: {answers or 'nobody answered'}.")
        state["summarised"] = True
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    done = not pending
    print(json.dumps({"status": "done" if done else "idle", "next_wake": None if done else (now + HOUR).isoformat()}))


if __name__ == "__main__":
    main()
