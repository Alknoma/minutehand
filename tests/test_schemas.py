"""The published schemas are what the models generate, and `minutehand validate` names each problem with its place.

`schemas/` is committed so an editor can read a file's schema from the repository; a model changed without
regenerating them (`minutehand schema <kind> > schemas/...`) fails here, naming the file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minutehand import agent_api
from minutehand.application.files import FileKind, schema
from minutehand.cli import main

SCHEMAS = Path(__file__).parent.parent / "schemas"
EXAMPLES = Path(__file__).parent.parent / "examples" / "reference_agent"


@pytest.mark.parametrize("kind", list(FileKind))
def test_the_committed_schema_of_each_file_is_the_one_its_model_generates(kind: FileKind) -> None:
    committed = json.loads((SCHEMAS / f"{kind.value}.schema.json").read_text())
    assert committed == schema(kind), f"schemas/{kind.value}.schema.json drifted: run minutehand schema {kind.value}"


def test_the_committed_agent_api_is_the_one_its_models_generate() -> None:
    committed = json.loads((SCHEMAS / "agent-api.openapi.json").read_text())
    assert committed == json.loads(json.dumps(agent_api.document())), "run minutehand schema agent-api"
    operations = {op["operationId"] for item in committed["paths"].values() for op in item.values()}
    assert operations == {"wake", "report", "deliverReply", "listPending", "decide"}


def test_the_schema_command_prints_the_committed_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["schema", "agent"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads((SCHEMAS / "agent.schema.json").read_text())


def test_every_reference_file_validates(capsys: pytest.CaptureFixture[str]) -> None:
    files = sorted(str(p) for p in EXAMPLES.glob("*.yaml"))
    assert main(["validate", *files]) == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert f"{EXAMPLES / 'agent.yaml'}: a valid agent file" in out
    assert f"{EXAMPLES / 'scenario.yaml'}: a valid scenario file" in out


def test_validate_rejects_bad_files_naming_each_problem_with_its_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = "name: a\nwakes: [{kind: command, argv: [x]}]\ninboxes:\n  - name: approvals\n"
    decisions = "    decisions: [{name: approve, request: {kind: template, method: POST, url: 'http://h/{item.id}'}}]\n"
    path = tmp_path / "path.yaml"
    path.write_text(
        head
        + "    pending: {request: {kind: template, url: 'http://h/'}, items: '$.items[*]', id: id, summary: $.s}\n"
        + decisions
    )
    named = tmp_path / "named.yaml"
    named.write_text(
        head
        + "    pending: {request: {kind: template, url: 'http://h/{person.mail}'}, items: '$.items[*]', id: $.id,\n"
        + "              summary: $.s}\n"
        + decisions
    )
    seed = tmp_path / "seed.yaml"
    seed.write_text("people: [{key: Nadia, name: Nadia, email: n@example.com}]\n")

    assert main(["validate", str(path), str(named), str(seed)]) == 1
    err = capsys.readouterr().err.splitlines()
    assert f"{path}: inboxes[0].pending.id: Value error, 'id': a JSONPath query starts at the root, $" in err
    assert any(
        line.startswith(f"{named}: inboxes[0].pending: Value error, the list's request's url names {{person.mail}}")
        for line in err
    ), err
    assert f"{seed}: people[0].key: String should match pattern '^[a-z][a-z0-9_]*$'" in err
