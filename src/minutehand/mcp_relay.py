"""`minutehand mcp-relay --name NAME -- <server command>`: an MCP server on standard input and output, run by the
relay, which passes every line between the agent and the server unchanged and reports each tool call, with the
response that answered it, to the run (MINUTEHAND_MCP_URL, which Minutehand hands the agent). The agent's MCP
configuration names the relay in place of the server's command; nothing else changes. Without MINUTEHAND_MCP_URL
(the agent run outside Minutehand) it only relays.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
from typing import IO

from minutehand.adapters.telemetry.receiver import MCP_URL_ENV


def _report(url: str, server: str, request: str, response: str) -> None:
    body = json.dumps({"server": server, "request": request, "response": response}).encode()
    sent = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(sent, timeout=10).close()
    except OSError as e:
        print(f"minutehand mcp-relay: could not report a tool call to {url}: {e}", file=sys.stderr, flush=True)


def _ids(line: str, method: str | None) -> list[str]:
    """The ids of the JSON-RPC messages on one line, of one method when `method` is given."""
    try:
        parsed: object = json.loads(line)
    except ValueError:
        return []
    found = []
    for one in parsed if isinstance(parsed, list) else [parsed]:
        if isinstance(one, dict) and "id" in one and (method is None or one.get("method") == method):
            found.append(json.dumps(one["id"]))
    return found


def relay(name: str, command: list[str], url: str | None, stdin: IO[str], stdout: IO[str]) -> int:
    server = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    assert server.stdin is not None and server.stdout is not None
    asked: dict[str, str] = {}
    lock = threading.Lock()

    def to_server() -> None:
        assert server.stdin is not None
        for line in stdin:
            with lock:
                for key in _ids(line, "tools/call"):
                    asked[key] = line
            server.stdin.write(line)
            server.stdin.flush()
        server.stdin.close()

    threading.Thread(target=to_server, daemon=True).start()
    for line in server.stdout:
        stdout.write(line)
        stdout.flush()
        if url is None:
            continue
        with lock:
            answered = [(asked.pop(key), line) for key in _ids(line, None) if key in asked]
        for request, response in answered:
            _report(url, name, request, response)
    return server.wait()


def main(name: str, command: list[str]) -> int:
    url = os.environ[MCP_URL_ENV] if MCP_URL_ENV in os.environ else None
    return relay(name, command, url, sys.stdin, sys.stdout)
