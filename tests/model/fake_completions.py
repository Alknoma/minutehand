"""A chat-completions server for tests: real HTTP on 127.0.0.1 at an ephemeral port, answering by rule.

Minutehand asks for a structured answer as one tool named for the answer's class (`adapters.model.pydantic_ai`), so
the fake reads the schema from the request's one tool and answers with a call of it.

It records every request as it arrived, so a test can read what the model was told, and answers each with
whatever its rule returns: the JSON text of a structured answer, or an `Answer` carrying a status and a body
of its own. Nothing here is a model; a rule is the test saying what the model would have answered.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from httpx2 import AsyncClient
from pydantic import BaseModel
from pydantic_ai.models import Model as ProviderModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.adapters.model.pydantic_ai import PydanticAIModel
from tests.support.serving import serving

PROVIDER = "openai-chat"
"""pydantic-ai's name for OpenAI's chat completions API, which every OpenAI-compatible endpoint speaks."""


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
    def tool(self) -> dict[str, object]:
        """The one tool the request offers: the structured answer it asks for, named for the answer's class."""
        tools = self.body["tools"]
        assert isinstance(tools, list) and len(tools) == 1, tools
        function = tools[0]["function"]
        assert isinstance(function, dict)
        return function

    @property
    def schema_name(self) -> str:
        return str(self.tool["name"])

    @property
    def schema(self) -> dict[str, object]:
        schema = self.tool["parameters"]
        assert isinstance(schema, dict)
        return schema


@dataclass(frozen=True)
class Answer:
    """A response that is not a well-formed completion: an error status, or a body of the test's choosing."""

    status: int
    body: str


Rule = Callable[[Received], "str | BaseModel | Answer"]


def completion(content: str, model: str, tool: str) -> dict[str, object]:
    """A chat completion calling `tool` with `content` as its arguments: how a structured answer comes back."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "refusal": None,
                    "tool_calls": [
                        {"id": "call_test", "type": "function", "function": {"name": tool, "arguments": content}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@dataclass
class FakeCompletions:
    rule: Rule
    received: list[Received] = field(default_factory=list)
    base_url: str = ""
    checks_facts: bool = False
    """Whether `rule` answers the check of a person's reply against what they know (`FactCheck`). Unless it does,
    every such check finds the reply inside what the person knows, and is kept in `checked` rather than `received`,
    so a rule written for what people say need not know of it."""
    checked: list[Received] = field(default_factory=list)

    def for_schema(self, name: str) -> list[Received]:
        return [r for r in self.received if r.schema_name == name]

    async def _complete(self, request: Request) -> Response:
        if request.url.path != "/v1/chat/completions":
            return JSONResponse({"error": {"message": "not found"}}, status_code=404)
        body = json.loads(await request.body())
        assert isinstance(body, dict)
        authorization = request.headers["authorization"] if "authorization" in request.headers else ""
        received = Received(authorization=authorization, body=body)
        if received.schema_name == "FactCheck" and not self.checks_facts:
            self.checked.append(received)
            supported = '{"supported": true, "unsupported": [], "rationale": "It says only what they know."}'
            return JSONResponse(completion(supported, received.model, received.schema_name))
        self.received.append(received)
        answered = self.rule(received)
        if isinstance(answered, Answer):
            return Response(answered.body, status_code=answered.status, media_type="application/json")
        content = answered.model_dump_json() if isinstance(answered, BaseModel) else answered
        return JSONResponse(completion(content, received.model, received.schema_name))

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/v1/chat/completions", self._complete, methods=["POST"])])


def model_over(base_url: str, *, model_id: str, key: str = "sk-test-never-shown") -> PydanticAIModel:
    """Minutehand's model port over the chat completions at `base_url`: pydantic-ai's OpenAI chat model with a provider
    given `key` by hand, as a test may, over the HTTP client of each call."""

    def resolve(name: str, client: AsyncClient) -> ProviderModel:
        _, _, named = name.partition(":")
        return OpenAIChatModel(named, provider=OpenAIProvider(base_url=base_url, api_key=key, http_client=client))

    return PydanticAIModel(resolve, provider=PROVIDER, model_id=model_id)


@asynccontextmanager
async def fake_completions(rule: Rule, *, checks_facts: bool = False) -> AsyncIterator[FakeCompletions]:
    fake = FakeCompletions(rule=rule, checks_facts=checks_facts)
    async with serving(fake.app()) as base:
        fake.base_url = f"{base}/v1"
        yield fake
