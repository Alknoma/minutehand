"""A Messages API server for tests, shaped as Anthropic documents it (https://docs.anthropic.com/en/api/messages):
real HTTP on 127.0.0.1, answering by rule. A rule returns the tool input the model would have given, or an `Answer` of
the test's own; every request is kept as it arrived. Nothing here is a model."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.model.fake_completions import Answer
from tests.orchestrator.world import serving


@dataclass(frozen=True)
class Asked:
    headers: dict[str, str]
    body: dict[str, object]


Rule = Callable[[Asked], "dict[str, object] | Answer"]

USAGE = {"input_tokens": 12, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 2000, "output_tokens": 30}


def message(model: str, tool: str, given: dict[str, object]) -> dict[str, object]:
    """A response whose one content block calls `tool` with `given`, as a forced tool choice answers."""
    return {
        "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "tool_use", "id": "toolu_01A09q90qw90lq917835lq9", "name": tool, "input": given}],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": USAGE,
    }


@dataclass
class FakeMessages:
    rule: Rule
    asked: list[Asked] = field(default_factory=list)
    base_url: str = ""

    async def _messages(self, request: Request) -> Response:
        body = json.loads(await request.body())
        assert isinstance(body, dict)
        asked = Asked(headers=dict(request.headers), body=body)
        self.asked.append(asked)
        answered = self.rule(asked)
        if isinstance(answered, Answer):
            return Response(answered.body, status_code=answered.status, media_type="application/json")
        tool = body["tool_choice"]
        assert isinstance(tool, dict)
        return JSONResponse(message(str(body["model"]), str(tool["name"]), answered))


@asynccontextmanager
async def fake_messages(rule: Rule) -> AsyncIterator[FakeMessages]:
    fake = FakeMessages(rule=rule)
    async with serving(Starlette(routes=[Route("/v1/messages", fake._messages, methods=["POST"])])) as base:
        fake.base_url = base
        yield fake
