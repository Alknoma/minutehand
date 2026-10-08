"""What the store can say about its own bytes: what a run costs on disk, and what a sweep freed. None of it is the
world; it is how the world is kept."""

from __future__ import annotations

from pydantic import Field

from minutehand.domain.scenario import Model


class RunUsage(Model):
    """What one run costs on disk, in parts that do not overlap."""

    run_id: str
    rows: int = Field(ge=0, description="Bytes of the run's own rows, bodies kept inline included")
    bodies: int = Field(ge=0, description="Bytes of stored bodies no other run in the file refers to")


class Freed(Model):
    """What one sweep removed: stored bodies nothing referred to any more."""

    bodies: int = Field(ge=0)
    body_bytes: int = Field(ge=0)
