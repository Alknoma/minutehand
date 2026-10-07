"""The relay's reading of PostgreSQL's protocol, message by message, with no database: which statements it keeps,
which transactions it calls committed, and what it refuses to pretend it can replay."""

from __future__ import annotations

import hashlib
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from minutehand.adapters.database.postgres import wire
from minutehand.adapters.database.postgres.conversation import (
    Abandon,
    Commit,
    Conversation,
    Ended,
    startup_settings,
)
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.application.databases import DATABASE_CHECK, Recorder, made_up, nondeterministic
from minutehand.application.run_clock import RunClock
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.checks import FindingKind
from minutehand.domain.database import Committed, Database, Executed, StatementProtocol


def _split(message: bytes) -> tuple[bytes, bytes]:
    return message[:1], message[5:]


def send(conversation: Conversation, *messages: bytes) -> None:
    for message in messages:
        conversation.client(*_split(message))


def answer(conversation: Conversation, *messages: bytes) -> list[Ended]:
    return [conversation.server(*_split(message)) for message in messages]


def done(tag: str) -> bytes:
    return wire.frame(b"C", tag.encode() + b"\0")


def ready(status: bytes) -> bytes:
    return wire.frame(b"Z", status)


def row(*values: bytes) -> bytes:
    return wire.frame(b"D", struct.pack(">h", len(values)) + b"".join(struct.pack(">i", len(v)) + v for v in values))


ERROR = wire.frame(b"E", b"SERROR\0Mduplicate key value\0C23505\0\0")


def simple(conversation: Conversation, sql: str, *replies: bytes) -> Ended:
    send(conversation, wire.query(sql))
    return answer(conversation, *replies)[-1]


def test_an_insert_outside_a_transaction_is_committed_with_its_tag() -> None:
    c = Conversation()
    ended = simple(c, "INSERT INTO note VALUES (1)", done("INSERT 0 1"), ready(b"I"))
    assert isinstance(ended, Commit)
    [statement] = ended.statements
    assert (statement.protocol, statement.sql, statement.tags) == (
        StatementProtocol.SIMPLE,
        "INSERT INTO note VALUES (1)",
        ["INSERT 0 1"],
    )


def test_a_transaction_is_committed_only_at_its_commit_and_with_every_write_in_order() -> None:
    c = Conversation()
    assert simple(c, "BEGIN", done("BEGIN"), ready(b"T")) is None
    assert simple(c, "INSERT INTO note VALUES (1)", done("INSERT 0 1"), ready(b"T")) is None
    assert simple(c, "SELECT * FROM note", row(b"1"), done("SELECT 1"), ready(b"T")) is None
    assert simple(c, "UPDATE note SET id = 2", done("UPDATE 1"), ready(b"T")) is None
    ended = simple(c, "COMMIT", done("COMMIT"), ready(b"I"))
    assert isinstance(ended, Commit)
    assert [s.sql for s in ended.statements] == ["INSERT INTO note VALUES (1)", "UPDATE note SET id = 2"]


def test_a_rolled_back_transaction_records_nothing_and_says_it_was_abandoned() -> None:
    c = Conversation()
    simple(c, "BEGIN", done("BEGIN"), ready(b"T"))
    simple(c, "INSERT INTO note VALUES (1)", done("INSERT 0 1"), ready(b"T"))
    assert isinstance(simple(c, "ROLLBACK", done("ROLLBACK"), ready(b"I")), Abandon)
    # and the next transaction starts clean
    ended = simple(c, "INSERT INTO note VALUES (2)", done("INSERT 0 1"), ready(b"I"))
    assert isinstance(ended, Commit) and [s.sql for s in ended.statements] == ["INSERT INTO note VALUES (2)"]


def test_a_commit_of_a_failed_transaction_is_a_rollback() -> None:
    c = Conversation()
    simple(c, "BEGIN", done("BEGIN"), ready(b"T"))
    simple(c, "INSERT INTO note VALUES (1)", done("INSERT 0 1"), ready(b"T"))
    simple(c, "INSERT INTO note VALUES (1)", ERROR, ready(b"E"))
    assert isinstance(simple(c, "COMMIT", done("ROLLBACK"), ready(b"I")), Abandon)


def test_a_failed_write_outside_a_transaction_is_abandoned_since_it_may_have_drawn_from_a_sequence() -> None:
    c = Conversation()
    assert isinstance(simple(c, "INSERT INTO note VALUES (1)", ERROR, ready(b"I")), Abandon)


def test_a_read_is_not_kept_and_a_select_that_draws_from_a_sequence_is() -> None:
    c = Conversation()
    assert simple(c, "SELECT * FROM note", row(b"1"), done("SELECT 1"), ready(b"I")) is None
    ended = simple(c, "SELECT nextval('note_id_seq')", row(b"7"), done("SELECT 1"), ready(b"I"))
    assert isinstance(ended, Commit)
    assert ended.statements[0].rows == hashlib.sha256(row(b"7")[5:]).hexdigest()


def test_extended_statements_keep_their_parameters_text_and_binary_and_a_prepared_statement_is_reused() -> None:
    c = Conversation()
    send(
        c,
        wire.parse("s1", "INSERT INTO note VALUES ($1, $2) RETURNING id", [23, 25]),
        wire.bind("", "s1", [1, 0], [struct.pack(">i", 5), b"five"], [0]),
        wire.execute(""),
        wire.bind("", "s1", [1, 0], [struct.pack(">i", 6), None], [0]),
        wire.execute(""),
        wire.sync(),
    )
    results = answer(
        c,
        wire.frame(b"1"),
        wire.frame(b"2"),
        row(b"5"),
        done("INSERT 0 1"),
        wire.frame(b"2"),
        row(b"6"),
        done("INSERT 0 1"),
        ready(b"I"),
    )
    ended = results[-1]
    assert isinstance(ended, Commit)
    first, second = ended.statements
    assert first.protocol is StatementProtocol.EXTENDED
    assert (first.param_types, first.param_formats, first.params) == ([23, 25], [1, 0], ["00000005", "five"])
    assert second.params == ["00000006", None]
    assert first.rows == hashlib.sha256(row(b"5")[5:]).hexdigest()
    assert first.rows != second.rows


def test_after_an_extended_error_the_statements_up_to_the_sync_are_skipped_and_the_next_batch_is_read_right() -> None:
    c = Conversation()
    send(
        c,
        wire.parse("", "INSERT INTO note VALUES ($1)", [0]),
        wire.bind("", "", [], [b"1"], []),
        wire.execute(""),
        wire.parse("", "INSERT INTO note VALUES ($1)", [0]),
        wire.bind("", "", [], [b"2"], []),
        wire.execute(""),
        wire.sync(),
        wire.parse("", "UPDATE note SET id = $1", [0]),
        wire.bind("", "", [], [b"3"], []),
        wire.execute(""),
        wire.sync(),
    )
    first = answer(c, wire.frame(b"1"), wire.frame(b"2"), ERROR, ready(b"I"))
    assert isinstance(first[-1], Abandon)
    second = answer(c, wire.frame(b"1"), wire.frame(b"2"), done("UPDATE 1"), ready(b"I"))
    ended = second[-1]
    assert isinstance(ended, Commit) and [s.params for s in ended.statements] == [["3"]]


def test_a_savepoint_rolled_back_to_is_kept_in_place_so_the_replay_undoes_the_same_writes() -> None:
    c = Conversation()
    simple(c, "BEGIN", done("BEGIN"), ready(b"T"))
    simple(c, "INSERT INTO note VALUES (1)", done("INSERT 0 1"), ready(b"T"))
    simple(c, "SAVEPOINT a", done("SAVEPOINT"), ready(b"T"))
    simple(c, "INSERT INTO note VALUES (2)", done("INSERT 0 1"), ready(b"T"))
    simple(c, "ROLLBACK TO SAVEPOINT a", done("ROLLBACK"), ready(b"T"))
    ended = simple(c, "COMMIT", done("COMMIT"), ready(b"I"))
    assert isinstance(ended, Commit)
    assert [s.sql for s in ended.statements] == [
        "INSERT INTO note VALUES (1)",
        "SAVEPOINT a",
        "INSERT INTO note VALUES (2)",
        "ROLLBACK TO SAVEPOINT a",
    ]


def test_a_session_setting_goes_with_every_later_transaction_of_its_connection() -> None:
    c = Conversation()
    assert simple(c, "SET search_path TO app", done("SET"), ready(b"I")) is None
    ended = simple(c, "INSERT INTO note VALUES (1)", done("INSERT 0 1"), ready(b"I"))
    assert isinstance(ended, Commit) and ended.settings == ["SET search_path TO app"]


def test_a_copy_from_the_client_and_a_function_call_are_refused_as_unreplayable() -> None:
    c = Conversation()
    simple(c, "COPY note FROM STDIN", wire.frame(b"G", b"\0\0\0"))
    send(c, wire.frame(b"F", b"\0\0\0\1"))
    refused = c.refusals()
    assert len(refused) == 2
    assert "COPY ... FROM STDIN" in refused[0] and "function-call" in refused[1]
    assert c.refusals() == []


def test_a_copy_out_to_the_client_is_a_read() -> None:
    c = Conversation()
    assert simple(c, "COPY note TO STDOUT", wire.frame(b"H", b"\0\0\0"), done("COPY 3"), ready(b"I")) is None


def test_an_upstream_that_insists_on_tls_is_refused() -> None:
    with pytest.raises(ValidationError, match="never decrypts TLS"):
        Database(name="app", listen="127.0.0.1:6543", upstream="postgres://a:b@db:5432/app?sslmode=require")


def test_an_agent_file_with_two_databases_on_one_address_is_refused() -> None:
    database = {"kind": "postgres", "listen": "127.0.0.1:6543", "upstream": "postgres://a:b@db:5432/app"}
    with pytest.raises(ValidationError, match="listen on one address"):
        AgentUnderTest.model_validate(
            {
                "name": "a",
                "wakes": [{"kind": "command", "argv": ["true"]}],
                "databases": [{**database, "name": "one"}, {**database, "name": "two"}],
            }
        )


def test_a_write_returning_under_a_row_limit_is_kept_though_the_database_sends_no_tag() -> None:
    # asyncpg's fetchval: Execute asks for one row; the insert runs whole and the database answers PortalSuspended.
    c = Conversation()
    send(
        c,
        wire.parse("__asyncpg_stmt_1__", "INSERT INTO note(body) VALUES ($1) RETURNING id", []),
        wire.frame(b"D", b"S__asyncpg_stmt_1__\0"),
        wire.frame(b"H"),
    )
    assert answer(c, wire.frame(b"1"), wire.frame(b"t", b"\0\1\0\0\0\x19"), wire.frame(b"T", b"\0\0")) == [None] * 3
    send(c, wire.bind("", "__asyncpg_stmt_1__", [1], [b"hi"], [1]), wire.execute("", 1), wire.sync())
    ended = answer(c, wire.frame(b"2"), row(b"\0\0\0\7"), wire.frame(b"s"), ready(b"I"))[-1]
    assert isinstance(ended, Commit)
    [statement] = ended.statements
    assert (statement.max_rows, statement.suspended, statement.tags) == (1, True, [])
    assert statement.rows == hashlib.sha256(row(b"\0\0\0\7")[5:]).hexdigest()


def test_a_read_under_a_row_limit_is_not_kept_and_a_portal_continued_is_not_a_second_statement() -> None:
    c = Conversation()
    simple(c, "BEGIN", done("BEGIN"), ready(b"T"))
    send(c, wire.parse("", "SELECT id FROM note", []), wire.bind("c1", "", [], [], []), wire.execute("c1", 1))
    send(c, wire.sync())
    assert answer(c, wire.frame(b"1"), wire.frame(b"2"), row(b"1"), wire.frame(b"s"), ready(b"T"))[-1] is None
    send(c, wire.parse("", "INSERT INTO note VALUES (2) RETURNING id", []), wire.bind("c2", "", [], [], []))
    send(c, wire.execute("c2", 1), wire.sync())
    answer(c, wire.frame(b"1"), wire.frame(b"2"), row(b"2"), wire.frame(b"s"), ready(b"T"))
    send(c, wire.execute("c2", 1), wire.sync())  # the rest of its rows: the insert does not run again
    answer(c, done("INSERT 0 1"), ready(b"T"))
    ended = simple(c, "COMMIT", done("COMMIT"), ready(b"I"))
    assert isinstance(ended, Commit)
    assert [s.sql for s in ended.statements] == ["INSERT INTO note VALUES (2) RETURNING id"]


def test_settings_from_the_startup_packet_go_with_every_transaction_and_reset_all_puts_them_back() -> None:
    packet = wire.startup_message(
        [
            ("user", "agent"),
            ("database", "app"),
            ("client_encoding", "utf-8"),
            ("timezone", "Asia/Tokyo"),
            ("options", r"-c search_path=app\ two --DateStyle=ISO,DMY"),
        ]
    )
    startup = startup_settings(wire.startup_params(packet))
    assert startup == [
        "SET client_encoding TO 'utf-8'",
        "SET timezone TO 'Asia/Tokyo'",
        "SET search_path TO 'app two'",
        "SET DateStyle TO 'ISO,DMY'",
    ]
    c = Conversation(startup)
    simple(c, "SET statement_timeout = 5", done("SET"), ready(b"I"))
    ended = simple(c, "INSERT INTO note VALUES (1)", done("INSERT 0 1"), ready(b"I"))
    assert isinstance(ended, Commit) and ended.settings == [*startup, "SET statement_timeout = 5"]
    # asyncpg's pool resets a connection it takes back in one query
    reset = "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;"
    simple(c, reset, row(b""), done("SELECT 1"), done("CLOSE CURSOR ALL"), done("UNLISTEN"), done("RESET"), ready(b"I"))
    ended = simple(c, "INSERT INTO note VALUES (2)", done("INSERT 0 1"), ready(b"I"))
    assert isinstance(ended, Commit) and ended.settings == startup


@pytest.mark.parametrize(
    ("sql", "found"),
    [
        ("INSERT INTO note(at) VALUES (now())", ["now()"]),
        (
            "UPDATE job SET seen = CURRENT_TIMESTAMP, token = gen_random_uuid()",
            ["current_timestamp", "gen_random_uuid()"],
        ),
        ("INSERT INTO note(id, body) VALUES (nextval('note_id_seq'), $1)", ["nextval()"]),
        (
            "CREATE TABLE note(id serial, n bigint DEFAULT nextval('n_seq'), at timestamptz DEFAULT now())",
            ["a column default now()"],
        ),
        ("INSERT INTO note(body) VALUES ('now() and random()') -- now()", []),
        ("INSERT INTO note(at, body) VALUES ($1, $2)", []),
        ('SELECT "random"(1) FROM x', []),
    ],
)
def test_what_the_database_makes_up_is_named_from_the_statement(sql: str, found: list[str]) -> None:
    assert made_up(sql) == found


def test_a_committed_write_that_calls_now_is_a_finding_naming_the_statement_once(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "world.db", "r1", RunClock(datetime(2026, 8, 24, 9, tzinfo=UTC)))
    recorder = Recorder()
    recorder.mount(store)
    stamped = Executed(protocol=StatementProtocol.SIMPLE, sql="UPDATE job SET seen = now()", tags=["UPDATE 1"])
    plain = Executed(protocol=StatementProtocol.SIMPLE, sql="UPDATE job SET n = 1", tags=["UPDATE 1"])
    for statements in ([plain], [stamped], [plain, stamped]):
        recorder.heard(Committed(database="app", connection=2, statements=statements))
    [finding] = nondeterministic(store)
    assert finding.check == DATABASE_CHECK and finding.kind is FindingKind.REVIEW
    assert "`UPDATE job SET seen = now()` (2 times in the run, first at seq 2, on its connection 2)" in finding.message
    assert "calls now()" in finding.message and finding.evidence == [2, 3]
    store.close()
