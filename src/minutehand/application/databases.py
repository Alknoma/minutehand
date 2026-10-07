"""The agent's databases Minutehand fronts (`domain.database`), kept in the run's own log and put back from it.

Each record a relay hears is one entity of `EntityKind.DATABASE`, actor SCENARIO as the run loop's own records are
(no check counts the agent's bookkeeping as its work in the world), written as it happens: the base at the run's
start, each committed transaction before the agent learns it committed, sequences moved by a transaction that did
not commit, and any point the database stops being replayable. A fork shares its parent's log up to its checkpoint,
so what it replays is exactly the records it can see: no side file, no copy per checkpoint. What the log grows by is
what the agent wrote, never the size of the database.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import TypeAdapter

from minutehand.application.refusals import RunRefused
from minutehand.domain.checks import Finding, FindingKind, Severity
from minutehand.domain.database import (
    DATABASE_PROVIDER,
    BaseTaken,
    Committed,
    DatabaseDigest,
    DatabaseRecord,
    NotReplayable,
    SequencesMoved,
)
from minutehand.domain.world import Actor, Change, EntityKind, EntityRef, Operation, WorldEvent
from minutehand.ports.database import FrontsDatabase
from minutehand.ports.store import Store

_RECORD: TypeAdapter[DatabaseRecord] = TypeAdapter(DatabaseRecord)

NAME_LIMIT = 63
"""PostgreSQL's longest identifier, in bytes."""
SHOWN = 200
"""Characters of a statement a finding shows."""


class Recorder:
    """What the relays hear, written into the run mounted now (`HearsWrites`). Mounted with the proxy, once per run,
    since a fork is a new run in the same file."""

    def __init__(self) -> None:
        self._store: Store | None = None
        self._holding = False
        self._made = 0

    def mount(self, store: Store) -> None:
        self._store = store
        self._holding = False
        self._made = sum(1 for e in store.events() if e.entity.kind is EntityKind.DATABASE)

    def hold(self) -> None:
        """Until the next mount, what is heard is let go: a fork's agent, started before its restore, writes to a
        database the restore is about to make again from the base, so nothing it writes then survives."""
        self._store = None
        self._holding = True

    def heard(self, record: DatabaseRecord) -> None:
        if self._store is None:
            if self._holding:
                return
            raise RunRefused(
                f"database {record.database} heard a {record.kind} record before any run was mounted to keep it"
            )
        self._made += 1
        ref = EntityRef(
            provider=DATABASE_PROVIDER, kind=EntityKind.DATABASE, external_id=f"{record.database}:{self._made}"
        )
        self._store.apply(
            Change(entity=ref, operation=Operation.CREATE, actor=Actor.SCENARIO, body=record.model_dump_json())
        )


@dataclass
class History:
    """One database's records as a run sees them, in log order, each with the seq it was written at."""

    base: BaseTaken | None = None
    replay: list[Committed | SequencesMoved] = field(default_factory=list)
    refused: list[tuple[int, NotReplayable]] = field(default_factory=list)


def history(store: Store, database: str, *, upto: int | None = None) -> History:
    """What `store` holds of `database`, up to and including seq `upto` when given."""
    found = History()
    for event in store.events():
        if event.entity.kind is not EntityKind.DATABASE or (upto is not None and event.seq > upto):
            continue
        stored = store.get(event.entity)
        if stored is None:
            continue
        record = _RECORD.validate_json(stored.body)
        if record.database != database:
            continue
        if isinstance(record, BaseTaken):
            found.base = record
        elif isinstance(record, NotReplayable):
            found.refused.append((event.seq, record))
        else:
            found.replay.append(record)
    return found


def records(store: Store) -> list[DatabaseRecord]:
    """Every database record a run sees, in log order."""
    return [
        _RECORD.validate_json(stored.body)
        for e in store.events()
        if e.entity.kind is EntityKind.DATABASE and (stored := store.get(e.entity)) is not None
    ]


def base_name(database: str, run_id: str) -> str:
    return f"{database}_mh_{run_id}"[:NAME_LIMIT]


async def take_bases(fronts: Sequence[FrontsDatabase], recorder: Recorder, run_id: str) -> None:
    """Each fronted database's base, taken before the agent starts and recorded in the run mounted."""
    for front in fronts:
        recorder.heard(await front.take_base(base_name(front.database.database, run_id)))


async def start_again(fronts: Sequence[FrontsDatabase], first: Store, recorder: Recorder) -> None:
    """A sample after the first starts from the base the first took: each database made again from it, with nothing
    replayed, and the same base recorded in the new run."""
    for front in fronts:
        found = history(first, front.database.name)
        if found.base is None:
            raise RunRefused(f"run {first.run_id} took no base of database {front.database.name} to start again from")
        await _put_back(front, found.base, [], first.run_id)
        recorder.heard(found.base)


def refuse_unreplayable(fronts: Sequence[FrontsDatabase], store: Store, at_seq: int) -> None:
    """A fork from `at_seq` is refused for a database with no base, or with something unreplayable at or before it."""
    for front in fronts:
        name = front.database.name
        found = history(store, name, upto=at_seq)
        if found.base is None:
            raise RunRefused(
                f"run {store.run_id} took no base of database {name}, so a fork cannot put it back: it was played "
                "before the agent file declared the database"
            )
        if found.refused:
            seq, first = found.refused[0]
            raise RunRefused(
                f"database {name} cannot be put back as it was at seq {at_seq}: at seq {seq} {first.reason}. "
                "Fork from a checkpoint before it"
            )


class PutBack:
    """Every fronted database as the run `store` sees it: made again from its base, with every transaction the
    agent committed before the run's head replayed (`restore.PutsBack`), and, for a database that declares
    `digest`, digested and compared with `digests`, those taken at the checkpoint."""

    def __init__(self, fronts: Sequence[FrontsDatabase], store: Store, digests: Sequence[DatabaseDigest] = ()) -> None:
        self._fronts = list(fronts)
        self._store = store
        self._digests = {d.database: d for d in digests}

    async def put_back(self) -> str:
        done: list[str] = []
        for front in self._fronts:
            name = front.database.name
            found = history(self._store, name)
            if found.base is None:
                raise RunRefused(f"run {self._store.run_id} holds no base of database {name}")
            said = await _put_back(front, found.base, found.replay, self._store.run_id)
            if front.database.digest is not None:
                said += "; " + await self._compare(front)
            done.append(said)
        return "; ".join(done)

    async def _compare(self, front: FrontsDatabase) -> str:
        name = front.database.name
        if name not in self._digests:
            raise RunRefused(
                f"database {name} declares `digest`, and no digest of it was taken at this checkpoint (the run was "
                "played before the agent file declared it), so what was put back cannot be compared"
            )
        recorded = self._digests[name]
        now = await front.digest()
        if now.digest != recorded.digest:
            raise RunRefused(
                f"database {name} was put back and its digest differs from the one taken at the checkpoint: "
                f"{recorded.digest[:12]} ({recorded.tables} tables, {recorded.rows} rows) then, {now.digest[:12]} "
                f"({now.tables} tables, {now.rows} rows) now. Every replayed statement answered as it did, so what "
                "differs was not returned to the agent: a value the database made up itself (now(), random(), "
                "gen_random_uuid()), or a write that did not pass the relay"
            )
        return f"its digest equals the checkpoint's ({now.tables} tables, {now.rows} rows)"


async def _put_back(
    front: FrontsDatabase, base: BaseTaken, replay: list[Committed | SequencesMoved], run_id: str
) -> str:
    replayed = await front.put_back(base, replay)
    if replayed.diverged is not None:
        raise RunRefused(
            f"database {replayed.database} was made again from its base {replayed.base}, and replaying what the "
            f"agent committed in run {run_id} parted from the record: {replayed.diverged}. The fork was refused "
            "rather than run against a database that is not the one at the checkpoint"
        )
    return (
        f"database {replayed.database} made again from base {replayed.base}, {replayed.transactions} "
        f"transaction(s) ({replayed.statements} statement(s)) replayed and matched, in {replayed.seconds:.2f} s"
    )


MADE_UP = re.compile(
    r"\b(?:(now|clock_timestamp|statement_timestamp|transaction_timestamp|timeofday|random|random_normal|setseed"
    r"|gen_random_uuid|uuid_generate_v1|uuid_generate_v1mc|uuid_generate_v4|nextval|txid_current|pg_current_xact_id"
    r"|pg_backend_pid|inet_client_addr|inet_client_port)\s*\("
    r"|(current_timestamp|current_date|current_time|localtimestamp|localtime)\b)",
    re.IGNORECASE,
)
"""Functions whose answer the database makes up when a statement runs: the machine's clock, a random number, a
random UUID, a sequence drawn by name, the transaction or backend running it. A replay calls them again."""

QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", re.DOTALL)
"""String literals, quoted identifiers and comments, where a function's name is only text."""

DEFAULT_BEFORE = re.compile(r"\bdefault\s*$", re.IGNORECASE)

DATABASE_CHECK = "database_replay"


def made_up(sql: str) -> list[str]:
    """What in `sql` the database makes up when it runs, by name: `now()`, `current_timestamp`, `random()`,
    `nextval()` drawn by name. A column default is named as one (`default now()`): it is made up again at every
    insert that leaves the column out. `nextval` as a default is left out: that is a serial column, whose
    sequence the replay puts back."""
    text = QUOTED.sub(lambda m: " " * len(m.group(0)), sql)
    found: list[str] = []
    for match in MADE_UP.finditer(text):
        name = (match.group(1) or match.group(2)).lower()
        default = DEFAULT_BEFORE.search(text[: match.start()]) is not None
        if default and name == "nextval":
            continue
        said = f"{name}()" if match.group(1) else name
        found.append(f"a column default {said}" if default else said)
    return list(dict.fromkeys(found))


def nondeterministic(store: Store) -> list[Finding]:
    """A finding for each distinct statement this run committed through a relay whose SQL calls something the
    database makes up when it runs (`made_up`): a replay calls it again and may get another answer. A write that
    returned the value refuses the fork that replays it; one that did not leaves the restored database silently
    different, which only a declared `digest` catches."""
    said: dict[tuple[str, str], tuple[list[str], list[int], WorldEvent, int]] = {}
    for event in store.events():
        if event.entity.kind is not EntityKind.DATABASE or event.run_id != store.run_id:
            continue
        stored = store.get(event.entity)
        if stored is None:
            continue
        record = _RECORD.validate_json(stored.body)
        if not isinstance(record, Committed):
            continue
        for statement in record.statements:
            names = made_up(statement.sql)
            if not names:
                continue
            key = (record.database, statement.sql)
            if key in said:
                said[key][1].append(event.seq)
            else:
                said[key] = (names, [event.seq], event, record.connection)
    findings: list[Finding] = []
    for (database, sql), (names, seqs, first, connection) in said.items():
        shown = " ".join(sql.split())
        shown = shown if len(shown) <= SHOWN else shown[:SHOWN] + "…"
        times = f"{len(seqs)} times in the run, first" if len(seqs) > 1 else "once,"
        findings.append(
            Finding(
                check=DATABASE_CHECK,
                severity=Severity.WARNING,
                kind=FindingKind.REVIEW,
                message=f"database {database}: the agent committed `{shown}` ({times} at seq {first.seq}, on its "
                f"connection {connection}), which calls {', '.join(names)}: a fork replays it and the database "
                "makes the value up again. A write that returned it refuses the fork; one that did not leaves the "
                "restored database silently different. Pass the value as a parameter (the wake's `now`), or declare "
                "`digest: {}` on the database so a fork compares what it put back",
                at=first.sim_time,
                wake=first.wake,
                evidence=seqs,
            )
        )
    return findings
