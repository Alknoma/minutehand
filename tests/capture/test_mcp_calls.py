"""An agent's MCP tool calls, recorded as world events: read off the proxy for a server over HTTP, and reported by
`minutehand mcp-relay` for a server on standard input and output."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from minutehand.adapters.proxy.mcp import tool_calls
from minutehand.adapters.proxy.policy import Routing
from minutehand.adapters.proxy.registry import Registry
from minutehand.adapters.proxy.server import Proxy
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.telemetry.receiver import MCP_PATH, MCP_URL_ENV, Receiver
from minutehand.application.run_clock import RunClock
from minutehand.checks.expectations import Expectations
from minutehand.domain.outbound import UnknownHosts
from minutehand.domain.scenario import ToolCalled
from minutehand.domain.world import Actor, EntityKind, EntityRef, Operation, ToolCallSnapshot
from tests.capture.support import Call, by_environment
from tests.checks.world import Log, person, scenario, view
from tests.proxy.upstream import Answer, Authority, model_api

CALL = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/call",
    "params": {"name": "create_issue", "arguments": {"title": "Fix login"}},
}
RESULT = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "created ISSUE-12"}]}}


def test_a_tool_call_is_read_with_the_result_that_answers_its_id_from_json_or_an_event_stream() -> None:
    as_json = tool_calls("mcp.example", json.dumps(CALL), "application/json", json.dumps(RESULT), "application/json")
    stream = f"event: message\ndata: {json.dumps(RESULT)}\n\n"
    as_events = tool_calls("mcp.example", json.dumps(CALL), "application/json", stream, "text/event-stream")
    expected = ToolCallSnapshot(
        server="mcp.example", tool="create_issue", arguments='{"title": "Fix login"}', result="created ISSUE-12"
    )
    assert as_json == as_events == [expected]


def test_an_error_or_a_result_marked_is_error_is_an_error_and_other_methods_are_not_tool_calls() -> None:
    failed = {"jsonrpc": "2.0", "id": 7, "error": {"code": -32602, "message": "unknown tool"}}
    flagged = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "no"}], "isError": True}}
    listing = {"jsonrpc": "2.0", "id": 8, "method": "tools/list"}
    [by_error] = tool_calls("s", json.dumps(CALL), None, json.dumps(failed), None)
    [by_flag] = tool_calls("s", json.dumps([CALL, listing]), None, json.dumps(flagged), None)
    assert by_error.is_error and "unknown tool" in (by_error.result or "")
    assert by_flag.is_error and by_flag.result == "no"
    assert tool_calls("s", json.dumps(listing), None, "{}", None) == []


async def test_a_tool_call_to_an_mcp_server_over_http_is_recorded_as_the_agents_with_its_call(
    tmp_path: Path, authority: Authority
) -> None:
    clock = RunClock(scenario(person("owner")).starts_at)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    registry = Registry()
    registry.discover("minutehand.adapters.providers")
    answer = Answer("text/event-stream", [f"event: message\ndata: {json.dumps(RESULT)}\n\n".encode()])
    async with (
        model_api(authority, answer, host="::1") as server,
        Proxy(
            Routing(registry),
            store,
            clock,
            confdir=tmp_path / "ca",
            upstream_ca=authority.ca_cert,
            capture_unknown=UnknownHosts.ALL,
        ) as proxy,
    ):
        [answered] = await by_environment(
            proxy,
            [Call("POST", f"https://[::1]:{server.port}/mcp", json.dumps(CALL), {"content-type": "application/json"})],
        )
    assert answered.status == 200
    [event] = [e for e in store.events() if e.entity.kind is EntityKind.TOOL_CALL]
    assert event.actor is Actor.AGENT and isinstance(event.after, ToolCallSnapshot)
    assert (event.after.tool, event.after.result) == ("create_issue", "created ISSUE-12")
    [call] = store.calls()
    assert call.first_seq <= event.seq <= call.last_seq, "the event is tied to the call that carried it"


SERVER = """
import json, sys
for line in sys.stdin:
    asked = json.loads(line)
    text = "created " + asked["params"]["arguments"]["title"] if asked.get("method") == "tools/call" else "ok"
    print(json.dumps({"jsonrpc": "2.0", "id": asked["id"], "result": {"content": [{"type": "text", "text": text}]}}), flush=True)
"""


async def test_the_relay_passes_a_stdio_servers_lines_through_and_the_run_records_its_tool_call(tmp_path: Path) -> None:
    clock = RunClock(scenario(person("owner")).starts_at)
    store = SqliteStore(tmp_path / "world.db", "run", clock)
    (tmp_path / "server.py").write_text(SERVER)
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with Receiver(store, clock, host="127.0.0.1", port=0) as receiver:
        env = {**os.environ, MCP_URL_ENV: f"http://127.0.0.1:{receiver.port}{MCP_PATH}"}
        relay = await asyncio.create_subprocess_exec(
            str(Path(sys.executable).parent / "minutehand"),
            "mcp-relay",
            "--name",
            "issues",
            "--",
            sys.executable,
            str(tmp_path / "server.py"),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(
            relay.communicate((json.dumps(listing) + "\n" + json.dumps(CALL) + "\n").encode()), 30
        )

    lines = [json.loads(line) for line in out.decode().splitlines()]
    assert [m["id"] for m in lines] == [1, 7], "both answers reached the agent, in order, unchanged"
    [event] = [e for e in store.events() if e.entity.kind is EntityKind.TOOL_CALL]
    assert isinstance(event.after, ToolCallSnapshot)
    assert (event.after.server, event.after.tool, event.after.result) == ("issues", "create_issue", "created Fix login")


def test_tool_called_is_met_by_a_call_with_the_words_and_not_by_one_that_failed() -> None:
    log = Log()
    ok = ToolCallSnapshot(server="issues", tool="create_issue", arguments='{"title": "Fix login"}', result="ISSUE-12")
    failed = ok.model_copy(update={"is_error": True, "arguments": '{"title": "Fix signup"}'})
    for hours, call in ((1, ok), (2, failed)):
        ref = EntityRef(provider="mcp", kind=EntityKind.TOOL_CALL, external_id=f"issues/{hours}")
        log._add(hours, Actor.AGENT, Operation.CREATE, ref, call, wake=1)
    built = view(scenario(person("owner")), log, [])
    met = ToolCalled(tool="create_issue", mentions=["login"], succeeded=True)
    unmet = ToolCalled(tool="create_issue", mentions=["signup"], succeeded=True)
    scn = built.scenario.model_copy(update={"expect": [met, unmet]})
    kinds = [f.kind.value for f in Expectations().run(built.model_copy(update={"scenario": scn})).findings]
    assert kinds == ["informational", "fail"]
