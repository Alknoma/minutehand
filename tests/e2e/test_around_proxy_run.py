"""A whole run of an agent that makes one of its Slack calls with a client that ignores the proxy: its own telemetry
names the call, the proxy never saw it, and the run fails on it. The same agent with every call through the proxy
gets no such finding.

The real slack.com is stood in for by a listener on this machine that answers anything (`AROUND_URL`): the agent's
client reaches it directly, as one that ignores `HTTPS_PROXY` reaches the real service."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from minutehand import session
from minutehand.domain.checks import FindingKind
from minutehand.domain.run import VerdictKind
from minutehand.domain.scenario import Silent
from tests.e2e.support import agent_under_test, scenario


class _RealService(BaseHTTPRequestHandler):
    reached: int = 0

    def do_POST(self) -> None:
        type(self).reached += 1
        self.rfile.read(int(self.headers["content-length"] or 0))
        body = json.dumps({"ok": False, "error": "invalid_auth"}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def real_slack() -> Iterator[str]:
    _RealService.reached = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RealService)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/api/chat.postMessage"
    finally:
        server.shutdown()


async def test_a_call_the_agents_telemetry_names_and_the_proxy_never_saw_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_slack: str
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", tracing=True)
    monkeypatch.setenv("AROUND_URL", real_slack)
    [outcome] = await session.play(
        scenario(Silent()), launched.agent, state=tmp_path / "state", command=launched.command
    )
    assert _RealService.reached == 1
    [around] = [f for f in outcome.result.findings if f.check == "around_proxy"]
    assert around.kind is FindingKind.FAIL
    assert "1 call to slack.com (slack) and the proxy saw 3: 1 went around Minutehand" in around.message
    assert "POST https://slack.com/api/chat.postMessage" in around.message
    assert "NODE_USE_ENV_PROXY=1" in around.message
    assert outcome.result.verdict.kind is VerdictKind.FAILED


async def test_an_agent_whose_every_call_went_through_the_proxy_is_not_held_to_have_gone_around_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = agent_under_test(tmp_path, monkeypatch, "forgetful", tracing=True)
    [outcome] = await session.play(
        scenario(Silent()), launched.agent, state=tmp_path / "state", command=launched.command
    )
    assert [f for f in outcome.result.findings if f.check == "around_proxy"] == []
