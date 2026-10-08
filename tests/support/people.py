"""The model that writes what people say in tests: the recipes' stand-in (`examples/recipes/fake_model.py`), served
on 127.0.0.1 from a thread of this process, once per process. It follows fixed rules over the prompt Minutehand
sends, so a run with model-written people is repeatable and calls no real model."""

from __future__ import annotations

from functools import cache

from examples.recipes import fake_model
from minutehand.adapters.model.openai_compatible import (
    API_KEY_VARIABLE,
    BASE_URL_VARIABLE,
    MODEL_VARIABLE,
    OpenAICompatible,
)

MODEL = "people-fake"
KEY = "sk-people-fake"


@cache
def _served() -> tuple[fake_model.Server, fake_model.Served]:
    return fake_model.start()


def people_base_url() -> str:
    server, _ = _served()
    return f"http://127.0.0.1:{server.server_port}/v1"


def people_model() -> OpenAICompatible:
    """A model port over the stand-in."""
    return OpenAICompatible(base_url=people_base_url(), api_key=KEY, model_id=MODEL)


def people_environment() -> dict[str, str]:
    """The variables `minutehand run` and `serve` read the people's model from, pointed at the stand-in."""
    return {BASE_URL_VARIABLE: people_base_url(), API_KEY_VARIABLE: KEY, MODEL_VARIABLE: MODEL}


def people_requests() -> list[dict[str, object]]:
    """Every request the stand-in was sent for a person, in order, as bodies."""
    _, served = _served()
    with served.lock:
        return [body for path, body in served.received if fake_model.people_schema(body) is not None]
