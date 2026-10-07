"""An agent that defers its follow-up with Google Cloud Tasks and handles it when Cloud Tasks calls back.

Stock `google-cloud-tasks` on its REST transport, or with `--grpc` on its default (gRPC), configured by nothing but
the environment it is started with.

    POST /wake            START: create an HTTP task for five hours on, aimed at this agent's own handler
    GET  /report          always IDLE with no next wake: Cloud Tasks is what brings it back
    POST /tasks/follow-up the task's delivery; `--fail-first` answers the first one 503

    python tasks_agent.py --port P --state FILE --queue NAME [--fail-first] [--grpc]
"""

from __future__ import annotations

import argparse
import datetime
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from google.auth.credentials import AnonymousCredentials
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--state", type=Path, required=True)
parser.add_argument("--queue", required=True)
parser.add_argument("--fail-first", action="store_true")
parser.add_argument("--grpc", action="store_true")
args = parser.parse_args()

lock = threading.Lock()
state: dict[str, list[object]] = {"created": [], "handled": []}


def save() -> None:
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state))


def defer(now: datetime.datetime) -> None:
    if args.grpc:
        client = tasks_v2.CloudTasksClient(credentials=AnonymousCredentials())
    else:
        client = tasks_v2.CloudTasksClient(transport="rest", credentials=AnonymousCredentials())
    at = timestamp_pb2.Timestamp()
    at.FromDatetime(now + datetime.timedelta(hours=5))
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"http://127.0.0.1:{args.port}/tasks/follow-up",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"do": "follow up"}).encode(),
        },
        "schedule_time": at,
    }
    state["created"].append(client.create_task(request={"parent": args.queue, "task": task}).name)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *more: object) -> None:
        pass

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(200, {"status": "idle", "next_wake": None})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with lock:
            if self.path == "/wake":
                request = json.loads(raw)
                if request["reason"] == "start":
                    defer(datetime.datetime.fromisoformat(request["now"]))
                save()
                self._send(200, {"ok": True})
                return
            heard = {
                "body": raw.decode(),
                "retry": self.headers["X-CloudTasks-TaskRetryCount"],
                "task": self.headers["X-CloudTasks-TaskName"],
            }
            state["handled"].append(heard)
            save()
            failing = args.fail_first and len(state["handled"]) == 1
            self._send(503 if failing else 200, {})


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


Server(("127.0.0.1", args.port), Handler).serve_forever()
