"""`ports.model.Model` over pydantic-ai. Minutehand manages no API key for any model provider: one variable names the
model, and the provider's own client reads its own variables from the environment Minutehand was started in.

    MINUTEHAND_MODEL    pydantic-ai's `provider:model` form. `openai:gpt-5-mini` is OpenAI's Responses API and
                        `openai-chat:<model>` its chat completions API, which is what an OpenAI-compatible endpoint
                        at OPENAI_BASE_URL speaks; `anthropic:claude-sonnet-5-5`, `google:gemini-2.5-pro`,
                        `google-cloud:<model>` (Vertex), `bedrock:<model>`, `azure:<deployment>`, `mistral:…`,
                        `groq:…`, `cohere:…`, `xai:…`, `deepseek:…`, `openrouter:…`, `together:…`, `fireworks:…`,
                        `ollama:…`, and every other provider pydantic-ai names.

The provider's client is configured by its own standard variables (OPENAI_API_KEY and OPENAI_BASE_URL,
ANTHROPIC_API_KEY, GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS, AWS_*, AZURE_OPENAI_*, MISTRAL_API_KEY,
GROQ_API_KEY, CO_API_KEY, XAI_API_KEY, …), which it reads itself: Minutehand reads, names, stores and passes none
of them. A provider whose package is not installed is refused naming the extra to install (`minutehand[openai]`);
one whose client refuses its configuration (no key, no endpoint) is refused in the client's own words, naming the
provider. Both are refused when the model is read from the environment, before anything starts.

The answer is structured: the caller's class is the one tool the model is made to call (`ToolOutput`, the mode
every provider pydantic-ai names supports); an input that does not validate is sent back once with the errors, as
pydantic-ai does, and a second raises `ModelFailed`. Tokens are counted as pydantic-ai counts them.

Each call is made through an HTTP client of Minutehand's own that reads no proxy variable (`trust_env=False`) and
is closed when the call ends: Minutehand's own proxy stands between the agent and the world, and Minutehand's
model calls are not the agent's traffic. Bedrock's client is boto3's, which takes no such client and reads the proxy
variables itself.
"""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Callable, Sequence
from typing import Any

import httpx2
import pydantic_ai
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UserError
from pydantic_ai.messages import ModelMessage as WireMessage
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model as ProviderModel
from pydantic_ai.models import infer_model
from pydantic_ai.providers import Provider, infer_provider, infer_provider_class
from pydantic_ai.settings import ModelSettings

from minutehand.application.refusals import RunRefused
from minutehand.domain.conversation import ModelMessage, Speaker
from minutehand.ports.model import Answered, AnswerT, ModelFailed

pydantic_ai.BANNER_ENABLED = False
"""pydantic-ai prints a first-run banner advertising its observability to a terminal; Minutehand's output is its
own, so the banner is turned off before any run."""

MODEL_VARIABLE = "MINUTEHAND_MODEL"
TIMEOUT_SECONDS = 120.0
RETRIES = 1
"""How many times an answer that does not validate is sent back before the call fails."""
_ERROR_CHARS = 300
_EXTRA = re.compile(r"pydantic-ai-slim\[([\w-]+)\]")
"""How pydantic-ai names the extra a missing provider package belongs to, in its own ImportError."""

Resolve = Callable[[str, httpx2.AsyncClient], ProviderModel]
"""pydantic-ai's model for a `provider:model` name, its provider's client making its requests through the given
HTTP client where the provider takes one."""


class PydanticAIModel:
    """A model named `provider:model_id`, answering through pydantic-ai. `resolve` builds the pydantic-ai model for
    that name, and for any other a request names (a bare name is one of the same provider's), over the HTTP client
    of the call; it is called once here, so a provider that cannot be built refuses now."""

    def __init__(self, resolve: Resolve, *, provider: str, model_id: str) -> None:
        self.provider = provider
        self.model_id = model_id
        self._resolve = resolve
        self._default_temperature: set[str] = set()
        """Models that take only their own default temperature, as their API said when one was sent: it is left out
        of every later request to them."""
        resolve(self._named(model_id), httpx2.AsyncClient(trust_env=False))

    def __repr__(self) -> str:
        return f"PydanticAIModel({self.provider}:{self.model_id})"

    def _named(self, model: str) -> str:
        return model if ":" in model else f"{self.provider}:{model}"

    async def answer(
        self,
        system: str,
        messages: Sequence[ModelMessage],
        answer: type[AnswerT],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> Answered[AnswerT]:
        named = model or self.model_id
        if named in self._default_temperature:
            temperature = None
        history, prompt = _wire(messages)
        settings: ModelSettings | None = {"temperature": temperature} if temperature is not None else None
        try:
            async with _own_http_client() as client:
                agent: Agent[None, AnswerT] = Agent(
                    self._resolve(self._named(named), client),
                    output_type=ToolOutput(answer, name=answer.__name__),
                    instructions=system,
                    retries=RETRIES,
                )
                result = await agent.run(prompt, message_history=history, model_settings=settings)
        except ModelHTTPError as e:
            if temperature is not None and _refuses_temperature(e):
                self._default_temperature.add(named)
                return await self.answer(system, messages, answer, model=model, temperature=None)
            raise ModelFailed(f"{self.provider} answered {e.status_code} to {named}: {_short(e.body)}") from None
        except ModelAPIError as e:
            raise ModelFailed(f"{named} at {self.provider} could not be reached: {_short(e)}") from None
        except UnexpectedModelBehavior as e:
            raise ModelFailed(f"{named} did not answer with a {answer.__name__}: {_short(e)}") from None
        usage = result.usage
        return Answered(
            answer=result.output,
            model=named,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens or None,
            cache_creation_tokens=usage.cache_write_tokens or None,
        )


def _wire(messages: Sequence[ModelMessage]) -> tuple[list[WireMessage], str]:
    """The conversation as pydantic-ai takes it: every turn but the last as history, the last, the asker's, as the
    prompt. Two of the asker's in a row are two requests, as they were sent."""
    turns = list(messages)
    prompt = turns.pop().text if turns and turns[-1].speaker is Speaker.ASKER else ""
    history: list[WireMessage] = []
    for turn in turns:
        match turn.speaker:
            case Speaker.ASKER:
                history.append(ModelRequest(parts=[UserPromptPart(turn.text)]))
            case Speaker.MODEL:
                history.append(ModelResponse(parts=[TextPart(turn.text)]))
    return history, prompt


def _short(said: object) -> str:
    """Text from the far end, cut short."""
    return str(said)[:_ERROR_CHARS]


class _Refused(BaseModel):
    """An OpenAI-shaped error body, read only for the parameter it refuses (`{"error": {"param": "temperature"}}`):
    someone else's wire format, so only what is read is declared."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    error: _Error | None = None


class _Error(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    param: str | None = None


def _refuses_temperature(error: ModelHTTPError) -> bool:
    """A 400 refusing the request's temperature, as OpenAI words it: the model takes only its default."""
    if error.status_code != 400 or not isinstance(error.body, dict):
        return False
    try:
        refused = _Refused.model_validate(error.body)
    except ValueError:
        return False
    return refused.error is not None and refused.error.param == "temperature"


# -- from the environment -----------------------------------------------------------------------------------


def named_model() -> str:
    """What `MINUTEHAND_MODEL` names, or nothing."""
    return os.environ[MODEL_VARIABLE].strip() if MODEL_VARIABLE in os.environ else ""


def from_environment() -> PydanticAIModel | None:
    """The model `MINUTEHAND_MODEL` names, its provider's client configured by that provider's own variables; None
    when nothing is named. Refused, naming what is wrong, when the name is not `provider:model`, the provider's
    package is not installed, or its client refuses its configuration."""
    named = named_model()
    if not named:
        return None
    provider, colon, model_id = named.partition(":")
    if not colon or not provider or not model_id:
        raise RunRefused(
            f"{MODEL_VARIABLE} is {named!r}: it names a model as pydantic-ai does, `provider:model` "
            "(`openai:gpt-5-mini`, `anthropic:claude-sonnet-5-5`, `google:gemini-2.5-pro`)"
        )
    return PydanticAIModel(resolve, provider=provider, model_id=model_id)


def _own_http_client() -> httpx2.AsyncClient:
    """The HTTP client a call is made through: it reads no proxy variable of the environment."""
    return httpx2.AsyncClient(trust_env=False, timeout=TIMEOUT_SECONDS)


def resolve(name: str, http_client: httpx2.AsyncClient) -> ProviderModel:
    """pydantic-ai's model for a `provider:model` name, its provider built over `http_client` when the provider takes
    one, else as pydantic-ai builds it; a refusal names what to install or what the provider said."""

    def provider_over(provider: str) -> Provider[Any]:
        cls: Any = infer_provider_class(provider)
        if "http_client" in inspect.signature(cls.__init__).parameters:
            return cls(http_client=http_client)
        return infer_provider(provider)

    provider, _, _ = name.partition(":")
    try:
        return infer_model(name, provider_factory=provider_over)
    except ImportError as e:
        extra = _EXTRA.search(str(e))
        if extra is not None:
            raise RunRefused(
                f"{MODEL_VARIABLE} names {name!r}, and the {provider} provider's package is not installed: "
                f"pip install 'minutehand[{extra.group(1)}]'"
            ) from None
        raise RunRefused(
            f"{MODEL_VARIABLE} names {name!r}, and the {provider} provider's package is not installed: {e}"
        ) from None
    except UserError as e:
        raise RunRefused(
            f"{MODEL_VARIABLE} names {name!r}, and the {provider} provider refused its configuration: {e}"
        ) from None
    except ValueError as e:
        # pydantic-ai's own refusal of a provider it does not name ("Unknown provider: …")
        raise RunRefused(f"{MODEL_VARIABLE} names {name!r}: {e}") from None
