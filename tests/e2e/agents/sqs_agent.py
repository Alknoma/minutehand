"""An agent that books its own follow-up with EventBridge Scheduler and finds it on its own SQS queue.

Started with `--port <n> --state <file> --act <seconds>`. On the START wake it creates a queue and a one-time
schedule five hours after the wake's `now`, targeting that queue, with stock boto3 configured only by the
environment Minutehand hands it. A poller thread reads the queue on its own timer; for each message it acts for
`--act` seconds and then deletes it, as a worker on SQS does. Its report says IDLE throughout: the agent cannot
know a delivery is on its way, so only the scheduler can tell the run the wake is not over.
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import boto3


class Agent:
    def __init__(self, state: Path, act: float) -> None:
        self.state = state
        self.act = act
        self.lock = threading.Lock()
        self.sqs: Any = boto3.client("sqs", region_name="us-east-1")
        self.scheduler: Any = boto3.client("scheduler", region_name="us-east-1")
        self.queue_url: str | None = None
        self.log: dict[str, list[str]] = {"received": [], "deleted": []}
        self.save()

    def save(self) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps(self.log))

    def wake(self, request: dict[str, object]) -> None:
        if request["reason"] != "start":
            return
        now = datetime.fromisoformat(str(request["now"]))
        url = self.sqs.create_queue(QueueName="follow-ups")["QueueUrl"]
        arn = self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        at = (now + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
        self.scheduler.create_schedule(
            Name="follow-up",
            ScheduleExpression=f"at({at})",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={"Arn": arn, "RoleArn": "arn:aws:iam::000000000000:role/scheduler", "Input": '{"do": "follow up"}'},
        )
        self.queue_url = url
        threading.Thread(target=self.poll, daemon=True).start()

    def poll(self) -> None:
        while True:
            time.sleep(0.2)
            for message in self.sqs.receive_message(QueueUrl=self.queue_url).get("Messages", []):
                self.log["received"].append(message["Body"])
                self.save()
                time.sleep(self.act)
                self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"])
                self.log["deleted"].append(message["Body"])
                self.save()
                return  # one follow-up is all it books: with it taken, the agent is quiet


def serve(port: int, state: Path, act: float) -> None:
    agent = Agent(state, act)

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
            self._answer({"status": "idle"})

        def log_message(self, format: str, *args: object) -> None:
            print(format % args, flush=True)

    class Server(ThreadingHTTPServer):
        def server_bind(self) -> None:
            # HTTPServer.server_bind looks up this machine's name with a reverse DNS query, which takes over 30
            # seconds on some macOS runners. The name is not used.
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "127.0.0.1", port

    server = Server(("127.0.0.1", port), Handler)
    print(f"listening on {port}", flush=True)
    server.serve_forever(poll_interval=0.05)


def keep(state: Path, *, restoring: bool) -> None:
    """`snapshot <state>` and `restore <state>`: the state file, copied to or from Minutehand's snapshot directory."""
    kept = Path(os.environ["MINUTEHAND_SNAPSHOT_DIR"]) / "state.json"
    source, target = (kept, state) if restoring else (state, kept)
    if source.exists():
        shutil.copyfile(source, target)


if __name__ == "__main__":
    args = sys.argv
    if args[1] in ("snapshot", "restore"):
        keep(Path(args[2]), restoring=args[1] == "restore")
    else:
        port, state = int(args[args.index("--port") + 1]), Path(args[args.index("--state") + 1])
        serve(port, state, float(args[args.index("--act") + 1]))
