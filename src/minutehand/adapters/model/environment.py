"""The model Minutehand's own calls go to (people's words, a declared service's answers, the judged checks), as this
process's environment configures it:

    MINUTEHAND_MODEL_API        openai (default): an OpenAI-compatible chat-completions API with JSON-schema output;
                                anthropic: Anthropic's Messages API, the answer as one forced tool's input
    MINUTEHAND_MODEL_BASE_URL   default https://api.openai.com/v1, or https://api.anthropic.com for anthropic
    MINUTEHAND_MODEL_API_KEY    sent as the API asks (a bearer token, or x-api-key); never logged, stored or put in an
                                exception
    MINUTEHAND_MODEL            the model a request names unless the caller names another
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum

from minutehand.adapters.model import anthropic, openai_compatible
from minutehand.adapters.model.anthropic import AnthropicMessages
from minutehand.adapters.model.openai_compatible import OpenAICompatible
from minutehand.application.refusals import RunRefused

API_VARIABLE = "MINUTEHAND_MODEL_API"
BASE_URL_VARIABLE = "MINUTEHAND_MODEL_BASE_URL"
API_KEY_VARIABLE = "MINUTEHAND_MODEL_API_KEY"
MODEL_VARIABLE = "MINUTEHAND_MODEL"
VARIABLES = (API_VARIABLE, BASE_URL_VARIABLE, API_KEY_VARIABLE, MODEL_VARIABLE)


class ModelApi(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


DEFAULT_BASE_URLS = {
    ModelApi.OPENAI: openai_compatible.DEFAULT_BASE_URL,
    ModelApi.ANTHROPIC: anthropic.DEFAULT_BASE_URL,
}


def _read(environ: Mapping[str, str], name: str) -> str:
    return environ[name] if name in environ else ""


def api_of(environ: Mapping[str, str]) -> ModelApi:
    """Which API the environment names; refused, naming the choices, when it names another."""
    named = _read(environ, API_VARIABLE).strip().lower() or ModelApi.OPENAI.value
    try:
        return ModelApi(named)
    except ValueError:
        raise RunRefused(f"{API_VARIABLE} is {named!r}: it is {' or '.join(a.value for a in ModelApi)}") from None


def base_url_of(environ: Mapping[str, str]) -> str:
    """The base URL requests go to: the environment's, else the API's own."""
    return _read(environ, BASE_URL_VARIABLE) or DEFAULT_BASE_URLS[api_of(environ)]


def from_environment(environ: Mapping[str, str] = os.environ) -> OpenAICompatible | AnthropicMessages | None:
    """The model the environment configures; None when neither the key nor the model is set.

    Half a configuration is refused, naming what is missing, rather than read as none.
    """
    key = _read(environ, API_KEY_VARIABLE)
    model = _read(environ, MODEL_VARIABLE)
    if not key and not model:
        return None
    if not key or not model:
        missing = API_KEY_VARIABLE if not key else MODEL_VARIABLE
        raise RunRefused(f"a model is half configured: {missing} is not set")
    api = api_of(environ)
    base = base_url_of(environ)
    if api is ModelApi.ANTHROPIC:
        return AnthropicMessages(base_url=base, api_key=key, model_id=model)
    return OpenAICompatible(base_url=base, api_key=key, model_id=model)
