"""A chat-completions server for tests: real HTTP on 127.0.0.1 at an ephemeral port, answering by rule.

It records every request as it arrived, so a test can read what the model was told, and answers each with
whatever its rule returns: the JSON text of a structured answer, or an `Answer` carrying a status and a body
of its own. Nothing here is a model; a rule is the test saying what the model would have answered.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.orchestrator.world import serving


@dataclass(frozen=True)
class Received:
    """One request as the server read it."""

    authorization: str
    body: dict[str, object]

    @property
    def model(self) -> str:
        return str(self.body["model"])

    @property
    def temperature(self) -> object:
        return self.body["temperature"] if "temperature" in self.body else None

    @property
    def said(self) -> list[tuple[str, str]]:
        """(role, content) for every message, the system text first."""
        messages = self.body["messages"]
        assert isinstance(messages, list)
        out: list[tuple[str, str]] = []
        for m in messages:
            assert isinstance(m, dict)
            out.append((str(m["role"]), str(m["content"])))
        return out

    @property
    def system(self) -> str:
        role, content = self.said[0]
        assert role == "system"
        return content

    @property
    def last(self) -> str:
        return self.said[-1][1]

    @property
    def asked(self) -> str:
        """The message a person is asked to answer, as the transcript Minutehand sends shows it last."""
        return self.last.rstrip().splitlines()[-1].split("] They: ", 1)[-1]

    @property
    def schema_name(self) -> str:
        response_format = self.body["response_format"]
        assert isinstance(response_format, dict)
        return str(response_format["json_schema"]["name"])

    @property
    def schema(self) -> dict[str, object]:
        response_format = self.body["response_format"]
        assert isinstance(response_format, dict)
        schema = response_format["json_schema"]["schema"]
        assert isinstance(schema, dict)
        return schema


@dataclass(frozen=True)
class Answer:
    """A response that is not a well-formed completion: an error status, or a body of the test's choosing."""

    status: int
    body: str


Rule = Callable[[Received], "str | BaseModel | Answer"]


def completion(content: str, model: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@dataclass
class FakeCompletions:
    rule: Rule
    received: list[Received] = field(default_factory=list)
    base_url: str = ""

    def for_schema(self, name: str) -> list[Received]:
        return [r for r in self.received if r.schema_name == name]

    async def _complete(self, request: Request) -> Response:
        if request.url.path != "/v1/chat/completions":
            return JSONResponse({"error": {"message": "not found"}}, status_code=404)
        body = json.loads(await request.body())
        assert isinstance(body, dict)
        authorization = request.headers["authorization"] if "authorization" in request.headers else ""
        received = Received(authorization=authorization, body=body)
        self.received.append(received)
        answered = self.rule(received)
        if isinstance(answered, Answer):
            return Response(answered.body, status_code=answered.status, media_type="application/json")
        content = answered.model_dump_json() if isinstance(answered, BaseModel) else answered
        return JSONResponse(completion(content, received.model))

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/v1/chat/completions", self._complete, methods=["POST"])])


@asynccontextmanager
async def fake_completions(rule: Rule) -> AsyncIterator[FakeCompletions]:
    fake = FakeCompletions(rule=rule)
    async with serving(fake.app()) as base:
        fake.base_url = f"{base}/v1"
        yield fake
