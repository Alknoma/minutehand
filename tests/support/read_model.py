"""A run's facts as `minutehand query` reads them: rows of its read model (`adapters/query`), which the viewer reads
too."""

from __future__ import annotations

import json
from pathlib import Path

from minutehand.adapters.query.reader import open_model


def rows(state: Path, run_id: str, sql: str, *args: object) -> list[dict[str, object]]:
    db = open_model(state, run_id)
    try:
        cursor = db.execute(sql, args)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    finally:
        db.close()


def listed(value: object) -> list[object]:
    """A JSON array column, read."""
    assert isinstance(value, str)
    found = json.loads(value)
    assert isinstance(found, list)
    return found
