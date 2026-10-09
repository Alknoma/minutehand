"""What Minutehand says to a language model, and what it keeps of the answer.

A model writes the replies of people whose `reply` is `Answers`, and judges what no deterministic check can.
Either way the record keeps which model answered and under which version of which prompt, so a finding or a
reply can be traced to the words that produced it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from minutehand.domain.scenario import Model
from minutehand.domain.world import EntityRef


class Speaker(StrEnum):
    """Who said a message in a conversation with a model: whoever the model is answering, or the model itself.
    The system text is not a message; it travels apart. An adapter spells these in its own API's words."""

    ASKER = "asker"
    MODEL = "model"


class ModelMessage(Model):
    speaker: Speaker
    text: str


class Provenance(Model):
    """Which model wrote something, under which prompt."""

    model: str = Field(description="The model id the request named")
    prompt_version: str = Field(description="The version constant of the prompt the request carried")


class Judgement(Provenance):
    """A model's verdict, kept with the finding it produced."""

    rationale: str = Field(description="Why the model answered as it did, in its own words")


class Wrote(StrEnum):
    """What a model wrote: for a person, for a declared service, or as a judge of the run."""

    REPLY = "reply"  # their words back to a message
    SUMMARY = "summary"  # what they saw before the turns they are shown word for word
    TRANSITION = "transition"  # the transition they take on an item pending on them, and what it carries
    SERVICE_MACHINE = "service_machine"  # the states and transitions of a declared service's items, proposed once
    SERVICE_ROUTE = "service_route"  # what a route of a declared service means, read once
    SERVICE_ANSWER = "service_answer"  # what a declared service answers a call, or pushes, from its state and log
    JUDGEMENT = "judgement"  # a judged check's verdict on something the scenario asks (`asked_about`)
    FACT_CHECK = "fact_check"  # whether a person's reply stays inside what they know, before it is sent
    REVIEW = "review"  # the shared reviewer's reading of one of the agent's effects (`checks/judged/review.py`)


class Side(StrEnum):
    """Whose a model call Minutehand made was, as the read model's `model_calls.side` says it."""

    PERSON = "person"  # a person's words, a summary of what they saw, or their move on an item
    SERVICE = "service"  # a declared service's machine, a route's meaning, an answer
    JUDGE = "judge"  # a judged check's verdict, or a person's reply checked against what they know
    ASSESSOR = "assessor"  # the shared reviewer of the agent's effects


SIDE = {
    Wrote.REPLY: Side.PERSON,
    Wrote.SUMMARY: Side.PERSON,
    Wrote.TRANSITION: Side.PERSON,
    Wrote.SERVICE_MACHINE: Side.SERVICE,
    Wrote.SERVICE_ROUTE: Side.SERVICE,
    Wrote.SERVICE_ANSWER: Side.SERVICE,
    Wrote.JUDGEMENT: Side.JUDGE,
    Wrote.FACT_CHECK: Side.JUDGE,
    Wrote.REVIEW: Side.ASSESSOR,
}


class PersonCall(Model):
    """One call Minutehand made to a model: to write what a person says or a declared service answers, or to judge
    the run. Kept with the world, so a rerun or fork with the same context (`key`) replays its answer and calls
    nothing, and a failure is on the record."""

    key: str = Field(description="SHA-256 of everything the model was asked: model, prompt version, system, messages")
    person: str | None = Field(description="Person.key; None for what no person says (a service's answer, a judgement)")
    service: str | None = Field(default=None, description="The declared service it was for, by its name")
    wrote: Wrote
    asked: EntityRef | None = Field(
        default=None, description="The message or item answered, or moved; None for a summary"
    )
    model: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    answer: str | None = Field(default=None, description="The structured answer, as its JSON text; None: it failed")
    failure: str | None = Field(default=None, description="Why the call failed; the reply stays owed")
    replayed: bool = Field(default=False, description="Answered from the world's record: no model was called")
    sim_time: AwareDatetime
    wake: int = Field(ge=0)
