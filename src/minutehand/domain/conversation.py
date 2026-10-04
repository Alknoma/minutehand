"""What Minutehand says to a language model, and what it keeps of the answer.

A model writes the replies of people whose `reply` is `Answers`, and judges what no deterministic check can.
Either way the record keeps which model answered and under which version of which prompt, so a finding or a
reply can be traced to the words that produced it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from minutehand.domain.scenario import Model


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
