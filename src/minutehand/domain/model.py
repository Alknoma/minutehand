"""The base of every model: frozen, and an unknown field is an error."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    """Every model in this package: frozen, and an unknown field is an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")
