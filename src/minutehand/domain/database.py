"""A database of the agent's own that Minutehand fronts: it listens where the agent's database URL points, relays
every connection to the real database, and records the agent's committed writes in the run's log. A fork puts the
database back by creating it again from the base taken at the run's start and replaying those writes, in order, up
to the checkpoint; nothing is asked of the agent.

    databases:
      - kind: postgres
        name: app
        listen: 127.0.0.1:6543
        upstream: postgres://agent:secret@127.0.0.1:5432/app
        env: DATABASE_URL                # set to postgres://agent:secret@127.0.0.1:6543/app for the agent's program
        base: {kind: template}           # CREATE DATABASE <base> TEMPLATE app, at the run's start

What the log holds for each fronted database (`EntityKind.DATABASE`, actor SCENARIO, as the run loop's own records
are): the base it starts from, each transaction the agent committed with every statement that changed something and
its parameters, how its sequences moved where a transaction that did not commit drew from them, and any point after
which the database can no longer be replayed (a COPY from the client, the function-call protocol), from which a fork
is refused. docs/design.md, "The agent's database, recorded at the wire".
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, model_validator

from minutehand.domain.scenario import Model

DATABASE_PROVIDER = "minutehand"
"""The provider every fronted database's records are written under, as the run loop's own are."""

SEALED_SSLMODES = ("require", "verify-ca", "verify-full")
"""The sslmodes a relay that never decrypts cannot serve: it speaks to the agent and to the database in the clear."""


class TemplateBase(Model):
    """The base is a copy of the agent's database made by PostgreSQL itself at the run's start:
    `CREATE DATABASE <base> TEMPLATE <database>`, a file-level copy. Quick for a database of a few gigabytes; it
    needs no other connection to the agent's database while it is made, so it is made before the agent starts."""

    kind: Literal["template"] = "template"


class CommandBase(Model):
    """The base is made and put back by commands of the agent's: a copy-on-write branch (Neon, a ZFS or btrfs
    snapshot, a cloud volume snapshot) for a database too large to copy. Each command gets MINUTEHAND_DB_URL (the
    upstream) and MINUTEHAND_DB_BASE.

    `take` runs at the run's start with MINUTEHAND_DB_BASE set to a name Minutehand suggests; the last line it
    prints names the base it made. `put_back` runs before a fork's replay with MINUTEHAND_DB_BASE set to that name,
    and must leave the agent's database exactly as the base holds it."""

    kind: Literal["command"] = "command"
    take: list[str] = Field(min_length=1)
    put_back: list[str] = Field(min_length=1)


DatabaseBase = Annotated[TemplateBase | CommandBase, Field(discriminator="kind")]


class Database(Model):
    """A PostgreSQL database of the agent's that Minutehand fronts. The relay speaks PostgreSQL's wire protocol in
    the clear only: a client asking for `sslmode=require` is told the server does not support SSL, and an upstream
    asking for it is refused here."""

    kind: Literal["postgres"] = "postgres"
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$", description="What the run's records call this database")
    listen: str = Field(
        pattern=r"^[A-Za-z0-9.\-]+:[0-9]{1,5}$", description="host:port the relay listens on for the agent"
    )
    upstream: str = Field(
        min_length=1,
        description="postgres://user:password@host:port/database: the real database, reached by the relay and by "
        "Minutehand's own connections (the base, the replay)",
    )
    env: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="A variable set, for the agent's program, to the upstream's URL pointed at the relay",
    )
    base: DatabaseBase = TemplateBase()

    @model_validator(mode="after")
    def _plain_postgres(self) -> Self:
        parts = urlsplit(self.upstream)
        if parts.scheme not in ("postgres", "postgresql"):
            raise ValueError(f"a database's upstream is postgres://, not {self.upstream!r}")
        if not parts.hostname or not parts.path.strip("/"):
            raise ValueError(f"a database's upstream names its host and its database: {self.upstream!r}")
        query = parse_qs(parts.query)
        modes = query["sslmode"] if "sslmode" in query else []
        if any(mode in SEALED_SSLMODES for mode in modes):
            raise ValueError(
                "the relay speaks to the database in the clear and never decrypts TLS, so an upstream with "
                f"sslmode={modes[0]} cannot be fronted"
            )
        return self

    @property
    def database(self) -> str:
        """The agent's database's name on the upstream server."""
        return urlsplit(self.upstream).path.strip("/")


def refuse_repeated_databases(databases: list[Database]) -> None:
    names = [d.name for d in databases]
    if len(names) != len(set(names)):
        raise ValueError(f"two databases share a name: {sorted(n for n in set(names) if names.count(n) > 1)}")
    listens = [d.listen for d in databases]
    if len(listens) != len(set(listens)):
        raise ValueError(
            f"two databases listen on one address: {sorted(a for a in set(listens) if listens.count(a) > 1)}"
        )


class StatementProtocol(StrEnum):
    SIMPLE = "simple"  # a Query message: the text, which may hold several statements
    EXTENDED = "extended"  # Parse, Bind and Execute: one statement and its parameters


class Executed(Model):
    """One statement the agent ran, as the relay saw it on the wire, and what the database answered."""

    protocol: StatementProtocol
    sql: str
    param_types: list[int] = Field(default=[], description="The type OIDs the agent's Parse named; 0 for unnamed")
    param_formats: list[int] = Field(default=[], description="One per parameter: 0 text, 1 binary, as on the wire")
    params: list[str | None] = Field(
        default=[], description="Each parameter: its text, or its bytes in hex when sent binary; None for NULL"
    )
    result_formats: list[int] = Field(default=[], description="The result format codes the agent's Bind asked for")
    tags: list[str] = Field(description="The CommandComplete tag of each statement it ran: `INSERT 0 1`")
    rows: str | None = Field(
        default=None, description="SHA-256 of the rows it returned (RETURNING), as sent; None when it returned none"
    )


class SequenceValue(Model):
    name: str = Field(description="schema.sequence")
    value: int | None = Field(description="Its last value; None for one never drawn from")


class BaseTaken(Model):
    """Where the database starts from: taken at the run's start, and shared by every fork of the run."""

    kind: Literal["base"] = "base"
    database: str
    base: str = Field(description="The base database's name (a template), or what the agent's `take` printed")
    how: Literal["template", "command"]
    seconds: float = Field(ge=0, description="How long taking it took")


class Committed(Model):
    """One transaction the agent committed that changed the database: its statements, in order, and the session
    settings it ran under, as `SET` statements."""

    kind: Literal["committed"] = "committed"
    database: str
    connection: int = Field(ge=1, description="Which of the agent's connections it ran on, counted from 1")
    settings: list[str] = Field(default=[], description="The SET statements the connection had run before it")
    statements: list[Executed] = Field(min_length=1)
    sequences: list[SequenceValue] = Field(default=[], description="Every sequence's last value once it committed")


class SequencesMoved(Model):
    """A transaction that did not commit drew from a sequence, which PostgreSQL does not roll back: the values
    are recorded, though the transaction is not, so a replay draws the same values after it."""

    kind: Literal["sequences"] = "sequences"
    database: str
    sequences: list[SequenceValue]


class NotReplayable(Model):
    """Something the relay cannot record or replay happened: a fork from any later checkpoint is refused."""

    kind: Literal["not_replayable"] = "not_replayable"
    database: str
    reason: str


DatabaseRecord = Annotated[BaseTaken | Committed | SequencesMoved | NotReplayable, Field(discriminator="kind")]


class Replayed(Model):
    """What one put-back did."""

    database: str
    base: str
    transactions: int = Field(ge=0)
    statements: int = Field(ge=0)
    seconds: float = Field(ge=0)
    diverged: str | None = Field(
        default=None,
        description="Where the replay first answered otherwise than the agent's statement did; None: nowhere",
    )
