"""The committed schemas are the generated ones: a model changed without `minutehand schema` being run again fails
here, so an editor reading `schemas/` and a team generating types from them read what the code validates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from minutehand import agent_api, session
from minutehand.application.files import FileKind, record_schema, schema
from minutehand.cli import main
from minutehand.domain.checks import Finding, FindingKind, Severity
from tests.export.run import T0, finish, write_run

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


@pytest.mark.parametrize("kind", list(FileKind))
def test_the_committed_file_schema_is_the_generated_one(kind: FileKind) -> None:
    committed = json.loads((SCHEMAS / f"{kind.value}.schema.json").read_text(encoding="utf-8"))
    assert committed == schema(kind), f"run `minutehand schema {kind.value} > schemas/{kind.value}.schema.json`"


def test_the_committed_agent_api_is_the_generated_one() -> None:
    committed = json.loads((SCHEMAS / "agent-api.openapi.json").read_text(encoding="utf-8"))
    assert committed == agent_api.document(), "run `minutehand schema agent-api > schemas/agent-api.openapi.json`"


def test_the_committed_record_schema_is_the_generated_one() -> None:
    committed = json.loads((SCHEMAS / "record.schema.json").read_text(encoding="utf-8"))
    assert committed == record_schema(), "run `minutehand schema record > schemas/record.schema.json`"


def _exported(tmp_path: Path, *, finished: bool) -> object:
    """What `minutehand export` writes for a run still being written, or for one that finished with a finding citing
    every event of the run."""
    run = write_run(tmp_path)
    if finished:
        with session.reading(run.state, run.run_id) as world:
            evidence = [e.seq for e in world.events()]
        cited = Finding(
            check="asks_before_it_acts",
            severity=Severity.ERROR,
            kind=FindingKind.FAIL,
            message="asks_before_it_acts did not hold",
            at=T0,
            wake=1,
            evidence=evidence,
        )
        finish(run, [cited])
    out = tmp_path / "record.json"
    assert main(["export", run.run_id, "--out", str(out), "--state", str(run.state)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize("finished", [False, True], ids=["unfinished", "finished"])
def test_an_exported_run_is_valid_against_the_committed_record_schema(tmp_path: Path, finished: bool) -> None:
    written = _exported(tmp_path, finished=finished)
    assert isinstance(written, dict) and ("assessment" in written) is finished
    validator = Draft202012Validator(json.loads((SCHEMAS / "record.schema.json").read_text(encoding="utf-8")))
    assert [e.message for e in validator.iter_errors(written)] == []


def test_the_record_schema_rejects_another_version_an_unknown_field_and_a_missing_version(tmp_path: Path) -> None:
    written = _exported(tmp_path, finished=False)
    assert isinstance(written, dict)
    validator = Draft202012Validator(record_schema())
    assert not validator.is_valid({**written, "schema": "minutehand-replay-record/1"})
    assert not validator.is_valid({**written, "story": "told by hand"})
    assert not validator.is_valid({k: v for k, v in written.items() if k != "schema"})


def test_the_agent_api_holds_no_endpoint_that_exists_for_minutehand() -> None:
    """Nothing is asked of the agent: the document covers only places the agent's own product already has."""
    operations = {op["operationId"] for path in agent_api.document()["paths"].values() for op in path.values()}  # type: ignore[union-attr]
    assert operations == {"deliverReply", "listPending", "decide"}


def test_the_agent_file_schema_takes_a_sandbox_and_no_wake_kinds() -> None:
    agent = schema(FileKind.AGENT)
    assert "sandbox" in agent["required"]  # type: ignore[operator]
    assert "wakes" not in agent["properties"]  # type: ignore[operator]
    assert not {"Reported", "Marked", "Polled", "Command", "Contained", "GoalByWake"} & set(agent["$defs"])  # type: ignore[arg-type]
