"""A test agent whose state lives only in its process: each wake it asks its model for a note, keeps every note in a
list, and posts the whole list to Sam in Slack. Restarted, it would forget; replayed, it rebuilds the list from the
recorded answers (`minutehand replay`). It wakes once a day.

    MODEL_URL  its model's chat completions URL;  PORT  where it listens"""

from __future__ import annotations

import json
import os
import socketserver
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from slack_sdk import WebClient

notes: list[str] = []
last: list[datetime] = []
lock = threading.Lock()
slack = WebClient(token="xoxb-remembers")


def wake(now: datetime) -> None:
    with lock:
        last.append(now)
        asked = {
            "model": "model-luna",
            "messages": [{"role": "user", "content": f"note {len(notes) + 1}; so far: {notes}"}],
        }
        said = httpx.post(os.environ["MODEL_URL"], json=asked, timeout=30).json()
        notes.append(said["choices"][0]["message"]["content"])
        user = slack.users_lookupByEmail(email="sam@example.com")["user"]["id"]
        channel = slack.conversations_open(users=[user])["channel"]["id"]
        slack.chat_postMessage(channel=channel, text=" | ".join(notes))


class Handler(BaseHTTPRequestHandler):
    def _answer(self, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        with lock:
            due = (last[-1] + timedelta(days=1)).isoformat() if last else None
        self._answer({"status": "idle", "next_wake": due})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length", "0")))
        if self.path == "/wake":
            wake(datetime.fromisoformat(json.loads(raw)["now"]))
        self._answer({})

    def log_message(self, format: str, *args: object) -> None:
        pass


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


if __name__ == "__main__":
    Server(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
