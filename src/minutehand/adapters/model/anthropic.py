"""`ports.model.Model` over Anthropic's Messages API, its structured answer taken as the input of one forced tool.

    POST {base}/v1/messages   x-api-key, anthropic-version: 2023-06-01
    {"model", "max_tokens", "system", "messages", "tools": [{"name", "description", "input_schema"}],
     "tool_choice": {"type": "tool", "name": ...}, "temperature"?}

The answer's JSON schema is the one tool's `input_schema`, and `tool_choice` names it, so the model answers with a
`tool_use` block whose `input` is the answer (https://docs.anthropic.com/en/docs/build-with-claude/tool-use, "Forcing
tool use" and "JSON mode"). An input that does not validate is sent back once, as Anthropic's tool-use flow asks: the
assistant turn with its `tool_use` block, then a user turn whose `tool_result` for that block's id says `is_error` and
what was wrong; a second invalid answer raises `ModelFailed`.

Tokens are counted as the conventions count `gen_ai.usage.input_tokens`, every input token: Anthropic's
`usage.input_tokens` leaves out `cache_read_input_tokens` and `cache_creation_input_tokens`, which are summed in and
kept apart too (https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching#tracking-cache-performance).

As with the OpenAI-compatible client, no proxy variable of the environment is read (`trust_env=False`), and the key is
never logged, stored or put in an exception.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from minutehand.application.refusals import RunRefused
from minutehand.domain.conversation import ModelMessage, Speaker
from minutehand.domain.scenario import Model
from minutehand.ports.model import Answered, AnswerT, ModelFailed

DEFAULT_BASE_URL = "https://api.anthropic.com"
VERSION = "2023-06-01"
"""The `anthropic-version` header: the Messages API's only version (https://docs.anthropic.com/en/api/versioning)."""
MAX_TOKENS = 4096
"""`max_tokens` is required by the Messages API; a person's reply or a verdict is far shorter."""

TIMEOUT_SECONDS = 120.0
_ERROR_CHARS = 300


# -- the API's own JSON -------------------------------------------------------------------------------------


class _Text(Model):
    type: Literal["text"] = "text"
    text: str


class _ToolUse(Model):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, JsonValue]


class _ToolResult(Model):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = True


class _Turn(Model):
    role: Literal["user", "assistant"]
    content: str | list[_Text | _ToolUse | _ToolResult]


class _Tool(Model):
    name: str
    description: str
    input_schema: dict[str, JsonValue]


class _ToolChoice(Model):
    type: Literal["tool"] = "tool"
    name: str


class _Request(Model):
    model: str
    max_tokens: int
    system: str
    messages: list[_Turn]
    tools: list[_Tool]
    tool_choice: _ToolChoice
    temperature: float | None = None


class _Answered(BaseModel):
    """The API's response is someone else's wire format and grows fields; only what is read is declared."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class _Block(_Answered):
    type: str
    id: str | None = None
    name: str | None = None
    input: dict[str, JsonValue] | None = None
    text: str | None = None


class _Usage(_Answered):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


class _Message(_Answered):
    content: list[_Block]
    stop_reason: str | None = None
    usage: _Usage | None = None


class _Spent:
    """The tokens a request and its one retry cost, summed as the service counted them."""

    def __init__(self) -> None:
        self.input: int | None = None
        self.output: int | None = None
        self.read: int | None = None
        self.created: int | None = None

    def add(self, usage: _Usage | None) -> None:
        if usage is None:
            return
        if usage.input_tokens is not None:
            every = usage.input_tokens + (usage.cache_read_input_tokens or 0) + (usage.cache_creation_input_tokens or 0)
            self.input = (self.input or 0) + every
        if usage.output_tokens is not None:
            self.output = (self.output or 0) + usage.output_tokens
        if usage.cache_read_input_tokens is not None:
            self.read = (self.read or 0) + usage.cache_read_input_tokens
        if usage.cache_creation_input_tokens is not None:
            self.created = (self.created or 0) + usage.cache_creation_input_tokens

    def answered(self, answer: AnswerT, model: str) -> Answered[AnswerT]:
        return Answered(
            answer=answer,
            model=model,
            input_tokens=self.input,
            output_tokens=self.output,
            cache_read_tokens=self.read,
            cache_creation_tokens=self.created,
        )


def _role(speaker: Speaker) -> Literal["user", "assistant"]:
    match speaker:
        case Speaker.ASKER:
            return "user"
        case Speaker.MODEL:
            return "assistant"


# -- the client ---------------------------------------------------------------------------------------------


class AnthropicMessages:
    def __init__(self, *, base_url: str, api_key: str, model_id: str, timeout: float = TIMEOUT_SECONDS) -> None:
        if not api_key:
            raise RunRefused("the Anthropic API key is empty")
        self.model_id = model_id
        self._url = base_url.rstrip("/") + "/v1/messages"
        self.__key = api_key
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"AnthropicMessages({self._url!r}, model_id={self.model_id!r})"

    async def answer(
        self,
        system: str,
        messages: Sequence[ModelMessage],
        answer: type[AnswerT],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> Answered[AnswerT]:
        turns = [_Turn(role=_role(m.speaker), content=m.text) for m in messages]
        tool = _Tool(
            name=answer.__name__,
            description=f"Give the answer as a {answer.__name__}.",
            input_schema=answer.model_json_schema(),
        )
        named = model or self.model_id
        spent = _Spent()

        def request() -> _Request:
            return _Request(
                model=named,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=turns,
                tools=[tool],
                tool_choice=_ToolChoice(name=tool.name),
                temperature=temperature,
            )

        used = await self._complete(request(), tool.name, spent)
        try:
            return spent.answered(answer.model_validate(used.input), named)
        except ValidationError as first:
            turns += [
                _Turn(role="assistant", content=[used]),
                _Turn(
                    role="user",
                    content=[
                        _ToolResult(
                            tool_use_id=used.id,
                            content=(
                                "That input does not match the tool's schema:\n"
                                f"{_errors(first)}\n"
                                "Call the tool again, with input that matches the schema exactly."
                            ),
                        )
                    ],
                ),
            ]
        used = await self._complete(request(), tool.name, spent)
        try:
            return spent.answered(answer.model_validate(used.input), named)
        except ValidationError as second:
            raise ModelFailed(
                f"{named} twice answered with something that is not a {answer.__name__}:\n{_errors(second)}"
            ) from None

    async def _complete(self, request: _Request, tool: str, spent: _Spent) -> _ToolUse:
        body = request.model_dump_json(exclude_none=True)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
                response = await client.post(
                    self._url,
                    content=body,
                    headers={
                        "x-api-key": self.__key,
                        "anthropic-version": VERSION,
                        "content-type": "application/json",
                    },
                )
        except httpx.HTTPError as e:
            raise ModelFailed(f"{self._url} could not be reached: {type(e).__name__}: {self._scrub(str(e))}") from None
        if not response.is_success:
            raise ModelFailed(f"{self._url} answered {response.status_code}: {self._scrub(response.text)}")
        try:
            message = _Message.model_validate_json(response.content)
        except ValidationError:
            raise ModelFailed(
                f"{self._url} answered with something that is not a message: {self._scrub(response.text)}"
            ) from None
        spent.add(message.usage)
        used = next((b for b in message.content if b.type == "tool_use" and b.name == tool), None)
        if used is None or used.id is None or used.input is None:
            said = " ".join(b.text for b in message.content if b.text) or f"stop_reason {message.stop_reason}"
            raise ModelFailed(f"{request.model} did not call the tool {tool}: {self._scrub(said)}")
        return _ToolUse(id=used.id, name=tool, input=used.input)

    def _scrub(self, text: str) -> str:
        """Text from the far end, cut short and with the key taken out: some services echo a key they refuse."""
        return text[:_ERROR_CHARS].replace(self.__key, "[the API key]")


def _errors(error: ValidationError) -> str:
    """The validation errors without the input they were raised on, which is the model's own text."""
    return "\n".join(f"- {'.'.join(str(p) for p in e['loc']) or '(the answer)'}: {e['msg']}" for e in error.errors())
