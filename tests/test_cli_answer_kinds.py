"""The CLI's boundary: every outcome is one exit code (`ExitCode`). A refusal of what was asked is one line and exit
2; the machine failing is one line naming the resource and the next step, exit 5; Ctrl-C is 130; Minutehand's own
error is one line naming it and where its traceback was written, exit 4, with the traceback on screen only under
--debug."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from minutehand import cli, session
from minutehand.application.refusals import RunRefused
from minutehand.domain.errors import EnvironmentFailure
from minutehand.domain.run import ExitCode


@dataclass(frozen=True)
class Case:
    name: str
    raised: BaseException
    code: ExitCode
    says: str


CASES = [
    Case("usage", RunRefused("no finished run r1"), ExitCode.USAGE, "minutehand: the run could not be performed: "),
    Case(
        "environment",
        EnvironmentFailure("port 8080 on 127.0.0.1", "the proxy could not listen on it", "free the port"),
        ExitCode.ENVIRONMENT,
        "minutehand: port 8080 on 127.0.0.1: the proxy could not listen on it. free the port",
    ),
    Case("interrupted", KeyboardInterrupt(), ExitCode.INTERRUPTED, "minutehand: interrupted"),
    Case("internal", RuntimeError("kaboom"), ExitCode.TOOL_ERROR, "minutehand: internal error: RuntimeError: kaboom"),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
@pytest.mark.parametrize("debug", [False, True], ids=["plain", "debug"])
def test_each_kind_exits_with_its_code_in_one_line(
    case: Case, debug: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raising(_: list[str]) -> int:
        """Stands in for the command's work, which raised `case.raised`."""
        raise case.raised

    if case.name == "usage":  # the refusal is caught where every command's refusals are: inside the commands
        args = ["findings", "r1", "--state", str(tmp_path)]
    else:
        monkeypatch.setattr(cli, "_main", raising)
        args = ["findings", "r1", "--state", str(tmp_path)]
    assert cli.main([*args, "--debug"] if debug else args) == case.code
    err = capsys.readouterr().err
    lines = err.strip().splitlines()
    assert lines[0].startswith(case.says), err
    traced = "Traceback (most recent call last)" in err
    assert traced is (debug and case.code is ExitCode.TOOL_ERROR)
    if not traced:
        assert len(lines) == 1, err
    if case.code is ExitCode.TOOL_ERROR:
        written = Path(lines[0].rsplit("the traceback is in ", 1)[1].rstrip(")"))
        assert written.parent == tmp_path / cli.ERRORS
        assert "raise case.raised" in written.read_text(encoding="utf-8")


def test_a_run_record_minutehand_cannot_read_is_an_internal_error_not_a_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = session.run_dir(tmp_path, "r1")
    broken.mkdir(parents=True)
    (broken / session.RECORD).write_text("{not json", encoding="utf-8")
    assert cli.main(["findings", "r1", "--state", str(tmp_path)]) == ExitCode.TOOL_ERROR
    [line] = capsys.readouterr().err.strip().splitlines()
    assert line.startswith("minutehand: internal error: ValidationError: ")
    assert f"the traceback is in {tmp_path / cli.ERRORS}" in line
