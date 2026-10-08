"""The read model runs one SELECT at a time and refuses everything else, and the run it is built from is opened
read-only."""

from __future__ import annotations

import hashlib

import pytest

from minutehand.adapters.query.reader import MAX_ROWS, QueryRefused, open_model, query
from minutehand.session import RUNS, WORLD
from tests.query.fixture import Fixture

WRITES = (
    "DELETE FROM messages",
    "UPDATE run SET seed = 1",
    "INSERT INTO evidence VALUES (9, 9)",
    "DROP TABLE calls",
    "CREATE TABLE notes(a)",
    "CREATE TEMP TABLE notes(a)",
    "ALTER TABLE run ADD COLUMN extra",
    "PRAGMA query_only = OFF",
    "ATTACH DATABASE ':memory:' AS other",
    "VACUUM",
)


@pytest.mark.parametrize("statement", WRITES)
def test_a_statement_that_is_not_a_select_is_refused(run: Fixture, statement: str) -> None:
    db = open_model(run.state, run.run_id)
    try:
        with pytest.raises(QueryRefused, match=r"read-only|authoriz"):
            query(db, statement)
        assert query(db, "SELECT COUNT(*) FROM messages").rows == [[5]]
        assert query(db, "SELECT seed FROM run").rows == [[7]]
    finally:
        db.close()


def test_two_statements_at_once_are_refused(run: Fixture) -> None:
    db = open_model(run.state, run.run_id)
    try:
        with pytest.raises(QueryRefused, match="one statement"):
            query(db, "SELECT 1; DELETE FROM messages")
    finally:
        db.close()


def test_a_query_reads_the_world_file_without_changing_a_byte_of_it(run: Fixture) -> None:
    world = run.state / RUNS / run.run_id / WORLD
    before = hashlib.sha256(world.read_bytes()).hexdigest()
    db = open_model(run.state, run.run_id)
    try:
        query(db, "SELECT * FROM calls")
    finally:
        db.close()
    assert hashlib.sha256(world.read_bytes()).hexdigest() == before


def test_a_page_says_whether_more_rows_follow(run: Fixture) -> None:
    db = open_model(run.state, run.run_id)
    try:
        first = query(db, "SELECT seq FROM events ORDER BY seq", limit=10)
        last = query(db, "SELECT seq FROM events ORDER BY seq", limit=10, offset=20)
    finally:
        db.close()
    assert (first.rows[-1], first.more) == ([10], True)
    assert (last.rows, last.more) == ([[21], [22], [23], [24]], False)


def test_a_limit_beyond_the_cap_is_refused(run: Fixture) -> None:
    db = open_model(run.state, run.run_id)
    try:
        with pytest.raises(QueryRefused, match=f"1 to {MAX_ROWS}"):
            query(db, "SELECT 1", limit=MAX_ROWS + 1)
    finally:
        db.close()
