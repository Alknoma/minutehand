"""MCP's own wire format, read for the tool calls in it: JSON-RPC 2.0 `tools/call` requests and their results,
answered as one JSON body or as server-sent events (the streamable HTTP transport), or relayed line by line from a
server on standard input and output (`minutehand mcp-relay`).

https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
"""

from __future__ import annotations

import json
from typing import cast

from minutehand.domain.world import ToolCallSnapshot

JSON_OBJECT = dict[str, object]


def _messages(text: str | None, content_type: str | None) -> list[JSON_OBJECT]:
    """The JSON-RPC messages in a body: one object, a batch, or the `data:` lines of an event stream."""
    if not text:
        return []
    chunks = (
        [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if content_type is not None and content_type.startswith("text/event-stream")
        else [text]
    )
    found: list[JSON_OBJECT] = []
    for chunk in chunks:
        try:
            parsed: object = json.loads(chunk)
        except ValueError:
            continue
        for one in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(one, dict) and cast(JSON_OBJECT, one).get("jsonrpc") == "2.0":
                found.append(cast(JSON_OBJECT, one))
    return found


def _text(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    content = cast(JSON_OBJECT, result).get("content")
    if not isinstance(content, list):
        return json.dumps(result, sort_keys=True)
    parts = [str(cast(JSON_OBJECT, c).get("text")) for c in content if isinstance(c, dict) and "text" in c]
    return "\n".join(parts) if parts else json.dumps(result, sort_keys=True)


def tool_calls(
    server: str,
    request: str | None,
    request_type: str | None,
    response: str | None,
    response_type: str | None,
) -> list[ToolCallSnapshot]:
    """Every `tools/call` in the request, each with the response message that answers its id, if any."""
    answers = {json.dumps(m.get("id")): m for m in _messages(response, response_type) if "id" in m}
    found: list[ToolCallSnapshot] = []
    for message in _messages(request, request_type):
        if message.get("method") != "tools/call":
            continue
        params = message.get("params")
        params = cast(JSON_OBJECT, params) if isinstance(params, dict) else {}
        answer = answers.get(json.dumps(message.get("id")))
        result = answer.get("result") if answer is not None else None
        error = answer.get("error") if answer is not None else None
        flagged = isinstance(result, dict) and cast(JSON_OBJECT, result).get("isError") is True
        found.append(
            ToolCallSnapshot(
                server=server,
                tool=str(params.get("name", "")),
                arguments=json.dumps(params.get("arguments", {}), sort_keys=True),
                result=_text(result) if result is not None else (json.dumps(error) if error is not None else None),
                is_error=error is not None or flagged,
            )
        )
    return found
