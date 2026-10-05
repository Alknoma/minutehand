"""An agent whose state lives in Firestore, and in its own memory: it reads everything once as it starts, keeps
it in memory, answers its report from memory and writes every change through to Firestore. Restoring the
database alone would leave it answering from what it remembers, which is why the restore stops and starts it.

It waits on Rosa to confirm the offsite venue: it opens a commitment when it gets the goal, follows up twice,
two days apart (here a follow-up is a counter, not a message: the recipe is about state, not Slack), and
then gives up and drops the commitment. Standard library only.

    POST /wake    {"now": ..., "reason": ..., "goal": ...}
    GET  /report  {"status": ..., "next_wake": ..., "commitments": [...]}

Firestore, through the emulator's REST API (project demo-minutehand):
    jobs/offsite                          {status, follow_ups, next_wake}
    jobs/offsite/commitments/<key>        {description, opened_at, expected_by, status}

Environment: FIRESTORE_PORT (default 8080), PORT (default 8710).
"""

from __future__ import annotations

import json
import os
import socketserver
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOCUMENTS = (
    f"http://127.0.0.1:{os.environ.get('FIRESTORE_PORT', '8080')}"
    "/v1/projects/demo-minutehand/databases/(default)/documents"
)
JOB = "jobs/offsite"
WAITING = "rosa_confirms_venue"
FOLLOW_UP_AFTER = timedelta(days=2)
FOLLOW_UPS = 2


def firestore(method: str, path: str, body: dict | None = None) -> dict | None:
    request = urllib.request.Request(
        f"{DOCUMENTS}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json", "authorization": "Bearer owner"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def value(field: dict) -> object:
    if "nullValue" in field:
        return None
    if "integerValue" in field:
        return int(field["integerValue"])
    return field["stringValue"]


def fields(document: dict) -> dict[str, object]:
    return {k: value(v) for k, v in document.get("fields", {}).items()}


def encoded(values: dict[str, object]) -> dict:
    def one(v: object) -> dict:
        if v is None:
            return {"nullValue": None}
        if isinstance(v, int):
            return {"integerValue": str(v)}
        return {"stringValue": str(v)}

    return {"fields": {k: one(v) for k, v in values.items()}}


class Agent:
    def __init__(self) -> None:
        """Everything is read once, here."""
        job = firestore("GET", JOB)
        self.job: dict[str, object] = fields(job) if job else {"status": "idle", "follow_ups": 0, "next_wake": None}
        listed = firestore("GET", f"{JOB}/commitments") or {}
        self.commitments = {d["name"].rsplit("/", 1)[1]: fields(d) for d in listed.get("documents", [])}

    def save(self) -> None:
        firestore("PATCH", JOB, encoded(self.job))
        for key, commitment in self.commitments.items():
            firestore("PATCH", f"{JOB}/commitments/{key}", encoded(commitment))

    def wake(self, request: dict) -> None:
        now = datetime.fromisoformat(request["now"])
        if request["reason"] == "start":
            due = (now + FOLLOW_UP_AFTER).isoformat()
            self.job.update(next_wake=due, follow_ups=0)
            self.commitments[WAITING] = {
                "description": "Rosa confirms the offsite venue",
                "opened_at": now.isoformat(),
                "expected_by": due,
                "status": "open",
            }
        elif request["reason"] == "due" and WAITING in self.commitments:
            follow_ups = self.job["follow_ups"]
            assert isinstance(follow_ups, int)
            if follow_ups < FOLLOW_UPS:
                due = (now + FOLLOW_UP_AFTER).isoformat()
                self.job.update(follow_ups=follow_ups + 1, next_wake=due)
                self.commitments[WAITING]["expected_by"] = due
            else:
                self.job["next_wake"] = None
                self.commitments[WAITING]["status"] = "dropped"
        self.save()

    def report(self) -> dict:
        return {
            "status": self.job["status"],
            "next_wake": self.job["next_wake"],
            "commitments": [
                {
                    "key": key,
                    "description": c["description"],
                    "waiting_on": "person",
                    "person_email": "rosa@example.com",
                    "opened_at": c["opened_at"],
                    "expected_by": c["expected_by"],
                    "status": c["status"],
                }
                for key, c in sorted(self.commitments.items())
            ],
        }


agent = Agent()
one_at_a_time = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        with one_at_a_time:
            if self.path == "/wake":
                agent.wake(json.loads(body))
                self.answer(200, {"ok": True})
            else:
                self.answer(404, {"error": "no such endpoint"})

    def do_GET(self) -> None:
        with one_at_a_time:
            if self.path == "/report":
                self.answer(200, agent.report())
            else:
                self.answer(404, {"error": "no such endpoint"})

    def answer(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, flush=True)


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # skip the reverse DNS lookup of this machine's name
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8710"))
    print(f"listening on {port}, state in {DOCUMENTS}/{JOB}", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever(poll_interval=0.05)
