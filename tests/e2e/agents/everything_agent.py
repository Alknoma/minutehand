"""An agent that touches every part of Minutehand built for a proactive agent, standard library only.

START: asks Rosa in Slack; defers its follow-up five hours with Google Cloud Tasks; records Rosa in a CRM nobody
declared; files an issue through an MCP server over HTTP; removes an old download. Cloud Tasks calls it back to
follow up with Rosa; its own in-process timer (fired by the sandbox stub) tells Owen where things stand. Its report
says it is waiting on Rosa.

    python everything_agent.py --port P --queue NAME --downloads DIR --started ISO
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--queue", required=True)
parser.add_argument("--downloads", type=Path, required=True)
args = parser.parse_args()
opened: dict[str, str] = {}


def call(url: str, payload: object, headers: dict[str, str] | None = None) -> dict:
    sent = Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json", **(headers or {})}
    )
    with urlopen(sent, timeout=30) as answer:
        text = answer.read().decode()
    return json.loads(text) if text.strip().startswith("{") else {}


def slack(method: str, payload: dict) -> dict:
    return call(f"https://slack.com/api/{method}", payload, {"authorization": "Bearer xoxb-everything"})


def tell(email: str, text: str) -> None:
    user = slack("users.lookupByEmail", {"email": email})["user"]["id"]
    channel = slack("conversations.open", {"users": user})["channel"]["id"]
    slack("chat.postMessage", {"channel": channel, "text": text})


def start(now: datetime.datetime) -> None:
    opened["venue"] = now.isoformat()
    tell("rosa@example.com", "Hi Rosa, could you confirm the venue for the offsite?")
    task = {
        "httpRequest": {
            "url": f"http://127.0.0.1:{args.port}/tasks/follow-up",
            "httpMethod": "POST",
            "body": base64.b64encode(b'{"follow": "rosa"}').decode(),
        },
        "scheduleTime": (now + datetime.timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
    }
    call(f"https://cloudtasks.googleapis.com/v2/{args.queue}/tasks", {"task": task})
    call("https://crm.example/api/contacts", {"name": "Rosa Lind", "email": "rosa@example.com"})
    issue = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "create_issue", "arguments": {"title": "Book the offsite venue"}},
    }
    call("https://mcp.example/mcp", issue, {"accept": "application/json, text/event-stream"})
    (args.downloads / "old.zip").unlink()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *more: object) -> None:
        pass

    def _send(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        asked = self.rfile.read(int(self.headers["content-length"] or 0))
        if self.path == "/wake":
            request = json.loads(asked)
            if request["reason"] == "start":
                start(datetime.datetime.fromisoformat(request["now"]))
        elif self.path == "/tasks/follow-up":
            tell("rosa@example.com", "Following up on the offsite venue: could you confirm it?")
        elif self.path == "/timer":
            tell("owen@example.com", "Status: still waiting on Rosa for the venue.")
        self._send({"ok": True})

    def do_GET(self) -> None:
        commitments = (
            [
                {
                    "key": "venue",
                    "description": "Rosa confirms the venue",
                    "waiting_on": "person",
                    "person_email": "rosa@example.com",
                    "opened_at": opened["venue"],
                    "status": "open",
                }
            ]
            if "venue" in opened
            else []
        )
        self._send({"status": "idle", "next_wake": None, "commitments": commitments})


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


Server(("127.0.0.1", args.port), Handler).serve_forever()
