"""An agent whose worker long-polls its own SQS queue, as a worker on SQS does: `ReceiveMessage` with
`WaitTimeSeconds` 20, again and again, on stock boto3 configured only by the environment Minutehand hands it.

Started with `--port <n> --state <file> --mode <empty|booked|visible>`. On the START wake it creates a queue and,
by `--mode`:

- `empty`: nothing more; the worker polls three times and stops.
- `booked`: books a one-time EventBridge Scheduler schedule ten seconds after the wake's `now`, delivering to the
  queue; the worker polls until it receives that message, and deletes it.
- `visible`: sends a message and receives it with a visibility timeout of ten seconds, without deleting it; the
  worker polls until the message is visible again and it receives it, and deletes it.

The wake answers once the worker's first poll is on its way. Its report says IDLE throughout, with a next wake an
hour after the START wake's `now`, and none after that. Every answer the worker had is kept in `--state`.
"""

from __future__ import annotations

import json
import socketserver
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import boto3

WAIT = 20
POLLS = 3


class Agent:
    def __init__(self, state: Path, mode: str) -> None:
        self.state = state
        self.mode = mode
        self.lock = threading.Lock()
        self.sqs: Any = boto3.client("sqs", region_name="us-east-1")
        self.scheduler: Any = boto3.client("scheduler", region_name="us-east-1")
        self.polling = threading.Event()
        self.sqs.meta.events.register("before-send.sqs.ReceiveMessage", self._sending)
        self.next_wake: str | None = None
        self.log: dict[str, list[list[str]]] = {"answers": []}
        self.save()

    def _sending(self, **_: object) -> None:
        self.polling.set()

    def save(self) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps(self.log))

    def wake(self, request: dict[str, object]) -> None:
        now = datetime.fromisoformat(str(request["now"]))
        if request["reason"] != "start":
            self.next_wake = None
            return
        self.next_wake = (now + timedelta(hours=1)).isoformat()
        url = self.sqs.create_queue(QueueName="polls")["QueueUrl"]
        if self.mode == "booked":
            arn = self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
            self.scheduler.create_schedule(
                Name="nudge",
                ScheduleExpression=f"at({(now + timedelta(seconds=10)).strftime('%Y-%m-%dT%H:%M:%S')})",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target={"Arn": arn, "RoleArn": "arn:aws:iam::000000000000:role/scheduler", "Input": "booked"},
            )
        if self.mode == "visible":
            self.sqs.send_message(QueueUrl=url, MessageBody="again")
            self.sqs.receive_message(QueueUrl=url, VisibilityTimeout=10)
        self.polling.clear()
        threading.Thread(target=self.poll, args=(url,), daemon=True).start()
        self.polling.wait(10)

    def poll(self, url: str) -> None:
        for _ in range(POLLS if self.mode == "empty" else 1000):
            got = self.sqs.receive_message(QueueUrl=url, WaitTimeSeconds=WAIT).get("Messages", [])
            self.log["answers"].append([m["Body"] for m in got])
            self.save()
            for message in got:
                self.sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
            if got:
                return


def serve(port: int, state: Path, mode: str) -> None:
    agent = Agent(state, mode)

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"] or 0))
            with agent.lock:
                agent.wake(json.loads(body))
            self._answer({"ok": True})

        def do_GET(self) -> None:
            self._answer({"status": "idle", "next_wake": agent.next_wake})

        def log_message(self, format: str, *args: object) -> None:
            print(format % args, flush=True)

    class Server(ThreadingHTTPServer):
        def server_bind(self) -> None:
            # HTTPServer.server_bind looks up this machine's name with a reverse DNS query, slow on some macOS runners.
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "127.0.0.1", port

    server = Server(("127.0.0.1", port), Handler)
    print(f"listening on {port}", flush=True)
    server.serve_forever(poll_interval=0.05)


if __name__ == "__main__":
    args = sys.argv
    serve(int(args[args.index("--port") + 1]), Path(args[args.index("--state") + 1]), args[args.index("--mode") + 1])
