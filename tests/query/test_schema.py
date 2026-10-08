"""The read model's views are a contract: their names, columns and types are held against a golden copy, which only
changes with the version; and `minutehand query --schema`, the `minutehand_schema` view and docs/querying.md all say
the same."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from minutehand.adapters.query.reader import open_model, query
from minutehand.adapters.query.schema import VERSION, VIEWS, described
from minutehand.cli import main
from tests.query.fixture import Fixture

GOLDEN = Path(__file__).with_name("schema_golden.json")
DOCS = Path(__file__).parents[2] / "docs" / "querying.md"


def test_the_views_are_the_golden_ones_or_the_version_moved() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    now = {v.name: [f"{c.name} {c.type.value}" for c in v.columns] for v in VIEWS}
    if golden["version"] == VERSION:
        removed = {
            name: [c for c in columns if name not in now or c not in now[name]]
            for name, columns in golden["views"].items()
        }
        assert not any(removed.values()), (
            f"views or columns of read model version {VERSION} changed or went: {removed}. A column removed, "
            "renamed or retyped is a new version: raise schema.VERSION and write the new golden"
        )
        for name, columns in golden["views"].items():
            assert now[name][: len(columns)] == columns, f"{name}'s columns moved within version {VERSION}"
        assert now == golden["views"], (
            "a view or a column was added: that keeps the version, but write it into the golden so the next change "
            "is held against it"
        )
    else:
        assert golden["version"] < VERSION, "the version only goes up"
        pytest.fail(f"version {VERSION} is new: write its views into {GOLDEN.name}")


def test_every_view_names_its_columns_once() -> None:
    assert len({v.name for v in VIEWS}) == len(VIEWS)
    for view in VIEWS:
        names = [c.name for c in view.columns]
        assert len(set(names)) == len(names), view.name


def _documented() -> dict[str, list[tuple[str, str, str]]]:
    text = DOCS.read_text(encoding="utf-8")
    views: dict[str, list[tuple[str, str, str]]] = {}
    for section in re.split(r"^### ", text, flags=re.M)[1:]:
        heading = section.splitlines()[0]
        named = re.fullmatch(r"`([a-z_]+)`", heading.strip())
        if named is None:
            continue
        rows = re.findall(r"^\| `([a-z_]+)` \| (TEXT|INTEGER|REAL) \| (.*) \|$", section, flags=re.M)
        views[named.group(1)] = [(n, t, d) for n, t, d in rows]
    return views


def test_the_docs_list_every_view_and_column_as_the_schema_does() -> None:
    assert _documented() == {v.name: [(c.name, c.type.value, c.description) for c in v.columns] for v in VIEWS}


def test_query_schema_prints_every_view_and_column(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["query", "--schema"]) == 0
    printed = capsys.readouterr().out
    assert printed == described()
    for name, columns in _documented().items():
        assert f"\n{name}: " in printed
        for column, kind, description in columns:
            assert re.search(rf"^  {column} +{kind} +{re.escape(description)}$", printed, flags=re.M), (name, column)


def test_the_schema_view_describes_the_views(run: Fixture) -> None:
    db = open_model(run.state, run.run_id)
    try:
        listed = query(db, "SELECT version, view_name, position, column_name, type, description FROM minutehand_schema")
        tables = query(db, "SELECT name FROM sqlite_master WHERE type = 'table'")
    finally:
        db.close()
    assert {r[0] for r in listed.rows} == {VERSION}
    assert sorted(str((r[1], r[2], r[3], r[4], r[5])) for r in listed.rows) == sorted(
        str((v.name, i, c.name, c.type.value, c.description)) for v in VIEWS for i, c in enumerate(v.columns, start=1)
    )
    assert sorted(str(r[0]) for r in tables.rows) == sorted(v.name for v in VIEWS)
