"""An agent that chases a person by email and then puts a meeting in their calendar, on Gmail and Google Calendar.

Stock `googleapiclient` with stock `google-auth` user credentials, configured by nothing but the environment it is
started with. It signs in with a refresh token as an account of its own, and polls: Gmail pushes it nothing.

    POST /wake   START: email the question and remember the mailbox's history id;
                 later: read the mailbox's history; once the answer is in, invite them to a call and tell the owner
                 what they said; once they have accepted, it is done
    GET  /report IDLE with the next poll half an hour on, or DONE

    python workspace_agent.py --port P --state FILE --refresh TOKEN
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import socketserver
import threading
from email import message_from_bytes, policy
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--state", type=Path, required=True)
parser.add_argument("--refresh", required=True)
args = parser.parse_args()

POLL = datetime.timedelta(minutes=30)
SOFIA = "sofia@example.com"
OWNER = "owner@example.com"

lock = threading.Lock()
state: dict[str, object] = {"history": None, "thread": None, "answer": None, "event": None, "done": False, "seen": []}
credentials = Credentials(
    token=None,
    refresh_token=args.refresh,
    token_uri="https://oauth2.googleapis.com/token",
    client_id="1234.apps.googleusercontent.com",
    client_secret="client-secret",
)
gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False)
calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False)


def save() -> None:
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state))


def send(to: str, subject: str, text: str, thread: str | None = None) -> dict[str, str]:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    body: dict[str, str] = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    if thread is not None:
        body["threadId"] = thread
    sent: dict[str, str] = gmail.users().messages().send(userId="me", body=body).execute()
    return sent


def start() -> None:
    profile = gmail.users().getProfile(userId="me").execute()
    sent = send(SOFIA, "Partner pricing", "Hi Sofia, could you confirm the partner pricing for next year?")
    state["history"], state["thread"] = profile["historyId"], sent["threadId"]


def poll(now: datetime.datetime) -> None:
    if state["answer"] is None:
        history = (
            gmail.users()
            .history()
            .list(userId="me", startHistoryId=state["history"], historyTypes="messageAdded")
            .execute()
        )
        state["history"] = history["historyId"]
        for record in history.get("history", []):
            for added in record["messagesAdded"]:
                found = added["message"]
                if found["threadId"] != state["thread"] or "INBOX" not in found["labelIds"]:
                    continue
                message = gmail.users().messages().get(userId="me", id=found["id"], format="raw").execute()
                parsed = message_from_bytes(base64.urlsafe_b64decode(message["raw"] + "=="), policy=policy.default)
                assert isinstance(parsed, EmailMessage)
                body = parsed.get_body(preferencelist=("plain",))
                state["answer"] = body.get_content().strip() if body is not None else ""
                gmail.users().messages().modify(
                    userId="me", id=found["id"], body={"removeLabelIds": ["UNREAD"]}
                ).execute()
        if state["answer"] is None:
            return
        start_at = (now + datetime.timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        event = (
            calendar.events()
            .insert(
                calendarId="primary",
                sendUpdates="all",
                body={
                    "summary": "Partner pricing sign-off",
                    "start": {"dateTime": start_at.isoformat()},
                    "end": {"dateTime": (start_at + datetime.timedelta(minutes=30)).isoformat()},
                    "attendees": [{"email": SOFIA}],
                },
            )
            .execute()
        )
        state["event"] = event["id"]
        send(OWNER, "Partner pricing", f"Sofia says: {state['answer']} I have invited her to sign it off.")
        return
    event = calendar.events().get(calendarId="primary", eventId=state["event"]).execute()
    statuses = {a["email"]: a["responseStatus"] for a in event.get("attendees", [])}
    state["seen"].append(statuses[SOFIA])  # type: ignore[union-attr]
    state["done"] = statuses[SOFIA] == "accepted"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *more: object) -> None:
        pass

    def _send(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        with lock:
            if state["done"]:
                self._send({"status": "done", "next_wake": None})
                return
            now = datetime.datetime.fromisoformat(str(state["now"]))
            self._send({"status": "idle", "next_wake": (now + POLL).isoformat()})

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        with lock:
            now = datetime.datetime.fromisoformat(request["now"])
            state["now"] = now.isoformat()
            if request["reason"] == "start":
                start()
            else:
                poll(now)
            save()
            self._send({"ok": True})


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


Server(("127.0.0.1", args.port), Handler).serve_forever()
