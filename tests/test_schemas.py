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
EXAMPLES = Path(__file__).parent.parent / "tests" / "agents" / "reference_agent"


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
    files = sorted(str(p) for p in EXAMPLES.glob("*.yaml") if p.name != "team_policy.yaml")  # a list of rules alone
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


AGENT = "name: a\nwakes: [{kind: command, argv: [x]}]\n"
SCENARIO = (
    "name: s\ngoal: g\nowner: owen\npeople:\n"
    "  - {key: owen, name: Owen, email: owen@example.com}\n"
    "  - {key: rosa, name: Rosa, email: rosa@example.com}\n"
)
RULE = "  - {id: asks_rosa, count: {messages: {to: [%s]}}, at_least: 1%s}\n"


def _validated(tmp_path: Path, capsys: pytest.CaptureFixture[str], agent: str, scenario: str) -> tuple[int, str]:
    (tmp_path / "agent.yaml").write_text(agent)
    (tmp_path / "scenario.yaml").write_text(scenario)
    code = main(["validate", str(tmp_path / "agent.yaml"), str(tmp_path / "scenario.yaml")])
    return code, capsys.readouterr().err


def test_validate_accepts_an_agent_file_and_a_scenario_whose_rules_read_together(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = AGENT + "assess:\n" + RULE % ("rosa", "")
    assert _validated(tmp_path, capsys, agent, SCENARIO + "assess_off: [asks_rosa]\n") == (0, "")


def test_validate_rejects_a_scenario_switching_off_a_rule_nobody_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = _validated(tmp_path, capsys, AGENT, SCENARIO + "assess_off: [asks_rosa]\n")
    assert code == 1
    assert "scenario.yaml with" in err and "switches off asks_rosa, which no rule of the agent file" in err


def test_validate_rejects_an_agent_rule_naming_someone_the_scenario_does_not_have(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = _validated(tmp_path, capsys, AGENT + "assess:\n" + RULE % ("zed", ""), SCENARIO)
    assert code == 1 and "rule asks_rosa names zed, who is not in the scenario" in err


def test_validate_rejects_a_rule_naming_a_pattern_there_is_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, err = _validated(tmp_path, capsys, AGENT + "assess:\n" + RULE % ("rosa", ", pattern: be_nice"), SCENARIO)
    assert code == 1 and "agent.yaml: assess[0].pattern: no pattern 'be_nice'" in err


def test_validate_rejects_a_rule_whose_moment_its_each_does_not_have(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rule = "  - {id: r, count: {messages: {}, since: answer}, at_least: 1}\n"
    code, err = _validated(tmp_path, capsys, AGENT + "assess:\n" + rule, SCENARIO)
    assert code == 1 and "names answer, which only an ask or a hand-off has" in err


SERVICE_SCENARIO = """\
name: approvals
goal: Order the laptops once approved.
owner: owen
starts_at: "2026-08-24T09:00:00Z"
people:
  - {key: owen, name: Owen Hart, email: owen@example.com, reply: {kind: silent}}
  - {key: nadia, name: Nadia Ek, email: nadia@example.com, reply: %s}
services:
  - {host: api.approvals.example, name: approvals, responders: [nadia], within: {min: PT3H, max: P1D}}
"""


@pytest.mark.parametrize(
    ("nadia", "warned"),
    [
        ("{kind: scripted, then: silent}", "responder nadia's script ends in silence (`then: silent`)"),
        ("{kind: silent}", "responder nadia is declared silent, so nobody ever acts on its items"),
    ],
)
def test_validate_warns_of_a_service_responder_who_never_acts_and_still_accepts_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], nadia: str, warned: str
) -> None:
    (tmp_path / "scenario.yaml").write_text(SERVICE_SCENARIO % nadia, encoding="utf-8")
    assert main(["validate", str(tmp_path / "scenario.yaml")]) == 0
    err = capsys.readouterr().err
    assert "scenario.yaml: warning: services[0] (approvals): " + warned in err


def test_validate_says_nothing_of_a_service_responder_who_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "scenario.yaml").write_text(SERVICE_SCENARIO % "{kind: scripted, then: answers}", encoding="utf-8")
    assert main(["validate", str(tmp_path / "scenario.yaml")]) == 0
    assert "warning" not in capsys.readouterr().err
