"""Every query docs/querying.md offers runs as written against a run and finds rows."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from minutehand.adapters.query.reader import open_model, query
from tests.query.fixture import PRICES, Fixture

DOCS = Path(__file__).parents[2] / "docs" / "querying.md"
QUERIES = re.findall(r"^### ([^\n]+)\n\n```sql\n(.*?)```", DOCS.read_text(encoding="utf-8"), flags=re.M | re.S)


def test_the_docs_offer_at_least_twelve_queries() -> None:
    assert len(QUERIES) >= 12


@pytest.mark.parametrize(("title", "sql"), QUERIES, ids=[t for t, _ in QUERIES])
def test_a_documented_query_finds_rows(run: Fixture, title: str, sql: str) -> None:
    db = open_model(run.state, run.run_id, PRICES)
    try:
        found = query(db, sql)
    finally:
        db.close()
    assert found.rows, f"{title!r} found nothing"
