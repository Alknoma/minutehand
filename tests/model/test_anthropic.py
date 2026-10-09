"""The Anthropic Messages client, against a server on this machine shaped as Anthropic documents its API, and the
environment that chooses it."""

from __future__ import annotations

import json

import pytest

from minutehand.adapters.model.anthropic import VERSION, AnthropicMessages
from minutehand.adapters.model.environment import (
    API_KEY_VARIABLE,
    API_VARIABLE,
    BASE_URL_VARIABLE,
    MODEL_VARIABLE,
    from_environment,
)
from minutehand.adapters.model.openai_compatible import OpenAICompatible
from minutehand.application.refusals import RunRefused
from minutehand.domain.conversation import ModelMessage, Speaker
from minutehand.domain.scenario import Model
from minutehand.ports.model import ModelFailed
from tests.model.fake_completions import Answer
from tests.model.fake_messages import fake_messages

KEY = "sk-ant-test-0123456789-never-shown"


class Forecast(Model):
    sunny: bool
    note: str | None


ASKED = [
    ModelMessage(speaker=Speaker.ASKER, text="Will it be sunny?"),
    ModelMessage(speaker=Speaker.MODEL, text="Where?"),
    ModelMessage(speaker=Speaker.ASKER, text="Lisbon."),
]


def client(base_url: str) -> AnthropicMessages:
    return AnthropicMessages(base_url=base_url, api_key=KEY, model_id="claude-test")


async def test_the_answer_is_the_input_of_one_forced_tool_and_cached_tokens_are_counted() -> None:
    async with fake_messages(lambda _: {"sunny": True, "note": None}) as fake:
        answered = await client(fake.base_url).answer("You forecast.", ASKED, Forecast, temperature=0)
    assert answered.answer == Forecast(sunny=True, note=None)
    assert (answered.input_tokens, answered.cache_read_tokens, answered.cache_creation_tokens) == (2112, 2000, 100)
    assert answered.output_tokens == 30
    [asked] = fake.asked
    assert asked.headers["x-api-key"] == KEY and asked.headers["anthropic-version"] == VERSION
    assert "authorization" not in asked.headers
    body = asked.body
    assert body["system"] == "You forecast." and body["temperature"] == 0 and body["model"] == "claude-test"
    assert body["messages"] == [
        {"role": "user", "content": "Will it be sunny?"},
        {"role": "assistant", "content": "Where?"},
        {"role": "user", "content": "Lisbon."},
    ]
    [tool] = body["tools"]  # type: ignore[misc]
    assert tool["name"] == "Forecast" and tool["input_schema"] == Forecast.model_json_schema()
    assert body["tool_choice"] == {"type": "tool", "name": "Forecast"}
    assert isinstance(body["max_tokens"], int)


async def test_an_input_that_does_not_validate_is_sent_back_once_as_a_tool_result_error() -> None:
    answers: list[dict[str, object]] = [{"sunny": "maybe"}, {"sunny": False, "note": "rain"}]
    given = iter(answers)
    async with fake_messages(lambda _: next(given)) as fake:
        answered = await client(fake.base_url).answer("s", ASKED, Forecast)
    assert answered.answer == Forecast(sunny=False, note="rain")
    assert answered.input_tokens == 2 * 2112  # both requests counted
    retry = fake.asked[1].body["messages"]
    assert isinstance(retry, list)
    assistant, result = retry[-2], retry[-1]
    assert assistant["role"] == "assistant" and assistant["content"][0]["type"] == "tool_use"
    [said] = result["content"]
    assert said["type"] == "tool_result" and said["is_error"] is True
    assert said["tool_use_id"] == assistant["content"][0]["id"] and "sunny" in said["content"]


async def test_a_second_invalid_input_raises() -> None:
    async with fake_messages(lambda _: {"sunny": "maybe"}) as fake:
        with pytest.raises(ModelFailed, match="twice answered"):
            await client(fake.base_url).answer("s", ASKED, Forecast)


async def test_an_error_status_raises_without_the_key() -> None:
    error = {"type": "error", "error": {"type": "authentication_error", "message": f"invalid x-api-key {KEY}"}}
    async with fake_messages(lambda _: Answer(status=401, body=json.dumps(error))) as fake:
        with pytest.raises(ModelFailed) as raised:
            await client(fake.base_url).answer("s", ASKED, Forecast)
    assert "401" in str(raised.value) and KEY not in str(raised.value)


async def test_an_answer_with_no_tool_call_is_refused() -> None:
    text_only = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": "I would rather not."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    async with fake_messages(lambda _: Answer(status=200, body=json.dumps(text_only))) as fake:
        with pytest.raises(ModelFailed, match="did not call the tool Forecast: I would rather not"):
            await client(fake.base_url).answer("s", ASKED, Forecast)


def test_the_environment_chooses_the_anthropic_api_and_its_base_url() -> None:
    chosen = from_environment({API_VARIABLE: "anthropic", API_KEY_VARIABLE: KEY, MODEL_VARIABLE: "claude-test"})
    assert isinstance(chosen, AnthropicMessages) and "https://api.anthropic.com/v1/messages" in repr(chosen)
    elsewhere = from_environment(
        {API_VARIABLE: "anthropic", API_KEY_VARIABLE: KEY, MODEL_VARIABLE: "m", BASE_URL_VARIABLE: "http://h:1"}
    )
    assert "http://h:1/v1/messages" in repr(elsewhere)
    assert isinstance(from_environment({API_KEY_VARIABLE: KEY, MODEL_VARIABLE: "m"}), OpenAICompatible)


def test_an_api_the_environment_names_that_there_is_not_is_refused() -> None:
    with pytest.raises(RunRefused, match="openai or anthropic"):
        from_environment({API_VARIABLE: "gemini", API_KEY_VARIABLE: KEY, MODEL_VARIABLE: "m"})
