"""Names the contract shares across its parts: a provider's key, a signing secret, a window of a person's time."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from minutehand.domain.model import Model

ProviderKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
"""A provider's registry key ("slack", "asana"). Open, because the set of providers
is whatever is installed; it is checked against the registry when a scenario loads."""


VariableName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class GeneratedSecret(Model):
    """A fresh secret made for every run and handed to the agent's command in the variable `env`.

    Only a command Minutehand starts receives it; an agent already running has a secret of its own."""

    kind: Literal["generated"] = "generated"
    env: VariableName = Field(description="The variable the agent reads its signing secret from")


class SecretFromEnvironment(Model):
    """The agent's own secret, configured where it already runs: Minutehand reads the same value from its own
    variable `env` when the run starts, and refuses the run when it is not set."""

    kind: Literal["from_env"] = "from_env"
    env: VariableName = Field(description="The variable in Minutehand's own environment that holds the secret")


SigningSecret = Annotated[GeneratedSecret | SecretFromEnvironment, Field(discriminator="kind")]


class Window(Model):
    """When a person's answer lands: a moment drawn uniformly between `min` and `max` of the person's AVAILABLE
    time after the ask, the time inside their working hours and outside their absences. Two hours of available
    time asked at 16:00 on a Friday, of a person working 9 to 17 on weekdays, is 10:00 on Monday."""

    min: timedelta = Field(ge=timedelta(0))
    max: timedelta

    @model_validator(mode="after")
    def _ordered(self) -> Window:
        if self.max < self.min:
            raise ValueError(f"a window's max ({self.max}) is before its min ({self.min})")
        return self
