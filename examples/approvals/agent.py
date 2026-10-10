"""An agent that must get a person's sign-off before it acts. It brings its own work (the run hands it none): order
40 laptops (PO-7731) once the approver approves, and tell the requester how it went. It is proactive: it decides when
it next wakes, and nothing wakes it on a schedule; its report names the moment, or none. It is the one agent behind every example of docs/approvals.md; only
where the approval happens changes, by APPROVAL_VIA:

    inbox     in its own product: GET /approvals?approver=<email> lists what waits on a person,
              POST /approvals/<id>/decision {"decision": "approve" | "reject", "reason": "..."} decides it
    slack     a direct message in Slack with Approve and Reject buttons; Reject opens a modal asking why
              (POST /slack/interactive takes the presses and the modal's submission)
    email     an email the approver answers in words (POST /inbound/email takes the answer)
    service   an approval service of its own: POST https://approvals.example/v1/requests files the request,
              GET .../v1/requests/<id> reads it back on every wake

Whatever the channel, it reminds by email. Its wake contract is the usual one:

    POST /wake     {"now": ..., "reason": ...}
    GET  /report   {"status": "idle" | "done", "next_wake": ... or null}

The plan, from the first wake: ask the approver; a day on, still undecided, remind them once by email; two days
on, ask the backup approver as well and tell the requester; four days on, tell the requester it is still waiting
and stop. On an approval it places the order (POST https://api.orders.example/v1/orders) and tells the requester;
on a rejection it places nothing and tells the requester why, in the approver's words.

AGENT_BEHAVIOUR=heedless places the order on any decision, approve or reject: the agent a team's rules must catch.

It keeps everything it knows in `minutehand.agent.store` (the run's memory under Minutehand), so a fork starts from
what it knew, and never reads the machine's clock: the time is the last wake's. Standard library only.

    PORT=8720 APPROVAL_VIA=inbox python agent.py
"""

from __future__ import annotations

import json
import os
import re
import socketserver
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from minutehand.agent import store

APPROVER = "nadia@example.com"
BACKUP = "marta@example.com"
OWNER = "owen@example.com"
NAMES = {APPROVER: "Nadia", BACKUP: "Marta", OWNER: "Owen"}
ORDER = "PO-7731"
WHAT = f"{ORDER}: 40 laptops for the new starters"

MAIL = os.environ.get("MAIL_API", "https://api.mail.example/v3/mail/send")
ORDERS = os.environ.get("ORDERS_API", "https://api.orders.example/v1/orders")
SERVICE = os.environ.get("APPROVALS_API", "https://approvals.example/v1/requests")
SLACK = os.environ.get("SLACK_API", "https://slack.com/api/")

VIA = os.environ.get("APPROVAL_VIA", "inbox")
HEEDLESS = os.environ.get("AGENT_BEHAVIOUR", "careful") == "heedless"


# -- the world outside: plain HTTP through whatever proxy the environment names ---------------------------------


def call(url: str, body: dict | None = None, *, method: str | None = None, form: bool = False) -> dict:
    data = None
    headers = {"Authorization": "Bearer agent-key"}
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as answered:
        raw = answered.read()
    return json.loads(raw) if raw else {}


def email(to: str, subject: str, text: str) -> None:
    call(
        MAIL,
        {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": "purchasing-agent@example.com"},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text}],
        },
    )


def slack(method: str, **fields: object) -> dict:
    sent = {k: json.dumps(v) if isinstance(v, dict | list) else str(v) for k, v in fields.items()}
    answer = call(SLACK + method, sent, form=True)
    if not answer["ok"]:
        raise RuntimeError(f"slack {method}: {answer['error']}")
    return answer


def slack_dm(to: str, text: str, blocks: list | None = None) -> None:
    user = slack("users.lookupByEmail", email=to)["user"]["id"]
    channel = slack("conversations.open", users=user)["channel"]["id"]
    if blocks is None:
        slack("chat.postMessage", channel=channel, text=text)
    else:
        slack("chat.postMessage", channel=channel, text=text, blocks=blocks)


# -- what it remembers ----------------------------------------------------------------------------------------


def work() -> dict:
    return store.get("work", {"status": "idle", "requests": {}, "reminders": 0, "escalated": False})


def keep(state: dict) -> None:
    store.put("work", state)


# -- asking, by channel ---------------------------------------------------------------------------------------


def ask(state: dict, approver: str) -> None:
    number = len(state["requests"]) + 1
    request = {"id": f"apr-{number}", "approver": approver, "status": "pending"}
    question = f"Could you approve {WHAT}? Approve or reject it, please."
    if VIA == "slack":
        buttons = [
            {"type": "button", "action_id": f"approve_{request['id']}", "value": request["id"],
             "text": {"type": "plain_text", "text": "Approve"}, "style": "primary"},
            {"type": "button", "action_id": f"reject_{request['id']}", "value": request["id"],
             "text": {"type": "plain_text", "text": "Reject"}, "style": "danger"},
        ]  # fmt: skip
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": question}},
            {"type": "actions", "elements": buttons},
        ]
        slack_dm(approver, question, blocks)
    elif VIA == "email":
        email(approver, f"Approval needed: {ORDER}", question)
    elif VIA == "service":
        filed = call(SERVICE, {"approver": approver, "subject": WHAT, "operation": ORDER, "status": "pending"})
        request["service_id"] = filed["id"]
    state["requests"][approver] = request  # inbox: listed by GET /approvals from here on


def remind(approver: str) -> None:
    email(approver, f"Reminder: {ORDER}", f"A reminder: {WHAT} is still waiting for your approval.")


def withdraw_others(state: dict, decided_by: str) -> None:
    for approver, request in state["requests"].items():
        if approver != decided_by and request["status"] == "pending":
            request["status"] = "withdrawn"  # inbox: gone from the list, so taken back


# -- deciding -------------------------------------------------------------------------------------------------


def decided(approver: str, decision: str, reason: str, *, act_now: bool = True) -> None:
    """The approver's decision, however it arrived: kept, then acted on once. A decision made through the agent's
    own product is acted on in the wake it brings, after the product has answered the decision."""
    state = work()
    request = state["requests"].get(approver)
    if request is None or request["status"] != "pending" or state["status"] == "done":
        return
    request["status"], request["reason"] = decision, reason
    withdraw_others(state, approver)
    state["to_act"] = approver
    keep(state)
    if act_now:
        act()


def act() -> None:
    state = work()
    approver = state.pop("to_act", None)
    if approver is None:
        return
    request = state["requests"][approver]
    name = NAMES[approver]
    if request["status"] == "approve" or HEEDLESS:
        call(ORDERS, {"po": ORDER, "item": "40 laptops", "approval": request["id"]})  # names the request it rests on
    if request["status"] == "approve":
        email(OWNER, f"{ORDER} ordered", f"{name} approved {ORDER}, and it is ordered.")
    else:
        email(OWNER, f"{ORDER} turned down", f"{name} turned {ORDER} down, so nothing was ordered. {request['reason']}")
    state["status"], state["next_wake"] = "done", None
    keep(state)


def read_email(sender: str, text: str) -> None:
    """An answer in words. An automatic reply naming who covers sends the request on to them."""
    state = work()
    covering = re.search(r"please contact .*\(([^)\s]+@[^)\s]+)\)", text)
    if text.startswith("Automatic reply") and covering is not None:
        delegate = covering.group(1)
        if delegate not in state["requests"]:
            ask(state, delegate)
            keep(state)
        return
    said = text.lower()
    if "reject" in said or "not approve" in said or ("turn" in said and "down" in said):
        decided(sender, "reject", text)
    elif "approve" in said:
        decided(sender, "approve", text)


def look_up_service(state: dict) -> None:
    """The approval service holds what was filed; a request it shows decided is acted on."""
    for approver, request in list(state["requests"].items()):
        if request["status"] == "pending" and "service_id" in request:
            found = call(f"{SERVICE}/{request['service_id']}")
            decision = {"approved": "approve", "rejected": "reject"}.get(found["status"])
            if decision is not None:
                decided(approver, decision, found.get("reason", ""))


# -- waking ---------------------------------------------------------------------------------------------------


def wake(request: dict) -> None:
    now = datetime.fromisoformat(request["now"])
    act()
    state = work()
    if state["status"] == "done":
        return
    if request["reason"] == "start":
        state["started"] = now.isoformat()
        ask(state, APPROVER)
        state["next_wake"] = (now + timedelta(days=1)).isoformat()
        keep(state)
        return
    if VIA == "service":
        look_up_service(state)
        state = work()
        if state["status"] == "done":
            return
    started = datetime.fromisoformat(state["started"])
    if state["next_wake"] is None or now < datetime.fromisoformat(state["next_wake"]):
        return
    if state["reminders"] == 0:
        remind(APPROVER)
        state["reminders"] = 1
        state["next_wake"] = (started + timedelta(days=2)).isoformat()
    elif not state["escalated"]:
        ask(state, BACKUP)
        state["escalated"] = True
        email(OWNER, f"{ORDER} still waiting", f"Nadia has not decided on {ORDER}; I have asked Marta as well.")
        state["next_wake"] = (started + timedelta(days=4)).isoformat()
    else:
        email(OWNER, f"{ORDER} still waiting", f"Nobody has decided on {ORDER} yet, so nothing is ordered.")
        state["next_wake"] = None
    keep(state)


def report() -> dict:
    state = work()
    return {"status": "done" if state["status"] == "done" else "idle", "next_wake": state.get("next_wake")}


def pending_for(approver: str) -> list[dict]:
    request = work()["requests"].get(approver)
    if VIA != "inbox" or request is None or request["status"] != "pending":
        return []
    return [{"id": request["id"], "summary": f"Approve {WHAT}?", "actions": ["approve", "reject"]}]


def approver_of(request_id: str) -> str | None:
    return next((a for a, r in work()["requests"].items() if r["id"] == request_id), None)


REASON_MODAL_BLOCK = "reason_block"


def slack_interaction(payload: dict) -> None:
    if payload["type"] == "block_actions":
        action = payload["actions"][0]
        approver = approver_of(action["value"])
        if approver is None:
            return
        if action["action_id"].startswith("approve_"):
            decided(approver, "approve", "")
        else:  # ask why, in a modal; the decision is taken when it is submitted
            slack("views.open", trigger_id=payload["trigger_id"], view={
                "type": "modal", "callback_id": "reject_reason", "private_metadata": action["value"],
                "title": {"type": "plain_text", "text": "Reject"}, "submit": {"type": "plain_text", "text": "Reject"},
                "blocks": [{"type": "input", "block_id": REASON_MODAL_BLOCK, "label": {"type": "plain_text", "text": "Why?"},
                            "element": {"type": "plain_text_input", "action_id": "reason"}}],
            })  # fmt: skip
    elif payload["type"] == "view_submission":
        approver = approver_of(payload["view"]["private_metadata"])
        reason = payload["view"]["state"]["values"][REASON_MODAL_BLOCK]["reason"]["value"]
        if approver is not None:
            decided(approver, "reject", reason)


# -- serving --------------------------------------------------------------------------------------------------

store.configure(store.SqliteBackend(os.environ.get("AGENT_DB", "approvals.db")))
one_at_a_time = threading.RLock()  # a wake, a decision and a press can arrive together; take them in turn


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        with one_at_a_time:
            if url.path == "/report":
                self.answer(200, report())
            elif url.path == "/approvals":
                approver = urllib.parse.parse_qs(url.query).get("approver", [""])[0]
                self.answer(200, {"items": pending_for(approver), "next": None})
            else:
                self.answer(404, {"error": "no such endpoint"})

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        path = urllib.parse.urlsplit(self.path).path
        with one_at_a_time:
            if path == "/wake":
                wake(json.loads(body))
                self.answer(200, {"ok": True})
            elif path.startswith("/approvals/") and path.endswith("/decision"):
                approver = approver_of(path.split("/")[2])
                if approver is None or work()["requests"][approver]["status"] != "pending":
                    self.answer(409, {"error": "not pending"})
                    return
                sent = json.loads(body)
                decided(approver, sent["decision"], sent.get("reason", ""), act_now=False)
                self.answer(200, {"decided": sent["decision"]})
            elif path == "/inbound/email":
                sent = json.loads(body)
                read_email(sent["from"].lower(), sent["text"])
                self.answer(200, {"ok": True})
            elif path == "/slack/interactive":
                slack_interaction(json.loads(urllib.parse.parse_qs(body.decode())["payload"][0]))
                self.answer(200, None)
            elif path == "/slack/events":
                sent = json.loads(body)
                self.answer(200, {"challenge": sent["challenge"]} if "challenge" in sent else {"ok": True})
            else:
                self.answer(404, {"error": "no such endpoint"})

    def answer(self, status: int, payload: dict | None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Skip HTTPServer's reverse DNS lookup of this machine's name, which can take seconds; it is never used.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8720"))
    print(f"listening on {port}: approvals by {VIA}{', heedless' if HEEDLESS else ''}", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
