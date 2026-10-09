"""The OpenAI-compatible client against a chat-completions server on this machine."""

from __future__ import annotations

import json

import pytest

from minutehand.adapters.model.openai_compatible import (
    API_KEY_VARIABLE,
    BASE_URL_VARIABLE,
    DEFAULT_BASE_URL,
    MODEL_VARIABLE,
    OpenAICompatible,
    from_environment,
)
from minutehand.application.refusals import RunRefused
from minutehand.domain.conversation import ModelMessage, Speaker
from minutehand.domain.scenario import Model
from minutehand.ports.model import ModelFailed
from tests.model.fake_completions import Answer, Received, completion, fake_completions

KEY = "sk-test-0123456789abcdef-never-shown"


class Forecast(Model):
    sunny: bool
    note: str | None


def client(base_url: str) -> OpenAICompatible:
    return OpenAICompatible(base_url=base_url, api_key=KEY, model_id="judge-1")


ASKED = [
    ModelMessage(speaker=Speaker.ASKER, text="Will it be sunny?"),
    ModelMessage(speaker=Speaker.MODEL, text="Where?"),
    ModelMessage(speaker=Speaker.ASKER, text="Lisbon."),
]


async def test_the_request_carries_the_system_text_the_messages_and_a_strict_schema() -> None:
    async with fake_completions(lambda _: Forecast(sunny=True, note=None)) as fake:
        answered = (await client(fake.base_url).answer("You forecast.", ASKED, Forecast, temperature=0.2)).answer

    assert answered == Forecast(sunny=True, note=None)
    [received] = fake.received
    assert received.authorization == f"Bearer {KEY}"
    assert received.model == "judge-1" and received.temperature == 0.2
    assert received.said == [
        ("system", "You forecast."),
        ("user", "Will it be sunny?"),
        ("assistant", "Where?"),
        ("user", "Lisbon."),
    ]
    assert received.body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Forecast", "schema": received.schema, "strict": True},
    }
    assert received.schema["required"] == ["sunny", "note"]
    assert received.schema["additionalProperties"] is False


async def test_a_request_may_name_another_model_and_leave_the_temperature_to_the_provider() -> None:
    async with fake_completions(lambda _: Forecast(sunny=False, note="rain")) as fake:
        await client(fake.base_url).answer("s", ASKED, Forecast, model="person-2")

    [received] = fake.received
    assert received.model == "person-2" and "temperature" not in received.body


async def test_the_cached_prompt_tokens_the_service_details_are_counted_apart() -> None:
    # https://platform.openai.com/docs/guides/prompt-caching: prompt_tokens includes the cached tokens it details
    body = completion(Forecast(sunny=True, note=None).model_dump_json(), "judge-1")
    body["usage"] = {
        "prompt_tokens": 2006,
        "completion_tokens": 300,
        "total_tokens": 2306,
        "prompt_tokens_details": {"cached_tokens": 1920},
    }
    async with fake_completions(lambda _: Answer(status=200, body=json.dumps(body))) as fake:
        answered = await client(fake.base_url).answer("s", ASKED, Forecast)
    assert (answered.input_tokens, answered.cache_read_tokens, answered.output_tokens) == (2006, 1920, 300)


async def test_an_answer_that_does_not_validate_is_sent_back_once_with_the_error() -> None:
    def rule(received: Received) -> str | Forecast:
        return '{"sunny": "maybe"}' if len(fake.received) == 1 else Forecast(sunny=False, note=None)

    async with fake_completions(rule) as fake:
        answered = (await client(fake.base_url).answer("s", ASKED, Forecast)).answer

    assert answered == Forecast(sunny=False, note=None)
    first, second = fake.received
    assert second.said[: len(first.said)] == first.said
    assert second.said[-2] == ("assistant", '{"sunny": "maybe"}')
    retry = second.last
    assert "does not match the required schema" in retry and "sunny" in retry and "note" in retry


async def test_a_second_invalid_answer_raises_and_names_what_was_wrong() -> None:
    async with fake_completions(lambda _: '{"sunny": "maybe"}') as fake:
        with pytest.raises(ModelFailed, match="twice answered") as raised:
            await client(fake.base_url).answer("s", ASKED, Forecast)

    assert len(fake.received) == 2
    assert "Forecast" in str(raised.value) and "sunny" in str(raised.value)


async def test_an_error_status_raises_without_the_key_even_when_the_service_echoes_it() -> None:
    echoed = f'{{"error": {{"message": "Incorrect API key provided: {KEY}"}}}}'
    async with fake_completions(lambda _: Answer(status=401, body=echoed)) as fake:
        with pytest.raises(ModelFailed, match="answered 401") as raised:
            await client(fake.base_url).answer("s", ASKED, Forecast)

    assert KEY not in str(raised.value) and "[the API key]" in str(raised.value)
    assert KEY not in repr(client(fake.base_url))


async def test_a_body_that_is_not_a_completion_raises() -> None:
    async with fake_completions(lambda _: Answer(status=200, body='{"choices": []}')) as fake:
        with pytest.raises(ModelFailed, match="not a chat completion"):
            await client(fake.base_url).answer("s", ASKED, Forecast)


async def test_the_proxy_variables_of_the_environment_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    async with fake_completions(lambda _: Forecast(sunny=True, note=None)) as fake:
        assert (await client(fake.base_url).answer("s", ASKED, Forecast)).answer == Forecast(sunny=True, note=None)


def test_no_key_and_no_model_in_the_environment_is_no_model() -> None:
    assert from_environment({}) is None


def test_half_a_configuration_is_refused_naming_what_is_missing() -> None:
    with pytest.raises(RunRefused, match=MODEL_VARIABLE):
        from_environment({API_KEY_VARIABLE: KEY})
    with pytest.raises(RunRefused, match=API_KEY_VARIABLE):
        from_environment({MODEL_VARIABLE: "m"})


def test_the_base_url_defaults_to_openai_and_may_be_set() -> None:
    default = from_environment({API_KEY_VARIABLE: KEY, MODEL_VARIABLE: "m"})
    local = from_environment({API_KEY_VARIABLE: KEY, MODEL_VARIABLE: "m", BASE_URL_VARIABLE: "http://127.0.0.1:1/v1/"})
    assert default is not None and DEFAULT_BASE_URL in repr(default)
    assert local is not None and "http://127.0.0.1:1/v1/chat/completions" in repr(local)
    assert default.model_id == "m"
