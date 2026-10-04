"""What a simulated person does, and where it is delivered."""

from __future__ import annotations

from pydantic import AwareDatetime, Field

from minutehand.domain.conversation import Provenance
from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import EntityRef


class PersonReply(Model):
    """One reply from one person, decided once and stored with the run."""

    person: str = Field(description="Person.key")
    in_reply_to: EntityRef
    text: str
    at: AwareDatetime = Field(description="Simulated time the reply lands")
    written_by: Provenance | None = Field(
        default=None, description="The model and prompt that wrote it; None is scripted"
    )


class PersonMessage(Model):
    """Something a person says to the agent unprompted: the owner handing over the goal, or a direction."""

    person: str = Field(description="Person.key")
    text: str
    at: AwareDatetime = Field(description="Simulated time the message is sent")


class InboundTarget(Model):
    """Where a provider pushes events to the agent, the way the real service would."""

    provider: ProviderKey
    url: str
    secret_env: str | None = Field(
        default=None, description="Name of the variable the agent reads its signing secret from"
    )
