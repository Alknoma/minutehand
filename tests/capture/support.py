"""What the capture tests share: a proxy that captures, and a client configured by nothing but the environment
Minutehand hands an agent.

The "real host" is `upstream.model_api` on `::1`, a local HTTPS server with a CA of its own: the handed-out
environment sends `localhost` and `127.0.0.1` direct, so a real host the proxy must not be bypassed for has to
be some other address of this machine. The client is stdlib `urllib` in a process of its own: what an agent's
code would use, reading HTTPS_PROXY, NO_PROXY and SSL_CERT_FILE and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from minutehand import session
from minutehand.adapters.proxy.server import Proxy
from tests.support.stored import everything

START = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
V6 = "::1"

SECRET_HEADER = "hdr-secret-7f3a"
SECRET_COOKIE = "cookie-secret-91be"
SECRET_API_KEY = "x-api-key-secret-22c4"
SECRET_QUERY = "query-secret-5d10"
SECRET_BODY = "body-secret-a8e2"
SECRET_DECLARED = "declared-secret-3c77"
SECRETS = (SECRET_HEADER, SECRET_COOKIE, SECRET_API_KEY, SECRET_QUERY, SECRET_BODY, SECRET_DECLARED)

_CLIENT = """
import json, sys, urllib.request, urllib.error
answers = []
for call in json.loads(sys.argv[1]):
    request = urllib.request.Request(
        call["url"], data=call["body"].encode() if call["body"] is not None else None,
        headers=call["headers"], method=call["method"],
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:
            answers.append({"status": answer.status, "body": answer.read().decode(), "headers": dict(answer.headers)})
    except urllib.error.HTTPError as refused:
        answers.append({"status": refused.code, "body": refused.read().decode(), "headers": dict(refused.headers)})
print(json.dumps(answers))
"""


@dataclass(frozen=True)
class Call:
    method: str
    url: str
    body: str | None = None
    headers: dict[str, str] | None = None


@dataclass(frozen=True)
class Answered:
    status: int
    body: str
    headers: dict[str, str]

    def header(self, name: str) -> str | None:
        found = [v for k, v in self.headers.items() if k.lower() == name.lower()]
        return found[0] if found else None


def handed_out(proxy: Proxy) -> dict[str, str]:
    """This process's environment without its own proxies, plus exactly what `minutehand run` hands an agent."""
    own = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy") and k != "SSL_CERT_DIR"}
    return own | session.agent_environment(session.Listen(), proxy.port, proxy.ca_bundle, {}, telemetry_port=None)


async def by_environment(proxy: Proxy, calls: Sequence[Call]) -> list[Answered]:
    """Each call made in order by a separate Python process configured only by the handed-out environment."""
    plan = json.dumps([{"method": c.method, "url": c.url, "body": c.body, "headers": c.headers or {}} for c in calls])
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CLIENT,
        plan,
        env=handed_out(proxy),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(child.communicate(), timeout=40)
    assert child.returncode == 0, err.decode()
    return [Answered(a["status"], a["body"], a["headers"]) for a in json.loads(out)]


def stored_bytes(directory: Path) -> bytes:
    """Every byte under a directory: the world file, its write-ahead log, anything written beside them, and every
    stored body and snapshot file decompressed."""
    return everything(directory)
