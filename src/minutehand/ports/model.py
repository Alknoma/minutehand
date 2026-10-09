"""A language model, for the two parts of a run that cannot be computed: what a person writes back, and
whether something the agent wrote makes sense to the person it was for.

The answer is always structured: the caller names a Pydantic class and gets an instance of it, never text to
parse. Which model answered and under which prompt is the caller's to record (`domain.conversation.Provenance`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from minutehand.domain.checks import CheckReport, Needs, RunView
from minutehand.domain.conversation import ModelMessage, Wrote
from minutehand.domain.world import EntityRef

AnswerT = TypeVar("AnswerT", bound=BaseModel)


class Answered[T: BaseModel](BaseModel):
    """A structured answer, and what it cost: the tokens the service counted, None when it said nothing of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: T
    model: str = Field(description="The model the request named")
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelFailed(Exception):
    """The model could not be reached, refused, or answered twice with something that is not the answer asked for."""


class Model(Protocol):
    model_id: str
    """The model a request names when the caller names none."""

    async def answer(
        self,
        system: str,
        messages: Sequence[ModelMessage],
        answer: type[AnswerT],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> Answered[AnswerT]:
        """The model's answer to `messages` under `system`, validated as an `answer`.

        `model` names another model than `model_id` for this request; `temperature` None leaves the provider's
        default. Raises `ModelFailed` rather than return something that is not an `answer`.
        """
        ...


class JudgedCheck(Protocol):
    """A check that needs a model. It runs after every deterministic check, and never on an entity one of them
    already failed: a ticket failed for a wrong name is not then judged for clarity.

    Its findings carry `Finding.judged` and are `FindingKind.REVIEW`. Every call it makes is kept with the world
    under `wrote` (`application.kept.KeptModel`), so a re-assessment replays it.
    """

    id: str
    needs: frozenset[Needs]
    prompt_version: str
    wrote: Wrote

    def applies(self, view: RunView) -> bool:
        """Whether the run holds anything for it to judge: without, it asks nothing and is never said to be blocked."""
        ...

    async def judge(self, view: RunView, model: Model, *, failed: frozenset[EntityRef]) -> CheckReport:
        """Judge what `view` holds, leaving out every entity in `failed`."""
        ...
