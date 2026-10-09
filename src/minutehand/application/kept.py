"""A model whose every answer is kept with the world (`Store.record_person_call`): asked the same again, in a rerun
or a fork in the same world file, it answers from the record and calls nothing. What a judged check or the shared
reviewer of the agent's effects is handed, so their model calls are on the record beside the people's
(`model_calls`, side `judge` or `assessor`) and a re-assessment costs nothing."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from minutehand.application.replier import context_key
from minutehand.domain.conversation import ModelMessage, PersonCall, Wrote
from minutehand.ports.model import Answered, AnswerT, ModelFailed
from minutehand.ports.model import Model as LanguageModel
from minutehand.ports.store import Store


class KeptModel:
    """`ports.model.Model` over another, through the world's record."""

    def __init__(
        self,
        model: LanguageModel,
        world: Store,
        *,
        wrote: Wrote,
        prompt_version: str,
        sim_time: datetime,
        wake: int,
        person: str | None = None,
    ) -> None:
        self._model = model
        self._world = world
        self._wrote = wrote
        self._prompt_version = prompt_version
        self._sim_time = sim_time
        self._wake = wake
        self._person = person
        self.model_id = model.model_id

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
        key = context_key(named, self._prompt_version, system, messages, answer.__name__, temperature or 0)
        call = {
            "key": key,
            "person": self._person,
            "wrote": self._wrote,
            "model": named,
            "prompt_version": self._prompt_version,
            "sim_time": self._sim_time,
            "wake": self._wake,
        }
        kept = self._world.written(key)
        if kept is not None:
            self._world.record_person_call(PersonCall.model_validate({**call, "answer": kept, "replayed": True}))
            return Answered(answer=answer.model_validate_json(kept), model=named)
        try:
            answered = await self._model.answer(system, messages, answer, model=model, temperature=temperature)
        except ModelFailed as e:
            self._world.record_person_call(PersonCall.model_validate({**call, "failure": str(e)}))
            raise
        self._world.record_person_call(
            PersonCall.model_validate(
                {
                    **call,
                    "answer": answered.answer.model_dump_json(),
                    "input_tokens": answered.input_tokens,
                    "output_tokens": answered.output_tokens,
                    "cache_read_tokens": answered.cache_read_tokens,
                    "cache_creation_tokens": answered.cache_creation_tokens,
                }
            )
        )
        return answered
