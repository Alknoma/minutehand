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


class TicketField(StrEnum):
    """A `SeededTicket` field not every ticket provider can hold."""

    KEY = "key"  # the scenario's own name for the ticket, by which the provider's own seed names it
    LABELS = "labels"
    COMMENTS = "comments"


class DocumentChange(StrEnum):
    """What a person can do to a seeded document (`DocumentHappening.action`); not every document provider can show
    every one."""

    EDITED = "edited"
    RENAMED = "renamed"
    MOVED = "moved"
    SHARED = "shared"
    TRASHED = "trashed"
    COMMENTED = "commented"
    FIELD_SET = "field_set"


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
    ticket_fields: list[TicketField] = Field(
        default=[],
        description="The optional `SeededTicket` fields its tickets hold; a scenario that sets any other on one of "
        "its tickets is refused at load, since the provider would drop it in silence",
    )
    document_changes: list[DocumentChange] = Field(
        default=[],
        description="What a person can do to its seeded documents; a scenario whose document happening does any "
        "other is refused at load, since the provider could not show it",
    )
