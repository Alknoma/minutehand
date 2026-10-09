"""A command's positionals are read wherever they are written among its options, on every Python the package runs
on. argparse fills positionals greedily from the words before the first option: before Python 3.12.7 (CI's 3.12.3,
Ubuntu 24.04's own) `query RUN --state X SQL` left `sql` empty and refused SQL as unrecognized, and on every version
`rm A --state X B` refuses B."""

from __future__ import annotations

import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from minutehand.cli import _parser

SRC = Path(__file__).resolve().parents[1] / "src"
ORDERS = [
    ["query", "r1", "SELECT 1", "--state", "s"],
    ["query", "r1", "--state", "s", "SELECT 1"],
    ["query", "--state", "s", "r1", "SELECT 1"],
    ["query", "--format", "json", "r1", "--state", "s", "SELECT 1"],
]


@pytest.mark.parametrize("argv", ORDERS)
def test_query_reads_its_run_and_its_sql_in_any_order(argv: list[str]) -> None:
    args = _parser().parse_anywhere(argv)

    assert (args.run, args.sql, args.state) == ("r1", "SELECT 1", Path("s"))


def test_rm_reads_run_ids_on_both_sides_of_an_option() -> None:
    args = _parser().parse_anywhere(["rm", "a", "--state", "s", "b"])

    assert (args.run_ids, args.state) == (["a", "b"], Path("s"))


def test_a_word_no_command_takes_is_still_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        _parser().parse_anywhere(["query", "r1", "SELECT 1", "--state", "s", "extra"])

    assert exited.value.code == 2
    assert "unrecognized arguments: extra" in capsys.readouterr().err


def _python_3_12_3() -> str | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    found = subprocess.run([uv, "python", "find", "--no-project", "3.12.3"], capture_output=True, text=True)
    return found.stdout.strip() or None if found.returncode == 0 else None


def test_query_reads_every_order_on_python_3_12_3() -> None:
    """Under a 3.12.3 interpreter found on this machine, with this environment's packages, whose compiled modules
    load only under a 3.12."""
    python = _python_3_12_3()
    if python is None or sys.version_info[:2] != (3, 12):
        pytest.skip("needs a Python 3.12.3 here (`uv python install 3.12.3`) and a 3.12 environment to borrow from")
    script = (
        "import sys; sys.path[:0] = sys.argv[1:3]\n"
        "from minutehand.cli import _parser\n"
        f"for argv in {ORDERS!r}:\n"
        "    a = _parser().parse_anywhere(argv); print(sys.version.split()[0], a.run, a.sql, a.state)\n"
    )
    ran = subprocess.run(
        [python, "-c", script, str(SRC), sysconfig.get_paths()["purelib"]], capture_output=True, text=True, timeout=60
    )

    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.splitlines() == ["3.12.3 r1 SELECT 1 s"] * len(ORDERS), (sys.version, ran.stdout)
