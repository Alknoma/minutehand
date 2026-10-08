"""What the agent remembers: JSON values under string keys, in named collections.

    from minutehand_agent import store

    store.configure(store.SqliteBackend("agent.db"))       # once, at start: where the memory lives in production

    store.put("asks/sam", {"status": "asked", "expected_by": "2026-09-03T09:00:00+00:00"})
    store.get("asks/sam")                                   # {"status": "asked", ...}; None when there is none
    store.list("asks/")                                     # [("asks/sam", {...}), ...], ordered by key
    store.query("asks/", where={"status": "asked"})         # those whose value has each field as given
    store.delete("asks/sam")
    with store.batch() as b:                                # all or none
        b.put("asks/sam", {"status": "confirmed"})
        b.delete("asks/rosa")
    jobs = store.collection("jobs")                         # a namespace of its own, with the same calls
    await store.aget("asks/sam")                            # every call has an async twin: aput, alist, ...

**Production** (MINUTEHAND_ON unset): every call passes to the backend `configure` named, through a three-method
adapter (`Backend`: `get`, `scan`, `write` over JSON text). Two ship: `SqliteBackend(path)`, shared by every process
of the agent that opens the same file, and `MemoryBackend()`. Any other database is an adapter of three methods
(`docs/agent-contract.md`, "Writing an adapter"). Nothing is recorded and nothing else happens.

**Under Minutehand** (MINUTEHAND_ON and MINUTEHAND_AGENT_URL set by `minutehand run`): the backend is the run's, and
the one `configure` named is never called, so the agent's real database is not opened. Each write is an entry in the
run's log, the agent's, at the wake and simulated moment it was made; each read is answered from the run as it
stands. A run starts from the scenario's `memory:` and nothing else; a fork starts from its parent's memory as it
stood at the checkpoint. If the run cannot be reached the call raises `MinutehandUnreachable`; it never falls back
to the agent's own database.

**What is not part of a run.** Only what goes through this store. Whatever the agent keeps anywhere else (its own
database, files, a cache, a variable in a process that outlives a wake) is not simulated, not kept apart between
runs, and not put back by a fork: a later call can then depend on state from another moment or another run.
Minutehand reports what it can see of that (`docs/design.md`, "The agent's memory"): the store's reads and writes in
every wake, a fresh empty database for each run in every variable the agent file names under `own_databases`, and a
fork refused when the agent's report after it is not the one recorded at its checkpoint.
"""

from __future__ import annotations

from minutehand_agent._store import (
    DEFAULT,
    Backend,
    Batch,
    Delete,
    MemoryBackend,
    NotConfigured,
    Put,
    SqliteBackend,
    Store,
    Write,
    configure,
)
from minutehand_agent._wire import MinutehandRefused, MinutehandUnreachable

__all__ = [
    "DEFAULT",
    "Backend",
    "Batch",
    "Delete",
    "MemoryBackend",
    "MinutehandRefused",
    "MinutehandUnreachable",
    "NotConfigured",
    "Put",
    "SqliteBackend",
    "Store",
    "Write",
    "adelete",
    "aget",
    "alist",
    "aput",
    "aquery",
    "batch",
    "collection",
    "configure",
    "delete",
    "get",
    "list",
    "put",
    "query",
]

_DEFAULT = Store()

get = _DEFAULT.get
put = _DEFAULT.put
delete = _DEFAULT.delete
list = _DEFAULT.list
query = _DEFAULT.query
batch = _DEFAULT.batch
collection = _DEFAULT.collection
aget = _DEFAULT.aget
aput = _DEFAULT.aput
adelete = _DEFAULT.adelete
alist = _DEFAULT.alist
aquery = _DEFAULT.aquery
