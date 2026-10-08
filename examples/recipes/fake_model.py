"""A stand-in for a model API, so every recipe runs offline and gives the same answer every time.

Nothing here is a model. It is a few rules that say what a model would answer to the situations the recipes'
agents describe, served over the two wire formats the frameworks speak:

    POST /v1/chat/completions   OpenAI's chat completions (LangGraph through langchain-openai, the OpenAI
                                Agents SDK, Pydantic AI, the Vercel AI SDK)
    POST /v1/messages           Anthropic's messages, plain or streamed (the Claude Agent SDK, the Anthropic SDK)

Each recipe's agent writes one situation per turn, in plain sentences, and the rules read them:

    It is now 2026-08-24T09:00:00+00:00.
    Goal: Confirm the venue for the team offsite with Rosa.
    Owner: owen@example.com. Ask: rosa@example.com.
    and one of
        Nobody has been asked yet.
        No answer yet from rosa@example.com, expected by 2026-08-26T09:00:00+00:00. Follow-ups sent: 0.
        rosa@example.com answered: The lakeside hall, booked for the 14th.

and the "model" answers with the tool calls a good agent would make: ask and remember the wait with the date an
answer is expected by; follow up once and remember the new date; after that, tell the owner nobody answered and
close the wait; on an answer, thank the person, tell the owner, and close the wait. Once the tools have answered it
says it is finished. The tools it calls are the same in every recipe:

    send_slack_message(email, text)     a direct message in Slack
    remember_wait(email, expected_by)   an answer is owed by `email`, expected by that ISO 8601 moment
    close_wait(email)                   nothing more is owed by `email`

    python fake_model.py [--port 8790]

It also writes what Minutehand's people say, when Minutehand itself asks (a request whose structured answer is
`WrittenStep`, `WrittenReply`, `WrittenDecision` or `WrittenSummary`), so a run with model-written people needs no
real model either. The rules read the prompt Minutehand sends, never guess:

    a script step       its facts ("What this reply says:"), as plain sentences; a step that declines, asks back
                        or defers says so in a fixed sentence
    conversing          an answer only when the last message asks something (holds "?"): what the person knows,
                        as plain sentences, or "I do not know."; else no answer
    a decision          the one the script decided, else the first offered; each input from the reasons given
    a summary           how many earlier messages there were

Each answer to the same prompt is the same, so a run with it is repeatable.

It uses nothing outside the standard library, so it runs in whatever Python is at hand.
"""

from __future__ import annotations

import argparse
import json
import re
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8790
WAIT = timedelta(days=2)
"""How long the rules give a person to answer, from each message that asks them."""
FINISHED = "Finished for now."


@dataclass(frozen=True)
class Call:
    """One tool call the rules decided on."""

    name: str
    arguments: dict[str, str]


def _found(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    if match is None:
        raise ValueError(f"the situation does not say {pattern!r}: {text!r}")
    return match.group(1).strip()


def decide(situation: str) -> list[Call]:
    """The tool calls a careful agent makes in this situation."""
    now = datetime.fromisoformat(_found(r"It is now (\S+?)\.?(?:\s|$)", situation))
    goal = _found(r"Goal: (.+)", situation)
    owner = _found(r"Owner: (\S+@\S+)\.(?:\s|$)", situation)
    person = _found(r"Ask: (\S+@\S+)\.(?:\s|$)", situation)
    expected_by = (now + WAIT).isoformat()
    question = f"Hello! I am working on this for {owner}: {goal} Could you help, please?"
    if "Nobody has been asked yet." in situation:
        return [
            Call("send_slack_message", {"email": person, "text": question}),
            Call("remember_wait", {"email": person, "expected_by": expected_by}),
        ]
    answered = re.search(r"(\S+@\S+) answered: (.+)", situation)
    if answered is not None:
        return [
            Call("send_slack_message", {"email": answered.group(1), "text": "Thank you!"}),
            Call(
                "send_slack_message",
                {"email": owner, "text": f"Confirmed: {goal} {answered.group(1)} says: {answered.group(2).strip()}"},
            ),
            Call("close_wait", {"email": answered.group(1)}),
        ]
    if int(_found(r"Follow-ups sent: (\d+)", situation)) == 0:
        return [
            Call("send_slack_message", {"email": person, "text": f"Following up: {question}"}),
            Call("remember_wait", {"email": person, "expected_by": expected_by}),
        ]
    return [
        Call(
            "send_slack_message", {"email": owner, "text": f"No answer from {person}, even after a follow-up: {goal}"}
        ),
        Call("close_wait", {"email": person}),
    ]


# -- what people say, when Minutehand asks -------------------------------------------------------------------

PEOPLE = ("WrittenStep", "WrittenReply", "WrittenDecision", "WrittenSummary")


def _bullets(text: str, heading: str) -> list[str]:
    """The `- ` lines under `heading`, up to the first line that is not one."""
    if heading not in text:
        return []
    found: list[str] = []
    for line in text.split(heading, 1)[1].splitlines()[1:]:
        if not line.startswith("- "):
            break
        found.append(line[2:].strip())
    return found


def _sentences(facts: list[str]) -> str:
    said = []
    for fact in facts:
        fact = fact.strip()
        if not fact:
            continue
        fact = fact[0].upper() + fact[1:]
        said.append(fact if fact.endswith((".", "!", "?")) else fact + ".")
    return " ".join(said)


_UNKNOWN = ("nothing about this beyond", "what you know, as it bears on", "what you know")


def _known(system: str, heading: str) -> list[str]:
    return [f for f in _bullets(system, heading) if not f.startswith(_UNKNOWN)]


def _last_message(shown: str) -> str:
    marker = "Their last message, which you answer now:\n"
    return shown.split(marker, 1)[1] if marker in shown else shown


def person_answer(schema: str, system: str, shown: str) -> dict[str, object]:
    """What a person says, by the rules above."""
    known = _known(system, "What you know:")
    if schema == "WrittenSummary":
        lines = [line for line in shown.splitlines() if line.startswith("[")]
        return {"summary": f"{len(lines)} earlier messages."}
    if schema == "WrittenStep":
        carries = _known(system, "What this reply says:") or known
        intent = system.split("What you do with this message: ", 1)[1].splitlines()[0]
        if intent.startswith("you say that it is not yours"):
            return {"text": "That is not mine to answer."}
        if intent.startswith("you do not answer yet: you ask"):
            return {"text": "Before I answer: what is it for?"}
        if intent.startswith("you say you will come back"):
            return {"text": ("I will come back to you on this. " + _sentences(carries)).strip()}
        return {"text": _sentences(carries) or "I do not know."}
    if schema == "WrittenReply":
        if "?" not in _last_message(shown):
            return {"replies": False, "text": None, "press": None, "form": None}
        return {"replies": True, "text": _sentences(known) or "I do not know.", "press": None, "form": None}
    offered = re.findall(r'^- "([a-z][a-z0-9_]*)"', shown, flags=re.MULTILINE)
    decided = re.search(r'You have decided: "([a-z][a-z0-9_]*)"', system)
    decision = decided.group(1) if decided else offered[0]
    because = _known(system, "because:") or known
    block = shown.split(f'- "{decision}"', 1)[1].split('\n- "', 1)[0] if f'- "{decision}"' in shown else ""
    inputs = [
        {"name": name, "value": _sentences(because) or "No reason given."}
        for name in re.findall(r'input "([a-z][a-z0-9_]*)"', block)
    ]
    return {"decision": decision, "inputs": inputs}


def people_schema(body: dict[str, object]) -> str | None:
    """The structured answer a request asks for when Minutehand asks it to write what a person says."""
    response_format = body["response_format"] if "response_format" in body else None
    if not isinstance(response_format, dict) or "json_schema" not in response_format:
        return None
    name = str(response_format["json_schema"]["name"])
    return name if name in PEOPLE else None


def people_completion(body: dict[str, object], schema: str, number: int) -> dict[str, object]:
    messages = body["messages"]
    assert isinstance(messages, list)
    system = _text(messages[0]["content"])
    shown = _text(messages[-1]["content"])
    content = json.dumps(person_answer(schema, system, shown))
    return {
        "id": f"chatcmpl-{number}",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": (len(system) + len(shown)) // 4,
            "completion_tokens": len(content) // 4,
            "total_tokens": (len(system) + len(shown) + len(content)) // 4,
        },
    }


# -- reading a request -----------------------------------------------------------------------------------------


def _kind(part: object) -> str:
    return str(part["type"]) if isinstance(part, dict) and "type" in part else ""


def _text(content: object) -> str:
    """A message's text, whether it is a string or a list of parts in either format."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part["text"]) for part in content if _kind(part) in ("text", "input_text"))
    return ""


def _is_tool_result(message: dict[str, object]) -> bool:
    if message["role"] == "tool":
        return True
    content = message["content"] if "content" in message else None
    return isinstance(content, list) and any(_kind(part) == "tool_result" for part in content)


def _offered(body: dict[str, object]) -> list[str]:
    """The names of the tools the request offers, in either format."""
    tools = body["tools"] if "tools" in body else []
    assert isinstance(tools, list)
    return [str(t["function"]["name"] if "function" in t else t["name"]) for t in tools]


def _as_offered(call: Call, offered: list[str]) -> Call:
    """The call under the name the framework gave the tool: its own, or with a prefix such as an MCP server's."""
    for name in offered:
        if name == call.name or name.endswith(f"__{call.name}"):
            return Call(name, call.arguments)
    raise ValueError(f"the agent offers no tool named {call.name}; it offers {offered}")


def answer_for(body: dict[str, object]) -> list[Call] | str:
    """Tool calls for the latest situation, or the closing words once its tools have answered."""
    messages = body["messages"]
    assert isinstance(messages, list)
    for message in reversed(messages):
        if _is_tool_result(message):
            return FINISHED
        if message["role"] == "user":
            situation = _text(message["content"])
            if "It is now" in situation:
                offered = _offered(body)
                return [_as_offered(call, offered) for call in decide(situation)]
    raise ValueError("no situation in the conversation")


# -- the two wire formats --------------------------------------------------------------------------------------


def chat_completion(body: dict[str, object], number: int) -> dict[str, object]:
    decided = answer_for(body)
    if isinstance(decided, str):
        message: dict[str, object] = {"role": "assistant", "content": decided}
        finish = "stop"
    else:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{number}_{i}",
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for i, c in enumerate(decided)
            ],
        }
        finish = "tool_calls"
    return {
        "id": f"chatcmpl-{number}",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def message_blocks(body: dict[str, object], number: int) -> tuple[list[dict[str, object]], str]:
    decided = answer_for(body)
    if isinstance(decided, str):
        return [{"type": "text", "text": decided}], "end_turn"
    return [
        {"type": "tool_use", "id": f"toolu_{number}_{i}", "name": c.name, "input": c.arguments}
        for i, c in enumerate(decided)
    ], "tool_use"


def message(body: dict[str, object], number: int) -> dict[str, object]:
    blocks, stop = message_blocks(body, number)
    return {
        "id": f"msg_{number}",
        "type": "message",
        "role": "assistant",
        "model": body["model"],
        "content": blocks,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def message_events(body: dict[str, object], number: int) -> list[tuple[str, dict[str, object]]]:
    """The same message as the server-sent events of a streamed answer."""
    blocks, stop = message_blocks(body, number)
    start = message(body, number) | {"content": [], "stop_reason": None}
    events: list[tuple[str, dict[str, object]]] = [("message_start", {"type": "message_start", "message": start})]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            opened: dict[str, object] = {"type": "text", "text": ""}
            delta: dict[str, object] = {"type": "text_delta", "text": block["text"]}
        else:
            opened = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events += [
            ("content_block_start", {"type": "content_block_start", "index": i, "content_block": opened}),
            ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": i}),
        ]
    events += [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return events


# -- the server ------------------------------------------------------------------------------------------------


@dataclass
class Served:
    """What the server was asked, in order: the path and the request body."""

    received: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def handler(served: Served) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)))
            with served.lock:
                served.received.append((path, body))
                number = len(served.received)
            try:
                schema = people_schema(body) if path.endswith("/chat/completions") else None
                if schema is not None:
                    self.json(200, people_completion(body, schema, number))
                elif path.endswith("/chat/completions"):
                    self.json(200, chat_completion(body, number))
                elif path.endswith("/messages") and "stream" in body and body["stream"]:
                    self.stream(message_events(body, number))
                elif path.endswith("/messages"):
                    self.json(200, message(body, number))
                else:
                    self.json(404, {"error": {"message": f"no such endpoint: {path}"}})
            except ValueError as refused:
                self.json(400, {"error": {"type": "invalid_request_error", "message": str(refused)}})

        def json(self, status: int, payload: dict[str, object]) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def stream(self, events: list[tuple[str, dict[str, object]]]) -> None:
            encoded = b"".join(f"event: {name}\ndata: {json.dumps(e)}\n\n".encode() for name, e in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # Skip HTTPServer's reverse DNS lookup of this machine's name, which can take seconds and is never used.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def start(port: int = 0) -> tuple[Server, Served]:
    """Serve on 127.0.0.1 in a thread of this process; port 0 lets the system pick one."""
    served = Served()
    server = Server(("127.0.0.1", port), handler(served))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, served


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A stand-in for a model API, for the recipes.")
    parser.add_argument("--port", type=int, default=PORT)
    port = parser.parse_args().port
    server = Server(("127.0.0.1", port), handler(Served()))
    print(f"a fake model API on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
