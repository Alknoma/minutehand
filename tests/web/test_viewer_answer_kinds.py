"""The viewer API's boundary: a run that is not there is 404 `not_found`; a run Minutehand cannot read is its own
error, 500 `internal_error`, logged at error, never reported as a missing run."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from minutehand import session
from minutehand.adapters.store.sqlite import SqliteStore
from minutehand.adapters.web.app import create_app
from minutehand.adapters.web.responses import Refusal, RefusalCode
from minutehand.application.run_clock import RunClock

VIEWER = "minutehand.adapters.web.app"
START = datetime(2026, 8, 24, 10, 50, tzinfo=UTC)


@dataclass(frozen=True)
class Case:
    name: str
    record: str | None
    """The run's record as written; None: no run at all."""
    status: int
    code: RefusalCode
    starts: str
    level: int | None


CASES = [
    Case("missing", None, 404, RefusalCode.NOT_FOUND, "no run r1", None),
    Case(
        "unreadable", "{not json", 500, RefusalCode.INTERNAL_ERROR, "internal error: ValidationError: ", logging.ERROR
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
async def test_each_kind_is_one_refusal_body_with_its_code(
    case: Case, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    if case.record is not None:
        run = session.run_dir(tmp_path, "r1")
        run.mkdir(parents=True)
        SqliteStore(run / session.WORLD, "r1", RunClock(START)).close()
        (run / session.RECORD).write_text(case.record, encoding="utf-8")
    caplog.set_level(logging.DEBUG, logger=VIEWER)
    transport = httpx.ASGITransport(app=create_app(tmp_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://viewer") as http:
        answered = await http.get("/api/runs/r1/scorecard")
    assert answered.status_code == case.status
    refusal = Refusal.model_validate_json(answered.content)
    assert refusal.code is case.code
    assert refusal.error.startswith(case.starts)
    logged = [r.levelno for r in caplog.records if r.name == VIEWER]
    assert logged == ([] if case.level is None else [case.level])
