"""The reference agent's API process: it is woken, answers for itself, and takes email replies.

    POST /wake            a WakeRequest: the time, the reason, the goal on the first. Queued for the worker and
                          answered at once; the work happens in the background, in the worker
    GET  /report          an AgentReport: WORKING while any job is queued or running, else IDLE or DONE, with the
                          next moment it wants to be woken and what it is waiting on
    POST /inbound/email   a reply to one of its emails, signed with HMAC-SHA256 over the body
                          (X-Mail-Signature: sha256=<hex>, the secret in REFERENCE_MAIL_SECRET)
    GET  /healthz

On the first wake it thanks the owner by email itself, with `requests`, before it answers.

It keeps in memory which replies it has already taken, so a webhook delivered twice is taken once; a restore
that does not restart it leaves that memory from another moment (`hooks.py fingerprint` shows it).

REFERENCE_REPORT=naive answers WORKING only until the worker has picked the wake's job up, and IDLE from then on,
whatever the worker is still doing: the report many agents start with ("my handler has returned"), and the reason
a checkpoint can be taken mid-work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import telemetry
from opentelemetry import trace
from store import Store, open_store

PORT = int(os.environ.get("REFERENCE_PORT", "8790"))
HOME = Path(os.environ.get("REFERENCE_HOME", "."))
OWNER = os.environ.get("REFERENCE_OWNER", "owen@example.com")
MAIL = os.environ.get("REFERENCE_MAIL_URL", "https://api.mail.example")
SECRET = os.environ.get("REFERENCE_MAIL_SECRET", "")
MAIL_KEY = os.environ.get("REFERENCE_MAIL_KEY", "SG.reference-mail-key")
NAIVE = os.environ.get("REFERENCE_REPORT", "honest") == "naive"


class Api:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.http = requests.Session()
        self.taken = {r["id"] for r in store.replies()}
        self._remember()

    def _remember(self) -> None:
        digest = hashlib.sha256("\n".join(sorted(self.taken)).encode()).hexdigest()
        (HOME / "api.memory").write_text(digest)

    def wake(self, raw: bytes) -> None:
        request = json.loads(raw)
        with telemetry.tracer().start_as_current_span(f"wake {request['reason']}", kind=trace.SpanKind.SERVER):
            if request.get("goal") and self.store.fact("goal") is None:
                self.store.set_facts({"goal": request["goal"], "owner": OWNER, "started": request["now"]})
                self._thank_owner(request["goal"], request["now"])
            carrier = telemetry.inject({})
            self.store.enqueue("wake", {**request, "trace": carrier}, time.time())

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
        facts = {k: self.store.fact(k) for k in ("done", "next_wake", "venue_to", "asked_at", "answered", "goal")}
        if facts["done"] == "1":
            status = "done"
        elif self.store.queued() if NAIVE else self.store.in_flight():
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
        if reply["id"] in self.taken:
            return 200
        self.store.add_reply(
            {"id": reply["id"], "from_addr": reply["from"], "text": reply["text"], "in_reply_to": reply["in_reply_to"]}
        )
        self.taken.add(reply["id"])
        self._remember()
        return 200


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
            if self.path == "/report":
                self._send(200, api.report())
            elif self.path == "/healthz":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path == "/wake":
                api.wake(self._body())
                self._send(200, {"accepted": True})
            elif self.path == "/inbound/email":
                raw = self._body()
                status = api.reply(raw, self.headers.get("x-mail-signature", ""))
                self._send(status, {"ok": status == 200})
            else:
                self._send(404, {"error": "not found"})

    return Handler


def main() -> None:
    telemetry.setup("venue-agent-api")
    api = Api(open_store())
    server = ThreadingHTTPServer(("127.0.0.1", PORT), handler(api))

    def stop(*_: object) -> None:
        telemetry.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    server.serve_forever()


if __name__ == "__main__":
    main()
