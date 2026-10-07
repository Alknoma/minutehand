"""The reference agent's API process: it is woken, answers for itself, and takes email replies.

    POST /wake            a WakeRequest: the time, the reason, the goal on the first. Queued for the worker and
                          answered at once; the work happens in the background, in the worker
    GET  /report          an AgentReport: WORKING while any job is queued or running, else IDLE or DONE, with the
                          next moment it wants to be woken and what it is waiting on
    POST /inbound/email   a reply to one of its emails, signed with HMAC-SHA256 over the body
                          (X-Mail-Signature: sha256=<hex>, the secret in REFERENCE_MAIL_SECRET)
    GET  /approvals?approver=<email>[&cursor=N]
                          what waits on that approver in the agent's own web app, a page at a time
                          ({"items": [{"id", "summary", "operation", "actions"}], "next": cursor or null}); signed in
                          as the approver (Authorization: Bearer <REFERENCE_APPROVER_TOKEN>)
    POST /approvals/<id>/decision
                          the approver decides: {"decision": "approve" | "reject", "reason": "..."}; 409 once decided
    GET  /healthz

On the first wake it thanks the owner by email itself, with `requests`, before it answers.

Everything it knows is in its memory (`store.py`, over `minutehand_agent.store`), shared with the worker; a webhook
delivered twice is taken once because the memory already holds the reply.

REFERENCE_REPORT=naive answers WORKING only until the worker has picked the wake's job up, and IDLE from then on,
whatever the worker is still doing: the report many agents start with ("my handler has returned"), and the reason
a checkpoint can be taken mid-work: the run then finds the worker still writing its memory after the checkpoint, and
a fork from it is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import requests
import telemetry
from opentelemetry import trace
from store import Store, open_store

PORT = int(os.environ.get("REFERENCE_PORT", "8790"))
OWNER = os.environ.get("REFERENCE_OWNER", "owen@example.com")
MAIL = os.environ.get("REFERENCE_MAIL_URL", "https://api.mail.example")
SECRET = os.environ.get("REFERENCE_MAIL_SECRET", "")
MAIL_KEY = os.environ.get("REFERENCE_MAIL_KEY", "SG.reference-mail-key")
NAIVE = os.environ.get("REFERENCE_REPORT", "honest") == "naive"
APPROVER = os.environ.get("REFERENCE_APPROVER", "")
APPROVER_TOKEN = os.environ.get("REFERENCE_APPROVER_TOKEN", "")
PAGE = int(os.environ.get("REFERENCE_APPROVALS_PAGE", "20"))


class Api:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.http = requests.Session()

    def wake(self, raw: bytes) -> None:
        request = json.loads(raw)
        with telemetry.tracer().start_as_current_span(f"wake {request['reason']}", kind=trace.SpanKind.SERVER):
            if request.get("goal") and self.store.fact("goal") is None:
                self.store.set_facts({"goal": request["goal"], "owner": OWNER, "started": request["now"]})
                self._thank_owner(request["goal"], request["now"])
            carrier = telemetry.inject({})
            self.store.enqueue("wake", {**request, "trace": carrier})

    def _thank_owner(self, goal: str, now: str) -> None:
        body = {
            "personalizations": [{"to": [{"email": OWNER}]}],
            "from": {"email": "agent@venues.example"},
            "subject": "On it",
            "content": [
                {"type": "text/plain", "value": f"I have started on: {goal}. I will tell you when it is done."}
            ],
        }
        headers = telemetry.inject({"authorization": f"Bearer {MAIL_KEY}"})
        answer = self.http.post(f"{MAIL}/v3/mail/send", json=body, headers=headers, timeout=30)
        answer.raise_for_status()
        message_id = str(answer.json().get("id", ""))
        self.store.add_sent(
            {
                "id": f"thanks-{now}",
                "kind": "tell",
                "to_addr": OWNER,
                "subject": "On it",
                "text": "",
                "at": now,
                "message_id": message_id,
            }
        )

    def report(self) -> dict[str, object]:
        # Whether work is in flight is read first: the worker counts a job done after its last write, so once that
        # says idle, the facts read next are the job's last word, not a moment of it.
        busy = self.store.queued() if NAIVE else self.store.in_flight()
        facts = {k: self.store.fact(k) for k in ("done", "next_wake", "venue_to", "asked_at", "answered", "goal")}
        if facts["done"] == "1":
            status = "done"
        elif busy:
            status = "working"
        else:
            status = "idle"
        commitments = []
        if facts["venue_to"] is not None and facts["asked_at"] is not None:
            commitments.append(
                {
                    "key": f"answer:{facts['venue_to']}",
                    "description": "Is the venue free on Friday?",
                    "waiting_on": "person",
                    "person_email": facts["venue_to"],
                    "opened_at": facts["asked_at"],
                    "status": "met" if facts["answered"] == "1" else "open",
                }
            )
        return {"status": status, "next_wake": facts["next_wake"], "commitments": commitments}

    def reply(self, raw: bytes, signature: str) -> int:
        expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not SECRET or not hmac.compare_digest(expected, signature):
            return 401
        reply = json.loads(raw)
        self.store.add_reply(
            {"id": reply["id"], "from_addr": reply["from"], "text": reply["text"], "in_reply_to": reply["in_reply_to"]}
        )
        return 200

    def signed_in(self, authorization: str) -> str | None:
        """The approver the bearer token signs in as; None for any other token, or with no approver configured."""
        given = authorization.removeprefix("Bearer ")
        if not APPROVER or not APPROVER_TOKEN or not hmac.compare_digest(given, APPROVER_TOKEN):
            return None
        return APPROVER

    def pending(self, approver: str, cursor: int) -> dict[str, object]:
        waiting = [a for a in self.store.approvals() if a["approver"] == approver and a["state"] == "pending"]
        page = waiting[cursor : cursor + PAGE]
        following = cursor + PAGE
        return {
            "items": [
                {"id": a["id"], "summary": a["summary"], "operation": a["operation"], "actions": ["approve", "reject"]}
                for a in page
            ],
            "next": str(following) if following < len(waiting) else None,
        }

    def decide(self, approval_id: str, raw: bytes) -> tuple[int, dict[str, object]]:
        asked = json.loads(raw)
        decision = asked["decision"] if isinstance(asked, dict) and "decision" in asked else None
        if decision not in ("approve", "reject"):
            return 400, {"error": "decision is approve or reject"}
        state = "approved" if decision == "approve" else "rejected"
        reason = str(asked["reason"]) if "reason" in asked else ""
        if not self.store.decide_approval(approval_id, state, reason):
            return 409, {"error": f"approval {approval_id} is not pending"}
        return 200, {"ok": True, "state": state}


def handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, status: int, payload: object) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("content-length") or 0))

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            if parts.path == "/approvals":
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                who = api.signed_in(self.headers.get("authorization", ""))
                if who is None or "approver" not in query or query["approver"] != who:
                    self._send(401, {"error": "sign in as the approver whose approvals these are"})
                    return
                self._send(200, api.pending(who, int(query["cursor"]) if "cursor" in query else 0))
            elif self.path == "/report":
                self._send(200, api.report())
            elif self.path == "/healthz":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path == "/wake":
                api.wake(self._body())
                self._send(200, {"accepted": True})
            elif self.path.startswith("/approvals/") and self.path.endswith("/decision"):
                raw = self._body()
                if api.signed_in(self.headers.get("authorization", "")) is None:
                    self._send(401, {"error": "sign in as the approver"})
                    return
                status, answer = api.decide(unquote(self.path.split("/")[2]), raw)
                self._send(status, answer)
            elif self.path == "/inbound/email":
                raw = self._body()
                status = api.reply(raw, self.headers.get("x-mail-signature", ""))
                self._send(status, {"ok": status == 200})
            else:
                self._send(404, {"error": "not found"})

    return Handler


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind looks this machine's name up by reverse DNS, which takes over 30 seconds on some
        # Macs (GitHub's among them). The name is never used: bind and listen at once.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def main() -> None:
    telemetry.setup("venue-agent-api")
    api = Api(open_store())
    server = Server(("127.0.0.1", PORT), handler(api))
    print(f"api listening on 127.0.0.1:{PORT}", flush=True)

    def stop(*_: object) -> None:
        telemetry.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    server.serve_forever()


if __name__ == "__main__":
    main()
