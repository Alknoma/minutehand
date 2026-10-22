"""Minutehand's own model over pydantic-ai, against stubs of four providers' APIs on this machine.

Minutehand holds no key. Each test's environment holds `MINUTEHAND_MODEL`, the provider's own key variable with a
sentinel, and the provider's own base-URL variable pointed at the stub; the stub then sees the sentinel the way that
provider's client sends it, and nothing of Minutehand's named it.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from minutehand.adapters.model import pydantic_ai
from minutehand.adapters.model.pydantic_ai import MODEL_VARIABLE, from_environment
from minutehand.application.refusals import RunRefused
from minutehand.domain.conversation import ModelMessage, Speaker
from minutehand.domain.scenario import Model
from minutehand.ports.model import ModelFailed
from tests.support.serving import serving

SENTINEL = "sentinel-key-0123456789abcdef-never-real"
DEAD_PROXY = "http://127.0.0.1:9"
"""A proxy nothing listens at: a client that read the proxy variables would fail to connect."""


class Forecast(Model):
    sunny: bool
    note: str | None


ASKED = [
    ModelMessage(speaker=Speaker.ASKER, text="Will it be sunny?"),
    ModelMessage(speaker=Speaker.MODEL, text="Where?"),
    ModelMessage(speaker=Speaker.ASKER, text="Lisbon."),
]
VALID = json.dumps({"sunny": True, "note": None})
MALFORMED = json.dumps({"sunny": "maybe"})

Shape = Callable[[dict[str, object], str], "dict[str, object] | str"]
"""A provider's answer to a request body: a call of the tool `Forecast` with the given arguments, as JSON, or as the
text of a server-sent event stream."""


def chat_completion(body: dict[str, object], arguments: str) -> dict[str, object]:
    """OpenAI's chat completions, which every OpenAI-compatible endpoint speaks."""
    call = {"id": "call_1", "type": "function", "function": {"name": "Forecast", "arguments": arguments}}
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def response(body: dict[str, object], arguments: str) -> dict[str, object]:
    """OpenAI's Responses API, which `openai:` names."""
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Forecast", "arguments": arguments}
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": body["model"],
        "status": "completed",
        "output": [{**call, "status": "completed"}],
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    }


def anthropic_message(body: dict[str, object], arguments: str) -> dict[str, object] | str:
    """Anthropic's Messages API: the answer as a `tool_use` block, as one message or, when the client asked for a
    stream (pydantic-ai's Anthropic model does), as the server-sent events of one."""
    block = {"type": "tool_use", "id": "toolu_1", "name": "Forecast", "input": json.loads(arguments)}
    message: dict[str, object] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": body["model"],
        "content": [block],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    if not (body.get("stream")):
        return message
    events: list[tuple[str, dict[str, object]]] = [
        ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {**block, "input": {}}}),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": arguments},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(event)}\n\n" for name, event in events)


def gemini_content(body: dict[str, object], arguments: str) -> dict[str, object]:
    """The Gemini API's `generateContent`: the answer as a `functionCall` part."""
    part = {"functionCall": {"name": "Forecast", "args": json.loads(arguments)}}
    return {
        "candidates": [{"content": {"role": "model", "parts": [part]}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
    }


@dataclass
class Stub:
    """A provider's API on this machine: keeps each request's headers, path and body, and answers the nth request
    with a `Forecast` call carrying `arguments[n]` (the last, once they run out)."""

    shape: Shape
    arguments: list[str] = field(default_factory=lambda: [VALID])
    headers: list[dict[str, str]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    bodies: list[dict[str, object]] = field(default_factory=list)

    async def _answer(self, request: Request) -> Response:
        body = json.loads(await request.body())
        assert isinstance(body, dict)
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        self.paths.append(request.url.path)
        self.bodies.append(body)
        arguments = self.arguments[min(len(self.bodies) - 1, len(self.arguments) - 1)]
        answered = self.shape(body, arguments)
        if isinstance(answered, str):
            return Response(answered, media_type="text/event-stream")
        return JSONResponse(answered)

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/{rest:path}", self._answer, methods=["POST"])])


@asynccontextmanager
async def stubbed(shape: Shape, *arguments: str) -> AsyncIterator[tuple[Stub, str]]:
    stub = Stub(shape, list(arguments) or [VALID])
    async with serving(stub.app()) as base:
        yield stub, base


@dataclass(frozen=True)
class Provided:
    """One provider as an adopter configures it: Minutehand's name for the model, the provider's own key and
    base-URL variables, the API shape its stub answers in, and how its client sends the key."""

    named: str
    key_variable: str
    url_variable: str
    shape: Shape
    url: Callable[[str], str]
    path_end: str
    sent_as: Callable[[dict[str, str]], str]


PROVIDERS = [
    Provided(
        "openai:gpt-5-mini",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        response,
        lambda base: f"{base}/v1",
        "/v1/responses",
        lambda headers: headers["authorization"],
    ),
    Provided(
        "openai-chat:any-compatible-model",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        chat_completion,
        lambda base: f"{base}/v1",
        "/v1/chat/completions",
        lambda headers: headers["authorization"],
    ),
    Provided(
        "anthropic:claude-sonnet-5-5",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        anthropic_message,
        lambda base: base,
        "/v1/messages",
        lambda headers: headers["x-api-key"],
    ),
    Provided(
        "google:gemini-2.5-pro",
        "GOOGLE_API_KEY",
        "GOOGLE_GEMINI_BASE_URL",
        gemini_content,
        lambda base: base,
        ":generateContent",
        lambda headers: headers["x-goog-api-key"],
    ),
]
BEARER = {"openai:gpt-5-mini": f"Bearer {SENTINEL}", "openai-chat:any-compatible-model": f"Bearer {SENTINEL}"}
"""How each provider's client sends the key when it is not the bare value in its own header."""


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, str]], None]:
    """Replaces this process's environment with exactly the given variables (and the paths the interpreter needs),
    so a provider's client finds nothing but what the test gives it."""

    def only(variables: dict[str, str]) -> None:
        kept = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if name in os.environ}
        monkeypatch.setattr(os, "environ", {**kept, **variables})

    return only


@pytest.mark.parametrize("provided", PROVIDERS, ids=lambda p: p.named)
async def test_an_environment_holding_only_the_providers_own_key_configures_the_model_and_the_provider_sends_it(
    provided: Provided, environment: Callable[[dict[str, str]], None]
) -> None:
    async with stubbed(provided.shape) as (stub, base):
        environment(
            {MODEL_VARIABLE: provided.named, provided.key_variable: SENTINEL, provided.url_variable: provided.url(base)}
        )
        model = from_environment()
        assert model is not None and f"{model.provider}:{model.model_id}" == provided.named
        answered = await model.answer("You forecast.", ASKED, Forecast, temperature=0.2)

    assert answered.answer == Forecast(sunny=True, note=None)
    assert answered.model == provided.named.partition(":")[2]
    assert answered.input_tokens == 3 and answered.output_tokens == 2
    [headers] = stub.headers
    assert provided.sent_as(headers) == BEARER.get(provided.named, SENTINEL)
    [path] = stub.paths
    assert path.endswith(provided.path_end), path


async def test_the_models_calls_ignore_the_proxy_variables_of_the_environment(
    environment: Callable[[dict[str, str]], None],
) -> None:
    """The agent's environment carries HTTPS_PROXY pointing at Minutehand's own proxy; Minutehand's model calls are
    not the agent's traffic and read no proxy variable."""
    async with stubbed(chat_completion) as (stub, base):
        environment(
            {
                MODEL_VARIABLE: "openai-chat:people",
                "OPENAI_API_KEY": SENTINEL,
                "OPENAI_BASE_URL": f"{base}/v1",
                "HTTPS_PROXY": DEAD_PROXY,
                "HTTP_PROXY": DEAD_PROXY,
                "ALL_PROXY": DEAD_PROXY,
                "https_proxy": DEAD_PROXY,
                "http_proxy": DEAD_PROXY,
                "all_proxy": DEAD_PROXY,
            }
        )
        model = from_environment()
        assert model is not None
        answered = await model.answer("You forecast.", ASKED, Forecast)
    assert answered.answer == Forecast(sunny=True, note=None) and len(stub.bodies) == 1


def test_minutehand_model_api_key_configures_nothing_and_a_provider_without_its_own_key_is_refused_naming_it(
    environment: Callable[[dict[str, str]], None],
) -> None:
    environment({MODEL_VARIABLE: "openai-chat:people", "MINUTEHAND_MODEL_API_KEY": SENTINEL})
    with pytest.raises(RunRefused) as refused:
        from_environment()
    said = str(refused.value)
    assert "the openai-chat provider refused its configuration" in said
    assert "OPENAI_API_KEY" in said, "the provider's own words say what it wants"
    assert SENTINEL not in said


def test_nothing_named_is_no_model_and_a_name_that_is_not_provider_colon_model_is_refused(
    environment: Callable[[dict[str, str]], None],
) -> None:
    environment({})
    assert from_environment() is None
    environment({MODEL_VARIABLE: "gpt-5-mini"})
    with pytest.raises(RunRefused, match="`provider:model`"):
        from_environment()


def test_a_provider_pydantic_ai_does_not_name_is_refused(environment: Callable[[dict[str, str]], None]) -> None:
    environment({MODEL_VARIABLE: "nobody:some-model"})
    with pytest.raises(RunRefused, match="nobody"):
        from_environment()


async def test_a_malformed_answer_goes_back_once_and_a_second_raises(
    environment: Callable[[dict[str, str]], None],
) -> None:
    async with stubbed(chat_completion, MALFORMED, VALID) as (stub, base):
        environment({MODEL_VARIABLE: "openai-chat:people", "OPENAI_API_KEY": SENTINEL, "OPENAI_BASE_URL": f"{base}/v1"})
        model = from_environment()
        assert model is not None
        answered = await model.answer("You forecast.", ASKED, Forecast)
        assert answered.answer == Forecast(sunny=True, note=None)
        assert len(stub.bodies) == 2
        first, second = (b["messages"] for b in stub.bodies)
        assert isinstance(first, list) and isinstance(second, list) and len(second) > len(first), (
            "the second request carries the first answer and what was wrong with it"
        )

    async with stubbed(chat_completion, MALFORMED, MALFORMED, VALID) as (stub, base):
        environment({MODEL_VARIABLE: "openai-chat:people", "OPENAI_API_KEY": SENTINEL, "OPENAI_BASE_URL": f"{base}/v1"})
        model = from_environment()
        assert model is not None
        with pytest.raises(ModelFailed, match="Forecast"):
            await model.answer("You forecast.", ASKED, Forecast)
        assert len(stub.bodies) == 2, "sent back once, then given up on"


async def test_an_error_status_is_a_failure_naming_the_provider_and_the_status(
    environment: Callable[[dict[str, str]], None],
) -> None:
    def down(body: dict[str, object], arguments: str) -> dict[str, object] | str:
        raise AssertionError("never answered: the route below refuses")

    stub = Stub(down)

    async def refuse(request: Request) -> JSONResponse:
        return JSONResponse({"error": {"message": "down"}}, status_code=503)

    app = Starlette(routes=[Route("/{rest:path}", refuse, methods=["POST"])])
    async with serving(app) as base:
        environment({MODEL_VARIABLE: "openai-chat:people", "OPENAI_API_KEY": SENTINEL, "OPENAI_BASE_URL": f"{base}/v1"})
        model = from_environment()
        assert model is not None
        with pytest.raises(ModelFailed, match="openai-chat answered 503"):
            await model.answer("You forecast.", ASKED, Forecast)
    assert stub.bodies == []


def test_the_missing_package_refusal_names_the_extra_to_install() -> None:
    """pydantic-ai's own ImportError names its extra; the refusal names Minutehand's of the same name."""
    found = pydantic_ai._EXTRA.search(  # pyright: ignore[reportPrivateUsage]
        "Please install `openai` to use the OpenAI model, you can use the `openai` optional group — "
        '`pip install "pydantic-ai-slim[openai]"`'
    )
    assert found is not None and found.group(1) == "openai"
