"""Checks an agent's own repository keeps beside its agent file (`AgentUnderTest.checks`), run with Minutehand's."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from minutehand.application.files import load_agent
from minutehand.checks.runner import ChecksRefused, evaluate, load_checks
from minutehand.cli import main
from minutehand.domain.checks import FindingKind
from tests.checks.world import Log, person, scenario, view

OWN = """
from minutehand.domain.checks import CheckReport, Finding, FindingKind, Needs, Severity


class TwoMessagesAtMost:
    id = "two_messages_at_most"
    needs = frozenset({Needs.WORLD})

    def run(self, view):
        sent = [e for e in view.events if e.actor.value == "agent"]
        if len(sent) <= 2:
            return CheckReport()
        return CheckReport(findings=[Finding(check=self.id, severity=Severity.WARNING, kind=FindingKind.FAIL,
                                             message=f"the agent wrote {len(sent)} times; this team allows two")])
"""


AGENT = """name: team_agent
wakes: [{kind: reported, wake_url: "http://127.0.0.1:9/wake", report_url: "http://127.0.0.1:9/report"}]
checks: [team_checks.py]
"""


def write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    return path


def test_an_agents_own_check_runs_with_minutehands_and_its_finding_counts(tmp_path: Path) -> None:
    [check] = load_checks([str(write(tmp_path, "team_checks.py", OWN))])
    log = Log()
    for hour in (0, 1, 2):
        log.message([person("sofia")], hour)
    built = view(scenario(person("owner"), person("sofia")), log, [])

    result = evaluate(built, stop=None, own=[check])

    [finding] = [f for f in result.findings if f.check == "two_messages_at_most"]
    assert finding.kind is FindingKind.FAIL and finding.message == "the agent wrote 3 times; this team allows two"
    assert result.effectiveness.failed_checks >= 1
    assert "two_messages_at_most" not in {f.check for f in evaluate(built, stop=None).findings}


@pytest.mark.parametrize(
    ("source", "says"),
    [
        (None, "no such check file"),
        ("def broken(:\n", "could not be loaded: SyntaxError"),
        ("x = 1\n", "defines no check"),
    ],
)
def test_a_check_file_that_cannot_be_used_is_refused(tmp_path: Path, source: str | None, says: str) -> None:
    path = tmp_path / "team_checks.py"
    if source is not None:
        path.write_text(source)
    with pytest.raises(ChecksRefused, match=says):
        load_checks([str(path)])


def test_an_agents_check_that_takes_an_id_of_minutehands_is_refused(tmp_path: Path) -> None:
    clash = OWN.replace('id = "two_messages_at_most"', 'id = "expectations"')
    [check] = load_checks([str(write(tmp_path, "team_checks.py", clash))])
    with pytest.raises(ValueError, match="two checks share an id: expectations"):
        evaluate(view(scenario(person("owner")), Log(), []), stop=None, own=[check])


def test_check_paths_are_read_from_the_agent_files_folder_and_kept_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "agent"
    folder.mkdir()
    write(folder, "team_checks.py", OWN)
    (folder / "agent.yaml").write_text(AGENT)
    monkeypatch.chdir(tmp_path)

    agent = load_agent(folder / "agent.yaml")

    assert agent.checks == [str((folder / "team_checks.py").resolve())]


def test_validate_names_an_agent_check_file_that_cannot_load_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path, "team_checks.py", "x = 1\n")
    (tmp_path / "agent.yaml").write_text(AGENT)

    assert main(["validate", str(tmp_path / "agent.yaml")]) == 1
    said = capsys.readouterr().err
    assert ": checks: " in said and "team_checks.py: defines no check" in said
