"""The files in the folders of the agent's own machine its agent file says to watch (`AgentUnderTest.watches`),
recorded as world events whenever they change: by the agent in a wake, or by a scenario's machine command."""

from __future__ import annotations

import os
from pathlib import Path

from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, FileSnapshot, Operation
from minutehand.ports.store import Store

PROVIDER = "machine"
"""The provider key a watched file is recorded under."""

Listing = dict[str, tuple[int, int]]
"""Absolute path to (size, modification time in nanoseconds)."""


def listing(folders: list[str]) -> Listing:
    """Every regular file under the folders, never following a symbolic link into another place."""
    found: Listing = {}
    for folder in folders:
        for root, dirs, files in os.walk(folder, followlinks=False):
            dirs.sort()
            for name in sorted(files):
                path = Path(root) / name
                try:
                    st = path.lstat()
                except OSError:
                    continue
                found[str(path)] = (st.st_size, st.st_mtime_ns)
    return found


class Watcher:
    """What the watched folders held when last looked at, and the changes since, written to the store."""

    def __init__(self, folders: list[str]) -> None:
        self._folders = folders
        self._seen: Listing = {}

    def look(self) -> None:
        """Take what the folders hold now as the starting point, recording nothing."""
        self._seen = listing(self._folders)

    def record(self, store: Store, actor: Actor) -> int:
        """Record every file created, changed or removed since the last look as `actor`'s change, and answer how
        many there were."""
        if not self._folders:
            return 0
        now = listing(self._folders)
        changed = 0
        for path in sorted(self._seen.keys() | now.keys()):
            before, after = self._seen.get(path), now.get(path)
            if before == after:
                continue
            ref = EntityRef(provider=PROVIDER, kind=EntityKind.FILE, external_id=path)
            if after is None:
                store.apply(Change(entity=ref, operation=Operation.DELETE, actor=actor))
            else:
                snapshot = FileSnapshot(path=path, size=after[0])
                store.apply(
                    Change(
                        entity=ref,
                        operation=Operation.CREATE if before is None else Operation.UPDATE,
                        actor=actor,
                        body=snapshot.model_dump_json(),
                        after=snapshot,
                    )
                )
            changed += 1
        self._seen = now
        return changed
