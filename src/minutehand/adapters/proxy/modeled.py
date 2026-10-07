"""A service nobody declared, stood in for by a language model (`UnknownHosts.MODEL`).

The model is shown what the agent asked and every earlier exchange it answered for the same host in this run,
which are the only state that service has, and answers as the service would: a status, a content type and a body.
Nothing is sent anywhere. Its answers are a guess at the service, and every one is recorded as the model's.
"""

from __future__ import annotations

from pydantic import Field

from minutehand.domain.conversation import ModelMessage, Speaker
from minutehand.domain.scenario import Model
from minutehand.domain.world import AnsweredBy, RecordedCall
from minutehand.ports.model import Model as LanguageModel

KEPT_EXCHANGES = 30
"""Earlier exchanges of the host shown to the model, the latest first to go."""
BODY_SHOWN = 4000

SYSTEM = (
    "You stand in for the HTTP API at {host} while an automated test exercises a program that calls it. Answer each "
    "request exactly as that service would: its usual status codes, headers and JSON shapes, with ids that look like "
    "its own. The earlier exchanges listed are everything that has happened to this service: what was created there "
    "exists, what was deleted is gone, and nothing else exists. Never mention testing or being a model."
)


class ModeledAnswer(Model):
    """What the service answers, as the model writes it."""

    status: int = Field(ge=100, le=599)
    content_type: str = Field(default="application/json")
    body: str = Field(description="The response body as text; empty for none")


def _shown(text: str | None) -> str:
    text = text or ""
    return text if len(text) <= BODY_SHOWN else text[:BODY_SHOWN] + " …(cut)"


def earlier(calls: list[RecordedCall], host: str) -> list[RecordedCall]:
    """Every call to `host` a model answered in this run, oldest first."""
    return [
        c
        for c in calls
        if c.exchange.host == host
        and c.exchange.captured is not None
        and c.exchange.captured.answered_by is AnsweredBy.MODEL
    ]


async def answer(
    model: LanguageModel, host: str, method: str, path: str, body: str | None, before: list[RecordedCall]
) -> ModeledAnswer:
    """The model's answer, as the service at `host`, to one request; raises `ModelFailed` as the port does."""
    history = "\n\n".join(
        f"{c.exchange.method} {c.exchange.path}\n{_shown(c.exchange.request_body)}\n-> {c.exchange.status}\n"
        f"{_shown(c.exchange.response_body)}"
        for c in before[-KEPT_EXCHANGES:]
    )
    asked = (
        f"Earlier exchanges with {host}, oldest first:\n\n{history or '(none: the service is empty)'}\n\n"
        f"The request to answer now:\n{method} {path}\n{_shown(body)}"
    )
    return await model.answer(
        SYSTEM.format(host=host), [ModelMessage(speaker=Speaker.ASKER, text=asked)], ModeledAnswer, temperature=0
    )
