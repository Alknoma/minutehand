"""`ports.model.Model` over an OpenAI-compatible chat-completions API, with JSON-schema structured output.

Configured from the environment:

    MINUTEHAND_MODEL_BASE_URL   default https://api.openai.com/v1
    MINUTEHAND_MODEL_API_KEY    sent as a bearer token; never logged, stored or put in an exception
    MINUTEHAND_MODEL            the model a request names unless the caller names another

The client ignores every proxy variable in the environment (`trust_env=False`): Minutehand's own proxy stands
between the agent and the world, and Minutehand's model calls are not the agent's traffic.

An answer that does not validate against the schema is sent back once with the validation error; a second
invalid answer raises `ModelFailed`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from minutehand.application.refusals import RunRefused
from minutehand.domain.conversation import ModelMessage, Speaker
from minutehand.domain.scenario import Model
from minutehand.ports.model import Answered, AnswerT, ModelFailed

BASE_URL_VARIABLE = "MINUTEHAND_MODEL_BASE_URL"
API_KEY_VARIABLE = "MINUTEHAND_MODEL_API_KEY"
MODEL_VARIABLE = "MINUTEHAND_MODEL"
VARIABLES = (BASE_URL_VARIABLE, API_KEY_VARIABLE, MODEL_VARIABLE)
DEFAULT_BASE_URL = "https://api.openai.com/v1"

TIMEOUT_SECONDS = 120.0
_ERROR_CHARS = 300


# -- the API's own JSON -------------------------------------------------------------------------------------


class _Said(Model):
    role: Literal["system", "user", "assistant"]
    content: str


class _JsonSchema(Model):
    name: str
    schema_: dict[str, JsonValue] = Field(serialization_alias="schema")
    strict: bool = True


class _ResponseFormat(Model):
    type: Literal["json_schema"] = "json_schema"
    json_schema: _JsonSchema


class _Request(Model):
    model: str
    messages: list[_Said]
    response_format: _ResponseFormat
    temperature: float | None = None


class _Answered(BaseModel):
    """The API's response is someone else's wire format and grows fields; only what is read is declared."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class _Reply(_Answered):
    content: str | None = None
    refusal: str | None = None


class _Choice(_Answered):
    message: _Reply
    finish_reason: str | None = None


class _PromptDetails(_Answered):
    cached_tokens: int | None = None


class _Usage(_Answered):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_tokens_details: _PromptDetails | None = None


class _Completion(_Answered):
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage | None = None


def _role(speaker: Speaker) -> Literal["user", "assistant"]:
    match speaker:
        case Speaker.ASKER:
            return "user"
        case Speaker.MODEL:
            return "assistant"


def strict_schema(answer: type[BaseModel]) -> dict[str, JsonValue]:
    """The answer's JSON schema as strict structured output accepts it: every property required, no other
    property allowed, and no defaults."""
    schema = answer.model_json_schema()
    _tighten(schema)
    return schema


def _tighten(node: JsonValue) -> None:
    if isinstance(node, list):
        for item in node:
            _tighten(item)
        return
    if not isinstance(node, dict):
        return
    node.pop("default", None)
    properties = node["properties"] if "properties" in node else None
    if isinstance(properties, dict):
        node["required"] = list(properties)
        node["additionalProperties"] = False
    for value in node.values():
        _tighten(value)


class _Spent:
    """The tokens a request and its one retry cost, summed as the service counted them."""

    def __init__(self) -> None:
        self.input: int | None = None
        self.output: int | None = None
        self.cached: int | None = None

    def add(self, usage: _Usage | None) -> None:
        """`prompt_tokens` counts every input token, the cached ones `prompt_tokens_details.cached_tokens` names
        among them (https://platform.openai.com/docs/guides/prompt-caching)."""
        if usage is None:
            return
        if usage.prompt_tokens is not None:
            self.input = (self.input or 0) + usage.prompt_tokens
        if usage.completion_tokens is not None:
            self.output = (self.output or 0) + usage.completion_tokens
        if usage.prompt_tokens_details is not None and usage.prompt_tokens_details.cached_tokens is not None:
            self.cached = (self.cached or 0) + usage.prompt_tokens_details.cached_tokens

    def answered(self, answer: AnswerT, model: str) -> Answered[AnswerT]:
        return Answered(
            answer=answer,
            model=model,
            input_tokens=self.input,
            output_tokens=self.output,
            cache_read_tokens=self.cached,
        )


# -- the client ---------------------------------------------------------------------------------------------


class OpenAICompatible:
    def __init__(self, *, base_url: str, api_key: str, model_id: str, timeout: float = TIMEOUT_SECONDS) -> None:
        if not api_key:
            raise RunRefused(f"{API_KEY_VARIABLE} is empty")
        self.model_id = model_id
        self._url = base_url.rstrip("/") + "/chat/completions"
        self.__key = api_key
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"OpenAICompatible({self._url!r}, model_id={self.model_id!r})"

    async def answer(
        self,
        system: str,
        messages: Sequence[ModelMessage],
        answer: type[AnswerT],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> Answered[AnswerT]:
        said = [_Said(role="system", content=system)]
        said += [_Said(role=_role(m.speaker), content=m.text) for m in messages]
        response_format = _ResponseFormat(json_schema=_JsonSchema(name=answer.__name__, schema_=strict_schema(answer)))
        named = model or self.model_id
        spent = _Spent()
        content = await self._complete(
            _Request(model=named, messages=said, response_format=response_format, temperature=temperature), spent
        )
        try:
            return spent.answered(answer.model_validate_json(content), named)
        except ValidationError as first:
            said += [
                _Said(role="assistant", content=content),
                _Said(
                    role="user",
                    content=(
                        "That answer does not match the required schema:\n"
                        f"{_errors(first)}\n"
                        "Answer again, with JSON that matches the schema exactly."
                    ),
                ),
            ]
        content = await self._complete(
            _Request(model=named, messages=said, response_format=response_format, temperature=temperature), spent
        )
        try:
            return spent.answered(answer.model_validate_json(content), named)
        except ValidationError as second:
            raise ModelFailed(
                f"{named} twice answered with something that is not a {answer.__name__}:\n{_errors(second)}"
            ) from None

    async def _complete(self, request: _Request, spent: _Spent) -> str:
        body = request.model_dump_json(by_alias=True, exclude_none=True)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
                response = await client.post(
                    self._url,
                    content=body,
                    headers={"Authorization": f"Bearer {self.__key}", "Content-Type": "application/json"},
                )
        except httpx.HTTPError as e:
            raise ModelFailed(f"{self._url} could not be reached: {type(e).__name__}: {self._scrub(str(e))}") from None
        if not response.is_success:
            raise ModelFailed(f"{self._url} answered {response.status_code}: {self._scrub(response.text)}")
        try:
            completion = _Completion.model_validate_json(response.content)
        except ValidationError:
            raise ModelFailed(
                f"{self._url} answered with something that is not a chat completion: {self._scrub(response.text)}"
            ) from None
        spent.add(completion.usage)
        reply = completion.choices[0].message
        if reply.refusal is not None:
            raise ModelFailed(f"{request.model} refused: {self._scrub(reply.refusal)}")
        if reply.content is None:
            raise ModelFailed(f"{request.model} answered with no content")
        return reply.content

    def _scrub(self, text: str) -> str:
        """Text from the far end, cut short and with the key taken out: some services echo a key they refuse."""
        return text[:_ERROR_CHARS].replace(self.__key, "[the API key]")


def _errors(error: ValidationError) -> str:
    """The validation errors without the input they were raised on, which is the model's own text."""
    return "\n".join(f"- {'.'.join(str(p) for p in e['loc']) or '(the answer)'}: {e['msg']}" for e in error.errors())


def from_environment(environ: Mapping[str, str] = os.environ) -> OpenAICompatible | None:
    """The model the environment configures; None when neither the key nor the model is set.

    Half a configuration is refused, naming what is missing, rather than read as none.
    """
    key = environ[API_KEY_VARIABLE] if API_KEY_VARIABLE in environ else ""
    model = environ[MODEL_VARIABLE] if MODEL_VARIABLE in environ else ""
    if not key and not model:
        return None
    if not key or not model:
        missing = API_KEY_VARIABLE if not key else MODEL_VARIABLE
        raise RunRefused(f"a model is half configured: {missing} is not set")
    base_url = environ[BASE_URL_VARIABLE] if BASE_URL_VARIABLE in environ else DEFAULT_BASE_URL
    return OpenAICompatible(base_url=base_url or DEFAULT_BASE_URL, api_key=key, model_id=model)
