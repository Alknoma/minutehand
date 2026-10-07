"""The relay's reading of PostgreSQL's protocol, message by message, with no database: which statements it keeps,
which transactions it calls committed, and what it refuses to pretend it can replay."""

from __future__ import annotations

import hashlib
import struct

import pytest
from pydantic import ValidationError

from minutehand.adapters.database.postgres import wire
from minutehand.adapters.database.postgres.conversation import Abandon, Commit, Conversation, Ended
from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.database import Database, StatementProtocol


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
