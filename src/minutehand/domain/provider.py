"""What a provider declares about itself. Data only: loading a manifest imports no provider code."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from minutehand.domain.scenario import Model, ProviderKey
from minutehand.domain.world import EntityKind


class Tier(StrEnum):
    """How much of the real service a provider reproduces."""

    OBSERVED = "observed"  # calls pass through to the real service and are recorded
    GENERATED = "generated"  # stateful create/read/update/delete built from the service's API description
    TAUGHT = "taught"  # generated, then corrected against recorded real traffic
    FINISHED = "finished"  # hand-finished: refusals, pushed events, sign-in, known quirks


class Manifest(Model):
    key: ProviderKey
    tier: Tier
    hosts: list[str] = Field(min_length=1, description="Exact hosts or '*.' wildcards this provider answers for")
    path_prefix: str = Field(default="", description="Prefix the real API serves under, e.g. '/api/1.0'")
    kinds: list[EntityKind] = Field(default=[], description="Entity kinds it maps; empty means records only")
    pushes_events: bool = False
    books_wakes: bool = Field(
        default=False, description="True for a scheduler: what the agent books here becomes a wake"
    )
