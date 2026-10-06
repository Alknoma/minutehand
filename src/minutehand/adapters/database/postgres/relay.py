"""A PostgreSQL database of the agent's, fronted: a relay that listens where the agent's database URL points and
passes every connection on to the real database, recording what the agent committed, and Minutehand's own
connections to take the base and to put the database back.

The relay never decrypts: it answers an SSL or GSS encryption request with `N` (the server does not speak it), so a
client that insists on TLS (`sslmode=require`) stops there with its driver's own error, and one that prefers it
carries on in the clear. Every other byte goes through unchanged, authentication included: the agent signs in to
the real database with its own credentials.

A transaction the agent commits is handed to the run (`HearsWrites`) before the ReadyForQuery that tells the agent
it committed is passed on, so nothing the agent does after a commit, a checkpoint included, can come before the
commit's record. With it go the database's sequences as they stand, read on a connection of Minutehand's own,
which is how a replay knows its sequences drew what the agent's did.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Sequence
from urllib.parse import urlsplit, urlunsplit

from minutehand.adapters.database.postgres import wire
from minutehand.adapters.database.postgres.client import (
    Address,
    Connection,
    ServerRefused,
    quote_ident,
    quote_literal,
)
from minutehand.adapters.database.postgres.conversation import Abandon, Commit, Conversation
from minutehand.application.refusals import RunRefused
from minutehand.domain.database import (
    BaseTaken,
    CommandBase,
    Committed,
    Database,
    NotReplayable,
    Replayed,
    SequencesMoved,
    SequenceValue,
)
from minutehand.ports.database import HearsWrites

SEQUENCES = (
    "SELECT schemaname || '.' || sequencename, last_value::text FROM pg_sequences "
    "WHERE schemaname NOT LIKE 'pg_temp%' ORDER BY 1"
)
BASE_ENV = "MINUTEHAND_DB_BASE"
URL_ENV = "MINUTEHAND_DB_URL"
COMMAND_LIMIT = 600.0
"""Seconds a base's `take` or `put_back` command may run: a copy-on-write branch is quick, a copy is not."""
SHOWN = 200
"""Characters of a statement shown where a replay parted from the record."""


class PostgresFront:
    def __init__(self, database: Database, hears: HearsWrites) -> None:
        self._database = database
        self._hears = hears
        self._address = Address.of(database.upstream)
        self._server: asyncio.Server | None = None
        self._connections = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._watcher: Connection | None = None
        self._watching = asyncio.Lock()
        self._known: list[SequenceValue] = []

    @property
    def database(self) -> Database:
        return self._database

    def agent_url(self) -> str:
        """The upstream's URL with the relay's address in place of the database's: what the agent connects to."""
        parts = urlsplit(self._database.upstream)
        user = parts.netloc.rpartition("@")[0]
        return urlunsplit(parts._replace(netloc=f"{user}@{self._database.listen}" if user else self._database.listen))

    async def start(self) -> None:
        host, _, port = self._database.listen.rpartition(":")
        try:
            self._server = await asyncio.start_server(self._serve, host, int(port))
        except OSError as e:
            raise RunRefused(
                f"the relay for database {self._database.name} could not listen on {self._database.listen}: {e}"
            ) from e

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        await self._unwatch()

    # -- the relay ----------------------------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        upstream: asyncio.StreamWriter | None = None
        try:
            packet = await wire.read_startup(reader)
            while wire.startup_code(packet) in (wire.SSL_REQUEST, wire.GSSENC_REQUEST):
                writer.write(wire.NO_ENCRYPTION)
                await writer.drain()
                packet = await wire.read_startup(reader)
            up_reader, upstream = await asyncio.open_connection(self._address.host, self._address.port)
            upstream.write(packet)
            await upstream.drain()
            if wire.startup_code(packet) == wire.CANCEL_REQUEST:
                return
            self._connections += 1
            conversation = Conversation()
            pumps = [
                asyncio.create_task(self._from_agent(reader, upstream, conversation)),
                asyncio.create_task(self._from_database(up_reader, writer, conversation, self._connections)),
            ]
            try:
                done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                for pump in pumps:
                    with contextlib.suppress(asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError):
                        await pump
            for pump in done:
                failure = pump.exception() if not pump.cancelled() else None
                if failure is not None and not isinstance(failure, asyncio.IncompleteReadError | ConnectionError):
                    raise failure
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for side in (writer, upstream):
                if side is not None:
                    side.close()
            self._tasks.discard(task)

    async def _from_agent(
        self, reader: asyncio.StreamReader, upstream: asyncio.StreamWriter, conversation: Conversation
    ) -> None:
        while True:
            kind, payload = await wire.read_message(reader)
            conversation.client(kind, payload)
            self._refuse(conversation)
            upstream.write(wire.frame(kind, payload))
            await upstream.drain()
            if kind == b"X":
                return

    async def _from_database(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, conversation: Conversation, connection: int
    ) -> None:
        while True:
            kind, payload = await wire.read_message(reader)
            ended = conversation.server(kind, payload)
            self._refuse(conversation)
            if isinstance(ended, Commit):
                sequences = await self._sequences()
                self._hears.heard(
                    Committed(
                        database=self._database.name,
                        connection=connection,
                        settings=ended.settings,
                        statements=ended.statements,
                        sequences=sequences,
                    )
                )
                self._known = sequences
            elif isinstance(ended, Abandon):
                sequences = await self._sequences()
                if sequences != self._known:
                    self._hears.heard(SequencesMoved(database=self._database.name, sequences=sequences))
                    self._known = sequences
            writer.write(wire.frame(kind, payload))
            transport = writer.transport
            if kind == b"Z" or transport.get_write_buffer_size() > 1 << 16:
                await writer.drain()

    def _refuse(self, conversation: Conversation) -> None:
        for reason in conversation.refusals():
            self._hears.heard(NotReplayable(database=self._database.name, reason=reason))

    async def _sequences(self) -> list[SequenceValue]:
        async with self._watching:
            if self._watcher is None:
                self._watcher = await Connection.open(self._address)
            answer = await self._watcher.must(SEQUENCES)
        return [SequenceValue(name=name or "", value=int(v) if v is not None else None) for name, v in answer.values]

    async def _unwatch(self) -> None:
        async with self._watching:
            if self._watcher is not None:
                await self._watcher.close()
                self._watcher = None

    # -- the base and the put-back ------------------------------------------------------------------------------

    async def take_base(self, suggested: str) -> BaseTaken:
        began = time.monotonic()
        await self._unwatch()
        base = self._database.base
        if isinstance(base, CommandBase):
            printed = await _command(base.take, {URL_ENV: self._database.upstream, BASE_ENV: suggested})
            lines = [line.strip() for line in printed.splitlines() if line.strip()]
            if not lines:
                raise RunRefused(f"the `take` command of database {self._database.name} printed no base name")
            name, how = lines[-1], "command"
        else:
            name, how = suggested, "template"
            await self._maintain(
                f"CREATE DATABASE {quote_ident(name)} TEMPLATE {quote_ident(self._database.database)}",
                hint="a template is copied only while no other connection to it is open: Minutehand takes the base "
                "before it starts the agent, so an agent started by hand must be started after the run begins",
            )
        self._known = await self._sequences()
        return BaseTaken(database=self._database.name, base=name, how=how, seconds=round(time.monotonic() - began, 3))

    async def put_back(self, base: BaseTaken, history: Sequence[Committed | SequencesMoved]) -> Replayed:
        began = time.monotonic()
        await self._unwatch()
        for task in list(self._tasks):  # the agent's connections are to a database about to be replaced
            task.cancel()
        own = self._database.base
        if isinstance(own, CommandBase):
            await _command(own.put_back, {URL_ENV: self._database.upstream, BASE_ENV: base.base})
        else:
            name = quote_ident(self._database.database)
            await self._maintain(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            await self._maintain(f"CREATE DATABASE {name} TEMPLATE {quote_ident(base.base)}")
        transactions = statements = 0
        diverged: str | None = None
        connection = await Connection.open(self._address)
        try:
            for n, item in enumerate(history, start=1):
                if isinstance(item, SequencesMoved):
                    for sequence in item.sequences:
                        if sequence.value is not None:
                            schema, _, name = sequence.name.partition(".")
                            target = quote_literal(f"{quote_ident(schema)}.{quote_ident(name)}")
                            await connection.must(f"SELECT setval({target}, {sequence.value}, true)")
                    continue
                diverged = await _replay(connection, item, n, len(history))
                if diverged is not None:
                    break
                transactions += 1
                statements += len(item.statements)
        finally:
            await connection.close()
        self._known = await self._sequences()
        return Replayed(
            database=self._database.name,
            base=base.base,
            transactions=transactions,
            statements=statements,
            seconds=round(time.monotonic() - began, 3),
            diverged=diverged,
        )

    async def _maintain(self, sql: str, *, hint: str = "") -> None:
        """One statement on the server's `postgres` database, where databases are made and dropped."""
        try:
            connection = await Connection.open(self._address.on("postgres"))
        except (OSError, ServerRefused) as e:
            raise RunRefused(
                f"database {self._database.name}: Minutehand could not reach {self._address.host}: {e}"
            ) from e
        try:
            answer = await connection.run(sql)
        finally:
            await connection.close()
        if answer.error is not None:
            raise RunRefused(
                f"database {self._database.name}: {sql} failed: {answer.error}" + (f". {hint}" if hint else "")
            )


async def _replay(connection: Connection, item: Committed, n: int, of: int) -> str | None:
    """One committed transaction again, inside a transaction of its own; the first way it answers otherwise than
    the agent's did, or None."""
    where = f"transaction {n} of {of} (the agent's connection {item.connection})"
    await connection.must("RESET ALL")
    for setting in item.settings:
        await connection.must(setting)
    await connection.must("BEGIN")
    for i, statement in enumerate(item.statements, start=1):
        answer = await connection.replay(statement)
        shown = statement.sql if len(statement.sql) <= SHOWN else statement.sql[:SHOWN] + "…"
        said = f"{where}, statement {i}: `{shown}`"
        if answer.error is not None:
            await connection.run("ROLLBACK")
            return f"{said} failed on replay: {answer.error}"
        if answer.tags != statement.tags:
            await connection.run("ROLLBACK")
            return f"{said} answered {answer.tags} on replay; the agent's answered {statement.tags}"
        if answer.rows != statement.rows:
            await connection.run("ROLLBACK")
            return (
                f"{said} returned other rows on replay than it returned the agent: a value the database made up "
                "itself (now(), random(), a sequence drawn outside a recorded write) differs"
            )
    committed = await connection.must("COMMIT")
    if committed.tags != ["COMMIT"]:
        return f"{where} did not commit on replay: {committed.tags}"
    if item.sequences:
        answer = await connection.must(SEQUENCES)
        now = {name: v for name, v in answer.values}
        for sequence in item.sequences:
            held = now[sequence.name] if sequence.name in now else None
            if held != (str(sequence.value) if sequence.value is not None else None):
                return (
                    f"after {where}, sequence {sequence.name} stands at {held} on replay; it stood at "
                    f"{sequence.value} when the agent committed: something drew from it that was not recorded "
                    "(a SELECT of a function that draws, or a statement from outside the agent)"
                )
    return None


async def _command(argv: list[str], env: dict[str, str]) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env={**os.environ, **env},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        raise RunRefused(f"{argv[0]} could not be started: {e}") from e
    try:
        out, _ = await asyncio.wait_for(process.communicate(), COMMAND_LIMIT)
    except TimeoutError as e:
        process.kill()
        await process.wait()
        raise RunRefused(f"{argv[0]} did not finish within {COMMAND_LIMIT:g} s") from e
    text = out.decode(errors="replace")
    if process.returncode != 0:
        raise RunRefused(f"{' '.join(argv)} exited {process.returncode}: {text.strip()[-2000:]}")
    return text
