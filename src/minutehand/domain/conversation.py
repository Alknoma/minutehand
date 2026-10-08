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
    """What a model wrote for a person."""

    REPLY = "reply"  # their words back to a message
    DECISION = "decision"  # their decision on an item in the agent's own product, and what they give with it
    SUMMARY = "summary"  # what they saw before the turns they are shown word for word
    TRANSITION = "transition"  # the transition they take on an item pending on them, and what it carries


class PersonCall(Model):
    """One call Minutehand made to a model to write what a person says: kept with the world, so a rerun or fork
    with the same context (`key`) replays its answer and calls nothing, and a failure is on the record."""

    key: str = Field(description="SHA-256 of everything the model was asked: model, prompt version, system, messages")
    person: str = Field(description="Person.key")
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
