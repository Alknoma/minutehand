"""A stand-in for the reliable agent's own model, an OpenAI-compatible chat-completions server on 127.0.0.1, in two
moods. `careful` picks the first move offered and writes only the facts it is shown. `reckless` makes the trial's
mistakes on purpose: it first asks to order whatever is offered, and writes a per-unit price nobody gave it into
every message. The agent's structure, not the model, keeps either from doing harm."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _answer(prompt: str, reckless: bool, picks: list[int]) -> dict[str, object]:
    if prompt.startswith("Pick one move"):
        offered = re.findall(r"^- \w+ \(([^)]*)\):", prompt, re.MULTILINE)
        picks[0] += 1
        if reckless and picks[0] % 2 == 1:
            return {"move": "order"}  # off the menu unless the request is approved
        return {"move": offered[0] if offered else None}
    if prompt.startswith("Write the message"):
        facts = dict(re.findall(r"^- (\w+): (.+)$", prompt, re.MULTILINE))
        found = re.search(r" to: (.+)\.$", prompt, re.MULTILINE)
        why = found.group(1) if found else ""
        po = facts.get("po", "the order")
        asked = re.match(r"(?:ask them for|follow up: their answer on) the (.+?)(?:,| was|$)", why)
        if asked:
            text = f"Hello, could you tell me the {asked.group(1)} for {po}, please?"
        else:
            text = f"Hello. {po}: {why.removeprefix('tell them ')}."
        return {"text": text + (" The quote is $1,200 per unit." if reckless else "")}
    if prompt.startswith("Give the "):
        said = prompt.split("They wrote:\n", 1)[-1]
        found = (
            re.search(r"[A-Z]{2,}-\d+", said) if "cost_centre" in prompt else re.search(r"\$[\d,]+ per \w+[^.]*", said)
        )
        return {"value": found.group(0) if found else None}
    return {}


@contextmanager
def agent_model(*, reckless: bool = False) -> Iterator[str]:
    """The server's base URL, for AGENT_MODEL_BASE_URL."""
    picks = [0]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            prompt = body["messages"][-1]["content"]
            content = json.dumps(_answer(prompt, reckless, picks))
            raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
