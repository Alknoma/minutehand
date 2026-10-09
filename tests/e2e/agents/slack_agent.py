"""A small proactive agent: a program of its own that knows Slack and the wake contract, and nothing else.

It talks to Slack with the stock `slack_sdk.WebClient` and its default base URL, verifies every pushed
event with the stock `SignatureVerifier`, and keeps everything it knows under one key of `minutehand.agent.store`,
read and written on every request: the run's memory under Minutehand, the SQLite file FILE in production. A fork
needs nothing from it.

    python slack_agent.py serve --port N --state FILE [--trace]

Behaviour, from AGENT_BEHAVIOUR:
  diligent    asks ASK_EMAIL a question when it gets the goal and wakes again in two days; woken with no
              answer it follows up once and waits two days more; on the answer it thanks the person and tells OWNER_EMAIL, done
  forgetful   asks once and reports idle with nothing to wake for; on an answer it thanks the owner, done
  slack_only  has no wake endpoint for its goal: the owner's DM is the goal, and it then acts as diligent
              does on the answer
  unacknowledging  as diligent, but answers every Slack event 500 after acting on it, as a handler that runs past
              Slack's timeout: every retry is acted on again

Other variables: AGENT_SLACK_SIGNING_SECRET (the signing secret), OWN_DB (a SQLite file the agent writes its goal to,
beside its memory), OUTSIDE_FILE (when set, the agent also keeps its
next wake in this file, outside its memory, and reports the one the file holds: state no fork puts back), TRACEPARENT (sent on every Slack call when
set), STRAY_URL (fetched once when the goal arrives), LOOKUP_URL (fetched when the goal arrives and again on
the answer), MAIL_URL (an email API, posted to once when the goal arrives, to MAIL_TO, in the shape a
SendGrid-like API takes: the text as HTML, MAIL_TEXT when set), AROUND_URL (a Slack call made around the proxy
when the goal arrives: posted to AROUND_URL, standing for the real slack.com, by a client that ignores every proxy
variable, under the HTTP client span OpenTelemetry's instrumentation would make for https://slack.com/api/chat.postMessage),
CRM_URL (a contacts collection of a REST API: when the goal arrives the agent creates ASK_EMAIL and OWNER_EMAIL as
contacts and reads the first back; on the answer it lists them, marks the first answered and deletes the second;
every answer goes into its memory under `crm`, for the test).

With --trace it traces itself with the stock OpenTelemetry SDK, exported over OTLP/HTTP to wherever its
environment's OTEL_* variables point: each message it sends is a span `agent turn`, under which a GenAI span
`chat model-test` (the model call that chose the message, with its messages and token counts) ends before a
span `send_dm` whose W3C traceparent rides on every Slack call it makes. Spans are flushed before each request
the agent answers returns.
"""

from __future__ import annotations

import faulthandler
import sys as _sys

# If this program has not started listening after 15 seconds, say where it is stuck.
faulthandler.dump_traceback_later(15, exit=False, file=_sys.stderr)

import json
import os
import socketserver
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from opentelemetry import propagate
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web import SlackResponse

from minutehand.agent import store

TOKEN = "xoxb-agent-under-test"
FOLLOW_UP_AFTER = timedelta(days=2)
QUESTION = "Could you confirm the partner pricing, please?"
MAILED = "I have asked Sofia to confirm the partner pricing and will report back."
FOLLOW_UP = "Following up on the partner pricing: could you confirm it?"
THANKS = "Thank you!"
MODEL = "model-test"


def env(name: str) -> str:
    if name not in os.environ:
        raise SystemExit(f"{name} is not set")
    return os.environ[name]


def answered(response: SlackResponse) -> dict[str, Any]:
    """Slack's JSON for one call; the SDK has already raised on `ok: false`."""
    assert isinstance(response.data, dict)
    return response.data


class Agent:
    def __init__(self, state: Path, *, tracing: bool = False) -> None:
        self.state = state
        self.traces: TracerProvider | None = None
        if tracing:
            self.traces = TracerProvider(resource=Resource.create({"service.name": "slack-agent"}))
            self.traces.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        self.behaviour = env("AGENT_BEHAVIOUR")
        self.verifier = SignatureVerifier(env("AGENT_SLACK_SIGNING_SECRET"))
        headers = {"traceparent": os.environ["TRACEPARENT"]} if "TRACEPARENT" in os.environ else {}
        self.slack = WebClient(token=TOKEN, headers=headers)
        self.lock = threading.Lock()

    # -- state, in its memory ------------------------------------------------------------------------------

    def load(self) -> dict[str, object]:
        found = store.get("state")
        if isinstance(found, dict):
            return {str(k): v for k, v in found.items()}
        return {
            "goal": None,
            "follow_ups": 0,
            "answer": None,
            "status": "idle",
            "next_wake": None,
            "verified": [],
            "rejected": 0,
        }

    def save(self, state: dict[str, object]) -> None:
        store.put("state", state)
        if "OUTSIDE_FILE" in os.environ:
            Path(os.environ["OUTSIDE_FILE"]).write_text(json.dumps(state["next_wake"]))

    # -- Slack ----------------------------------------------------------------------------------------------

    def user_id(self, email: str) -> str:
        return str(answered(self.slack.users_lookupByEmail(email=email))["user"]["id"])

    def dm(self, email: str, text: str) -> None:
        if self.traces is None:
            self._send(email, text)
            return
        tracer = self.traces.get_tracer("slack-agent")
        asked = [{"role": "user", "parts": [{"type": "text", "content": f"What should {email} be told?"}]}]
        said = [{"role": "assistant", "parts": [{"type": "tool_call", "name": "send_dm", "arguments": {"text": text}}]}]
        with tracer.start_as_current_span("agent turn"):
            with tracer.start_as_current_span(
                f"chat {MODEL}",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.system": "openai",
                    "gen_ai.request.model": MODEL,
                    "gen_ai.input.messages": json.dumps(asked),
                    "gen_ai.output.messages": json.dumps(said),
                    "gen_ai.usage.input_tokens": 120,
                    "gen_ai.usage.output_tokens": 18,
                },
            ):
                pass
            with tracer.start_as_current_span("send_dm"):
                carrier: dict[str, str] = {}
                propagate.inject(carrier)
                self.slack.headers["traceparent"] = carrier["traceparent"]
                try:
                    self._send(email, text)
                finally:
                    del self.slack.headers["traceparent"]

    def _send(self, email: str, text: str) -> None:
        channel = answered(self.slack.conversations_open(users=[self.user_id(email)]))["channel"]["id"]
        self.slack.chat_postMessage(channel=channel, text=text)

    def around(self) -> None:
        """A call by a client that ignores the proxy, as Node's fetch without NODE_USE_ENV_PROXY makes one."""
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            os.environ["AROUND_URL"], data=b"{}", headers={"content-type": "application/json"}
        )
        if self.traces is None:
            direct.open(request, timeout=10).close()
            return
        called = {"http.request.method": "POST", "url.full": "https://slack.com/api/chat.postMessage"}
        with self.traces.get_tracer("slack-agent").start_as_current_span("POST", attributes=called):
            direct.open(request, timeout=10).close()

    def look_up(self, state: dict[str, object]) -> None:
        """A lookup the agent makes and keeps no state at: the answer goes into its state file, for the test."""
        if "LOOKUP_URL" not in os.environ:
            return
        with urllib.request.urlopen(os.environ["LOOKUP_URL"], timeout=10) as answer:
            looked = state["looked_up"] if "looked_up" in state else []
            assert isinstance(looked, list)
            looked.append(answer.read().decode())
            state["looked_up"] = looked

    def crm(self, state: dict[str, object], method: str, path: str = "", sent: object | None = None) -> object:
        """One call to the contacts API, its status and answer kept in memory under `crm`, in order."""
        request = urllib.request.Request(
            os.environ["CRM_URL"] + path,
            data=json.dumps(sent).encode() if sent is not None else None,
            headers={"content-type": "application/json", "authorization": "Bearer crm-key-nobody-checks"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                status, text = answer.status, answer.read().decode()
        except urllib.error.HTTPError as refused:
            status, text = refused.code, refused.read().decode()
        kept = state["crm"] if "crm" in state else []
        assert isinstance(kept, list)
        kept.append({"call": f"{method} {path}", "status": status, "text": text})
        state["crm"] = kept
        return json.loads(text) if text else None

    def flush(self) -> None:
        if self.traces is not None:
            self.traces.force_flush()

    def take_goal(self, state: dict[str, object], goal: str, now: datetime) -> None:
        print(f"goal: {goal}", flush=True)
        state["goal"] = goal
        if "OWN_DB" in os.environ:  # a database of its own beside its memory, which no fork puts back
            with sqlite3.connect(os.environ["OWN_DB"]) as own:
                own.execute("CREATE TABLE IF NOT EXISTS goals(goal TEXT)")
                own.execute("INSERT INTO goals VALUES (?)", (goal,))
        if "STRAY_URL" in os.environ:
            try:
                urllib.request.urlopen(os.environ["STRAY_URL"], timeout=10)
            except urllib.error.HTTPError as refused:
                print(f"stray call answered {refused.code}", flush=True)
        self.look_up(state)
        if "MAIL_URL" in os.environ:
            sent = {
                "personalizations": [{"to": [{"email": env("MAIL_TO")}]}],
                "subject": "Partner pricing",
                "content": [{"type": "text/html", "value": f"<p>{os.environ.get('MAIL_TEXT', MAILED)}</p>"}],
                "api_key": "sg-key-in-body",
            }
            mail = urllib.request.Request(
                os.environ["MAIL_URL"],
                data=json.dumps(sent).encode(),
                headers={"content-type": "application/json", "authorization": "Bearer sg-key-in-header"},
            )
            with urllib.request.urlopen(mail, timeout=10) as answer:
                state["mailed"] = answer.status
        if "AROUND_URL" in os.environ:
            self.around()
        if "CRM_URL" in os.environ:
            asked = self.crm(state, "POST", "", {"name": "Sofia", "email": env("ASK_EMAIL"), "stage": "asked"})
            told = self.crm(state, "POST", "", {"name": "Owner", "email": env("OWNER_EMAIL"), "stage": "told"})
            assert isinstance(asked, dict) and isinstance(told, dict)
            state["contacts"] = [asked["id"], told["id"]]
            self.crm(state, "GET", f"/{asked['id']}")
        self.dm(env("ASK_EMAIL"), QUESTION)
        if self.behaviour == "forgetful":
            state["next_wake"] = None
        else:
            state["next_wake"] = (now + FOLLOW_UP_AFTER).isoformat()

    # -- the wake contract ------------------------------------------------------------------------------------

    def wake(self, request: dict[str, object]) -> None:
        now = datetime.fromisoformat(str(request["now"]))
        state = self.load()
        reason = request["reason"]
        if reason == "start" and request["goal"] is not None:
            self.take_goal(state, str(request["goal"]), now)
        elif reason == "due" and state["answer"] is None and self.behaviour != "forgetful":
            follow_ups = state["follow_ups"]
            assert isinstance(follow_ups, int)
            if follow_ups < 1:
                self.dm(env("ASK_EMAIL"), FOLLOW_UP)
                state["follow_ups"] = follow_ups + 1
                state["next_wake"] = (now + FOLLOW_UP_AFTER).isoformat()
            else:
                state["next_wake"] = None
        self.save(state)

    def report(self) -> dict[str, object]:
        state = self.load()
        # Its plan follows from what it remembers: nobody to chase once the answer is in.
        next_wake = state["next_wake"] if state["answer"] is None else None
        outside = Path(os.environ["OUTSIDE_FILE"]) if "OUTSIDE_FILE" in os.environ else None
        if outside is not None and outside.exists():
            next_wake = json.loads(outside.read_text())
        return {"status": state["status"], "next_wake": next_wake}

    # -- the Events API -------------------------------------------------------------------------------------

    def event(self, body: bytes, headers: dict[str, str]) -> bool:
        state = self.load()
        if not self.verifier.is_valid_request(body, headers):
            rejected = state["rejected"]
            assert isinstance(rejected, int)
            state["rejected"] = rejected + 1
            self.save(state)
            return False
        callback = json.loads(body)
        event = callback["event"]
        verified = state["verified"]
        assert isinstance(verified, list)
        verified.append(event["text"])
        when = datetime.fromtimestamp(callback["event_time"]).astimezone()
        if event["user"] == self.user_id(env("ASK_EMAIL")):
            state["answer"] = event["text"]
            self.look_up(state)
            if "CRM_URL" in os.environ:
                asked, told = state["contacts"]  # type: ignore[misc]
                self.crm(state, "GET")
                self.crm(state, "PATCH", f"/{asked}", {"stage": "answered"})
                self.crm(state, "DELETE", f"/{told}")
                self.crm(state, "GET", f"/{told}")
            self.slack.chat_postMessage(channel=event["channel"], text=THANKS)
            self.dm(env("OWNER_EMAIL"), f"Thanks: the pricing is confirmed ({event['text']}).")
            state["status"] = "done"
            state["next_wake"] = None
        elif (
            self.behaviour == "slack_only"
            and state["goal"] is None
            and event["user"] == self.user_id(env("OWNER_EMAIL"))
        ):
            self.take_goal(state, event["text"], when)
        self.save(state)
        return True


def serve(port: int, state: Path, *, tracing: bool) -> None:
    store.configure(store.SqliteBackend(state))
    agent = Agent(state, tracing=tracing)
    wakes = agent.behaviour != "slack_only"

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, status: int, payload: object) -> None:
            agent.flush()
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers["Content-Length"] or 0))

        def do_POST(self) -> None:
            body = self._body()
            with agent.lock:
                if self.path == "/wake" and wakes:
                    agent.wake(json.loads(body))
                    self._answer(200, {"ok": True})
                elif self.path == "/slack/events":
                    if agent.event(body, {k: v for k, v in self.headers.items()}):
                        # `unacknowledging` acts on every event and then never acknowledges it, as a handler that
                        # runs past Slack's timeout does: each retry is acted on again.
                        failed = agent.behaviour == "unacknowledging"
                        self._answer(500 if failed else 200, {"ok": not failed})
                    else:
                        self._answer(401, {"error": "invalid signature"})
                else:
                    self._answer(404, {"error": "no such endpoint"})

        def do_GET(self) -> None:
            with agent.lock:
                if self.path == "/report" and wakes:
                    self._answer(200, agent.report())
                else:
                    self._answer(404, {"error": "no such endpoint"})

        def log_message(self, format: str, *args: object) -> None:
            print(format % args, flush=True)

    class Server(ThreadingHTTPServer):
        def server_bind(self) -> None:
            # HTTPServer.server_bind looks up this machine's name with a reverse DNS query,
            # which took over 30 seconds on GitHub's macOS runner. The name is not used.
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "127.0.0.1", port

    server = Server(("127.0.0.1", port), Handler)
    faulthandler.cancel_dump_traceback_later()
    print(f"listening on {port} as {agent.behaviour}", flush=True)
    server.serve_forever(poll_interval=0.05)


def main() -> None:
    command = sys.argv[1]
    if command == "serve":
        port = int(sys.argv[sys.argv.index("--port") + 1])
        serve(port, Path(sys.argv[sys.argv.index("--state") + 1]), tracing="--trace" in sys.argv)
    else:
        raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    main()
