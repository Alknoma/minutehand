"""The command line's one converter (`cli.main`): a refusal it knows keeps its message and exit code; anything else
is Minutehand's own error, one line naming it and where its traceback was written, exit 4, and the traceback on
screen only with --debug. Before, such an error ended the command in a raw Python traceback, exit 1, which reads as
"a check failed"."""

from __future__ import annotations

from pathlib import Path

import pytest

from minutehand import cli, session

STATE = "<state>"


def _broken_run(state: Path) -> None:
    """A run whose record cannot be read back: the error is Minutehand's, not a refusal the command knows."""
    directory = session.run_dir(state, "r1")
    directory.mkdir(parents=True)
    (directory / session.RECORD).write_text("{this is not a record", encoding="utf-8")


@pytest.mark.parametrize(
    ("argv", "code", "said"),
    [
        (["findings", "nope", STATE], 2, "minutehand: the run could not be performed: no finished run nope under "),
        (["run", "s.yaml", "--agent", "a.yaml", STATE, "--"], 2, "minutehand: nothing follows --;"),
        (["--debug", "findings", "nope", STATE], 2, "minutehand: the run could not be performed: no finished run "),
        (["findings", "r1", STATE], 4, "minutehand: internal error (a bug in minutehand, not in the agent or the "),
        (["findings", "r1", STATE, "--debug"], 4, "minutehand: internal error (a bug in minutehand, not in the "),
    ],
)  # fmt: skip
def test_each_kind_of_failure_is_answered_by_the_command_with_its_own_line_and_exit_code(
    argv: list[str], code: int, said: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    _broken_run(state)

    exited = cli.main([a for given in argv for a in (["--state", str(state)] if given == STATE else [given])])

    printed = capsys.readouterr().err
    assert exited == code
    last = printed.rstrip("\n").splitlines()[-1]
    assert last.startswith(said)
    if code != 4:
        assert "Traceback" not in printed
        return
    assert "ValidationError" in last and "the traceback is in " in last
    written = Path(last.rsplit("the traceback is in ", 1)[1])
    assert written.is_file() and "Traceback (most recent call last)" in written.read_text(encoding="utf-8")
    written.unlink()
    assert ("Traceback (most recent call last)" in printed) is ("--debug" in argv)
    if "--debug" not in argv:
        assert printed.count("\n") == 1, "one line, and nothing else"
