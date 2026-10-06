"""One connection of the agent's, followed message by message: which statements it ran, what each answered, and
which of them the database committed.

The relay feeds every message the agent sends (`client`) and every message the database answers (`server`), in the
order each side sent them. Answers come in the order the statements were sent, so a queue of statements sent and not
yet answered says which statement each answer belongs to. A ReadyForQuery ends a batch and says where the
connection stands: idle, in a transaction, or in a failed one. Only on the move to idle can a transaction's fate be
known: committed when its last ending tag is `COMMIT` (or, outside an explicit transaction, when the batch raised no
error), and abandoned otherwise. A committed transaction hands back every statement in it that changed something;
an abandoned one hands back nothing but the fact, since a sequence it drew from stays drawn.

Which statements change something is read from the database's own answer, the CommandComplete tag (`INSERT 0 1`),
never from the agent's SQL, with one exception: a `SELECT` whose text names a write (`SELECT nextval(...)`, a
`WITH ... INSERT`, `SELECT ... INTO`) is kept too, since its tag says `SELECT`. Keeping a read costs a replayed read.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from dataclasses import dataclass, field

from minutehand.adapters.database.postgres.wire import Fields, tag_of
from minutehand.domain.database import Executed, StatementProtocol

CHANGES = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "COPY",
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "COMMENT",
        "REFRESH",
        "CALL",
        "DO",
        "SECURITY",
        "IMPORT",
        "CLUSTER",
        "REINDEX",
        "SAVEPOINT",
        "RELEASE",
        "ROLLBACK",
    }
)
"""The first word of every CommandComplete tag of a statement that changes the database, or the shape of the
transaction it is in (a savepoint, released or rolled back to), which a replay must repeat in the same place."""

CONTROL = frozenset({"SAVEPOINT", "RELEASE", "ROLLBACK"})
"""Tags that change nothing by themselves: a transaction holding only these committed nothing."""

SETTINGS = frozenset({"SET", "RESET"})
"""Tags of a session setting, which a replay runs before each transaction of the same connection."""

WRITE_IN_SELECT = re.compile(r"\b(insert|update|delete|merge|into|nextval|setval|create)\b", re.IGNORECASE)


@dataclass
class _Statement:
    executed: Executed
    hashed: hashlib._Hash = field(default_factory=hashlib.sha256)
    any_rows: bool = False
    failed: bool = False
    copied_out: bool = False


@dataclass(frozen=True)
class _Sync:
    """Where a Sync was sent: the database answers it with a ReadyForQuery, and skips everything before it after an
    error."""


@dataclass(frozen=True)
class _Bound:
    sql: str
    param_types: list[int]
    param_formats: list[int]
    params: list[str | None]
    result_formats: list[int]


@dataclass(frozen=True)
class Commit:
    statements: list[Executed]
    settings: list[str]


@dataclass(frozen=True)
class Abandon:
    """A transaction, or a batch outside one, that wrote or failed and did not commit."""


Ended = Commit | Abandon | None


class Conversation:
    def __init__(self) -> None:
        self._prepared: dict[str, tuple[str, list[int]]] = {}
        self._portals: dict[str, _Bound] = {}
        self._waiting: deque[_Statement | _Sync] = deque()
        self._in_transaction = False
        self._held: list[Executed] = []
        self._batch: list[Executed] = []
        self._tags: list[str] = []
        self._failed = False
        self._failed_before = False
        """An error in an earlier batch of the transaction still open."""
        self._settings: list[str] = []
        self._refusals: list[str] = []

    def refusals(self) -> list[str]:
        """What happened since last asked that a replay cannot repeat."""
        found, self._refusals = self._refusals, []
        return found

    # -- what the agent sends -------------------------------------------------------------------------------

    def client(self, kind: bytes, payload: bytes) -> None:
        fields = Fields(payload)
        if kind == b"Q":
            sql = fields.cstr()
            self._waiting.append(_Statement(Executed(protocol=StatementProtocol.SIMPLE, sql=sql, tags=[])))
        elif kind == b"P":
            name, sql = fields.cstr(), fields.cstr()
            types = [fields.uint32() for _ in range(fields.int16())]
            self._prepared[name] = (sql, types)
        elif kind == b"B":
            self._bind(fields)
        elif kind == b"E":
            portal = fields.cstr()
            if portal in self._portals:
                bound = self._portals[portal]
                executed = Executed(
                    protocol=StatementProtocol.EXTENDED,
                    sql=bound.sql,
                    param_types=bound.param_types,
                    param_formats=bound.param_formats,
                    params=bound.params,
                    result_formats=bound.result_formats,
                    tags=[],
                )
            else:  # its Bind failed: the database skips it until the Sync, and so does this
                executed = Executed(protocol=StatementProtocol.EXTENDED, sql="", tags=[])
            self._waiting.append(_Statement(executed))
        elif kind == b"S":
            self._waiting.append(_Sync())
        elif kind == b"C":
            which, name = fields.byte(), fields.cstr()
            if which == b"S":
                self._prepared.pop(name, None)
            else:
                self._portals.pop(name, None)
        elif kind == b"F":
            self._refusals.append("the agent called a function by the function-call protocol, which is not recorded")

    def _bind(self, fields: Fields) -> None:
        portal, name = fields.cstr(), fields.cstr()
        codes = [fields.int16() for _ in range(fields.int16())]
        count = fields.int16()
        values = []
        for _ in range(count):
            length = fields.int32()
            values.append(None if length < 0 else fields.take(length))
        result_formats = [fields.int16() for _ in range(fields.int16())]
        formats = [_format(codes, i) for i in range(count)]
        sql, types = self._prepared[name] if name in self._prepared else ("", [])
        self._portals[portal] = _Bound(
            sql=sql,
            param_types=types,
            param_formats=formats,
            params=[_param(v, f) for v, f in zip(values, formats, strict=True)],
            result_formats=result_formats,
        )

    # -- what the database answers --------------------------------------------------------------------------

    def server(self, kind: bytes, payload: bytes) -> Ended:
        if kind == b"D":
            head = self._head()
            if head is not None:
                head.hashed.update(payload)
                head.any_rows = True
        elif kind == b"C":
            head = self._head()
            if head is not None:
                tag = tag_of(payload)
                head.executed = head.executed.model_copy(update={"tags": [*head.executed.tags, tag]})
                self._tags.append(tag)
                if head.executed.protocol is StatementProtocol.EXTENDED:
                    self._waiting.popleft()
                    self._answered(head)
        elif kind in (b"I", b"s"):  # an empty query; a portal suspended at its row limit
            head = self._head()
            if head is not None and head.executed.protocol is StatementProtocol.EXTENDED:
                self._waiting.popleft()
        elif kind == b"E":
            self._error()
        elif kind == b"H":
            head = self._head()
            if head is not None:
                head.copied_out = True
        elif kind in (b"G", b"W"):
            self._refusals.append("the agent copied rows in from its side (COPY ... FROM STDIN), which is not recorded")
        elif kind == b"Z":
            return self._ready(payload[:1])
        return None

    def _head(self) -> _Statement | None:
        if self._waiting and isinstance(self._waiting[0], _Statement):
            return self._waiting[0]
        return None

    def _error(self) -> None:
        self._failed = True
        head = self._head()
        if head is None:
            return
        if head.executed.protocol is StatementProtocol.SIMPLE:
            head.failed = True
            return
        # The database skips every extended message up to the next Sync: none of them will be answered.
        while self._waiting and isinstance(self._waiting[0], _Statement):
            self._waiting.popleft()

    def _answered(self, statement: _Statement) -> None:
        executed = statement.executed
        if statement.failed or statement.copied_out or not executed.tags:
            return
        words = [tag.split(" ")[0] for tag in executed.tags]
        if any(w == "PREPARE" and "TRANSACTION" in t for w, t in zip(words, executed.tags, strict=True)):
            self._refusals.append("the agent prepared a two-phase transaction, which is not recorded")
            return
        if all(w in SETTINGS for w in words) and not self._in_transaction:
            self._settings.append(executed.sql)
            return
        changed = any(w in CHANGES for w in words) or (
            any(w == "SELECT" for w in words) and WRITE_IN_SELECT.search(executed.sql) is not None
        )
        if changed or (self._in_transaction and any(w in SETTINGS for w in words)):
            rows = statement.hashed.hexdigest() if statement.any_rows else None
            self._batch.append(executed.model_copy(update={"rows": rows}))

    def _ready(self, status: bytes) -> Ended:
        head = self._head()
        if head is not None and head.executed.protocol is StatementProtocol.SIMPLE:
            self._waiting.popleft()
            self._answered(head)
        while self._waiting and isinstance(self._waiting[0], _Statement):
            self._waiting.popleft()  # answered by nothing: skipped after an error
        if self._waiting:
            self._waiting.popleft()  # the Sync this ReadyForQuery answers
        if status in (b"T", b"E"):
            self._held += self._batch
            self._in_transaction = True
            self._failed_before = self._failed_before or self._failed
            self._reset_batch()
            return None
        statements = self._held + self._batch
        began = any(t.split(" ")[0] in ("BEGIN", "START") for t in self._tags)
        if self._in_transaction or began:
            endings = [t for t in self._tags if t in ("COMMIT", "ROLLBACK")]
            committed = bool(endings) and endings[-1] == "COMMIT"
        else:
            committed = not self._failed
        failed = self._failed or self._failed_before
        self._held = []
        self._in_transaction = False
        self._failed_before = False
        self._reset_batch()
        if committed:
            kept = [s for s in statements if not all(t.split(" ")[0] in CONTROL for t in s.tags)]
            return Commit(statements=statements, settings=list(self._settings)) if kept else None
        return Abandon() if statements or failed else None

    def _reset_batch(self) -> None:
        self._batch = []
        self._tags = []
        self._failed = False


BINARY = 1
"""PostgreSQL's format code for a value sent as bytes; 0 is text."""


def _format(codes: list[int], i: int) -> int:
    """A parameter's format code: none given is text for all, one given is for all, else one each."""
    return 0 if not codes else codes[0] if len(codes) == 1 else codes[i]


def _param(value: bytes | None, code: int) -> str | None:
    if value is None:
        return None
    return value.hex() if code == BINARY else value.decode()


def param_bytes(value: str | None, code: int) -> bytes | None:
    """A recorded parameter as it goes back on the wire."""
    if value is None:
        return None
    return bytes.fromhex(value) if code == BINARY else value.encode()
