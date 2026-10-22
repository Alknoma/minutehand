"""The model that writes what people say in tests: the recipes' stand-in (`examples/recipes/fake_model.py`), served
on 127.0.0.1 from a thread of this process, once per process. It follows fixed rules over the prompt Minutehand
sends, so a run with model-written people is repeatable and calls no real model."""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache

from minutehand.adapters.model.pydantic_ai import MODEL_VARIABLE, PydanticAIModel
from minutehand.application.people import People
from minutehand.application.replier import PeopleReplier
from minutehand.domain.scenario import Person, ProviderKey, Scenario
from minutehand.ports.model import Model as LanguageModel
from tests.model.fake_completions import PROVIDER, model_over
from tests.support import fake_model

MODEL = "people-fake"
KEY = "sk-people-fake"


@cache
def _served() -> tuple[fake_model.Server, fake_model.Served]:
    return fake_model.start()


def people_base_url() -> str:
    server, _ = _served()
    return f"http://127.0.0.1:{server.server_port}/v1"


def people_model() -> PydanticAIModel:
    """A model port over the stand-in."""
    return model_over(people_base_url(), model_id=MODEL, key=KEY)


def people_environment() -> dict[str, str]:
    """The variables `minutehand run` and `serve` read the people's model from, pointed at the stand-in: the model's
    name for Minutehand, and the OpenAI client's own variables for the endpoint and a key it never looks at."""
    return {MODEL_VARIABLE: f"{PROVIDER}:{MODEL}", "OPENAI_BASE_URL": people_base_url(), "OPENAI_API_KEY": KEY}


def people_requests() -> list[dict[str, object]]:
    """Every request the stand-in was sent to write for a person, in order, as bodies: the checks of their replies
    against what they know are `fact_checks()`."""
    _, served = _served()
    with served.lock:
        return [
            body
            for path, body in served.received
            if fake_model.people_schema(body) not in (None, "FactCheck", "ItemReview")
        ]


def fact_checks() -> list[dict[str, object]]:
    """Every check of a person's reply against what they know the stand-in was sent, in order, as bodies."""
    _, served = _served()
    with served.lock:
        return [body for path, body in served.received if fake_model.people_schema(body) == "FactCheck"]


def people_engine(scenario: Scenario, ports: Mapping[ProviderKey, object], model: LanguageModel | None) -> People:
    """The people engine over `ports`, by provider key, with the scripted replier over the same model."""
    return People(scenario, ports, PeopleReplier(scenario, model), model)


def email(person: Person) -> str:
    """A test person's email: every person a test writes has one."""
    assert person.email is not None, f"{person.key} has no email"
    return person.email
