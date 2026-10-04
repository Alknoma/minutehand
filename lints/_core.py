"""What every lint here shares, so that no lint carries its own copy.

Ported from alknoma-cloud's `research-services/lints/_core.py`, where fifteen
lints had grown fifteen ways of walking the tree, printing, and — the one that
mattered — parsing the escape hatch. Seven documented that the reason is
required; one enforced it. Here the decisions are made once:

  ROOT / SRC                       where the repo is, what a lint scans by default
  sources()                        one walk, one parse, one set of skips
  exempt()                         one escape-hatch grammar, reason required
  Finding / Severity / Kind        a finding is a record, not a sentence
  report() / cli()                 one output shape, one exit-code protocol

Every lint takes its root as a parameter, so a test can point it at a tree it
planted: the only way to show a lint fails when the violation is present.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
"""The repository."""

SRC = ROOT / "src"
"""Every lint's default scan root: the directory holding the `minutehand` package.
A finding's `file` is relative to it, so it reads as a module path."""

PACKAGE = "minutehand"

SKIPPED_PARTS = frozenset({"__pycache__", "tests", ".venv"})
"""Path components, relative to the scan root, that put a file outside a scan."""

REASON_LEADERS = " \t:—–-"  # noqa: RUF001 - the dashes a reason may follow, on purpose
"""Punctuation a reason may be introduced with after the marker."""


class Severity(StrEnum):
    """How loud a finding is."""

    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"


class Kind(StrEnum):
    """What the reader has to DO about a finding, orthogonal to severity."""

    FAIL = "fail"
    REVIEW = "review"
    INFORMATIONAL = "informational"


@dataclass(frozen=True)
class Finding:
    """One thing a lint found. `line` is None, never 0, when the finding is about a file."""

    file: str
    line: int | None
    message: str
    severity: Severity = Severity.ERROR
    kind: Kind = Kind.FAIL

    def render(self, base: str = "") -> str:
        path = f"{base}/{self.file}" if base else self.file
        where = f"{path}:{self.line}" if self.line is not None else path
        return f"{where}: {self.message}"


@dataclass(frozen=True)
class Source:
    """One parsed Python file, with everything a rule needs to read it."""

    path: Path
    rel: str
    lines: list[str]
    tree: ast.Module

    @property
    def module(self) -> str:
        """The dotted module this file is, relative to the scan root."""
        parts = list(Path(self.rel).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    @property
    def is_package(self) -> bool:
        return Path(self.rel).name == "__init__.py"


def sources(root: Path, *, containing: str | Sequence[str] = ()) -> Iterator[Source]:
    """Every parseable .py file under root, sorted, tests and caches skipped.

    `containing` is a cheap gate before the parse: a file whose text has none of
    the substrings cannot hold the construct the lint is after. A file that does
    not parse is not a lint's finding (pyright and the interpreter report that),
    so it is skipped.
    """
    needles = (containing,) if isinstance(containing, str) else tuple(containing)
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if not SKIPPED_PARTS.isdisjoint(rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needles and not any(needle in text for needle in needles):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        yield Source(path=path, rel=rel.as_posix(), lines=text.splitlines(), tree=tree)


def marker(name: str) -> str:
    """The escape hatch for lint `name`: `# <name>-lint: exempt <reason>`."""
    return f"# {name}-lint: exempt"


def exempt(lines: Sequence[str], name: str, lineno: int, *, end_lineno: int | None = None) -> bool:
    """True when `# <name>-lint: exempt <reason>` WITH A REASON sits on a line of lineno..end_lineno.

    A bare marker exempts nothing: an exemption nobody can evaluate is the failure
    every lint here exists to stop.
    """
    hatch = marker(name)
    last = min(end_lineno or lineno, len(lines))
    for line in lines[max(1, lineno) - 1 : last]:
        at = line.find(hatch)
        if at != -1 and line[at + len(hatch) :].strip(REASON_LEADERS).strip():
            return True
    return False


def report(title: str, findings: Sequence[Finding], *, base: str = "", guidance: str = "") -> int:
    """Print one lint's outcome and answer with its exit code: 1 when anything was found."""
    if not findings:
        print(f"{title}: clean")
        return 0
    print(f"{title}: {len(findings)} finding(s)\n")
    for finding in findings:
        print(f"  {finding.render(base)}")
    if guidance:
        print(f"\n{guidance}")
    return 1


def cli(title: str, run: Callable[[Path], list[Finding]], argv: Sequence[str], *, guidance: str = "") -> int:
    """`python -m lints.<name> [root]`: one lint over SRC, or over the root it was given."""
    root = Path(argv[0]).resolve() if argv else SRC
    return report(title, run(root), base=base_for(root), guidance=guidance)


def base_for(root: Path) -> str:
    """How a finding's path is prefixed so it resolves from the repo: `src` for the default root."""
    try:
        return root.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return root.as_posix()
