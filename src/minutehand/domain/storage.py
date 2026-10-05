"""What the store can say about its own bytes: the agent's snapshots it keeps, what a run costs on disk, and what
a sweep freed. None of it is the world; it is how the world is kept."""

from __future__ import annotations

from pydantic import Field

from minutehand.domain.scenario import Model


class AgentSnapshot(Model):
    """The agent's own state at the end of one wake, as the snapshot command wrote it, kept as a manifest of
    files in a pool shared by every snapshot in the same world file."""

    run_id: str = Field(description="The run that took it: `Restorable.snapshot_of`")
    wake: int = Field(ge=0)
    files: int = Field(ge=0, description="Regular files in the directory the snapshot command filled")
    size: int = Field(ge=0, description="Their bytes, as the snapshot command wrote them")
    held: int = Field(
        ge=0,
        description="Bytes on disk that only this snapshot refers to: what pruning it would free. A file it shares "
        "with another snapshot is counted by neither",
    )
    pinned: bool = Field(description="Kept whatever the agent's `StateHooks.keep` says")
    pruned: bool = Field(description="Its files were let go: a fork from its checkpoint is refused")


class RunUsage(Model):
    """What one run costs on disk, in parts that do not overlap."""

    run_id: str
    rows: int = Field(ge=0, description="Bytes of the run's own rows, bodies kept inline included")
    bodies: int = Field(ge=0, description="Bytes of stored bodies no other run in the file refers to")
    snapshots: int = Field(ge=0, description="Bytes of snapshot files no other run's snapshot refers to")


class Freed(Model):
    """What one sweep removed: stored bodies and snapshot files nothing referred to any more."""

    bodies: int = Field(ge=0)
    body_bytes: int = Field(ge=0)
    files: int = Field(ge=0)
    file_bytes: int = Field(ge=0)
