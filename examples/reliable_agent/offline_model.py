"""A stand-in for the reliable agent's own model, an OpenAI-compatible chat-completions server on 127.0.0.1, so the
agent runs with no model account:

    python offline_model.py            # careful, on 127.0.0.1:8792
    python offline_model.py reckless   # the trial's mistakes, on purpose
    python offline_model.py flaky      # its first few calls fail, as an API down for a while
    export AGENT_MODEL_BASE_URL=http://127.0.0.1:8792 AGENT_MODEL_API_KEY=offline

It answers by fixed rules, in two moods. `careful` picks the first move offered and writes only the facts it is shown. `reckless` makes the trial's
mistakes on purpose: it first asks to order whatever is offered, and writes a per-unit price nobody gave it into
every message. The agent's structure, not the model, keeps either from doing harm."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _answer(prompt: str, reckless: bool, picks: list[int], *, wordy: bool = False) -> dict[str, object]:
    if prompt.startswith("Pick one move"):
        offered = re.findall(r"^- (\S+) — ", prompt, re.MULTILINE)
        if wordy:  # as a real model did: the move's description, not its name
            described = re.findall(r"^- \S+ — (.+)$", prompt, re.MULTILINE)
            return {"move": described[0] if described else None}
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


FLAKY_FAILURES = 3
"""How many of its first calls the flaky mood fails, as a model API down for a while would."""


def _server(reckless: bool, port: int, *, flaky: bool = False, wordy: bool = False) -> ThreadingHTTPServer:
    picks = [0]
    calls = [0]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            calls[0] += 1
            if flaky and calls[0] <= FLAKY_FAILURES:
                self.send_response(500)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            prompt = body["messages"][-1]["content"]
            content = json.dumps(_answer(prompt, reckless, picks, wordy=wordy))
            raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


@contextmanager
def agent_model(*, reckless: bool = False, flaky: bool = False, wordy: bool = False) -> Iterator[str]:
    """A server on a free port for as long as the block runs: its base URL, for AGENT_MODEL_BASE_URL."""
    server = _server(reckless, 0, flaky=flaky, wordy=wordy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


if __name__ == "__main__":
    import sys

    mood = sys.argv[1] if len(sys.argv) > 1 else "careful"
    _server(mood == "reckless", 8792, flaky=mood == "flaky").serve_forever()
