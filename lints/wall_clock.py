"""A fake never reads the machine's clock; every timestamp comes from the run's clock.

The defect: the fakes stamped everything with the real date while the mission
clock ran eight days ahead, so a ticket filed on the mission's 30 August showed
as created on the 24th (run f431fc97f427). Instances: the Slack fake's message
`ts`, and the Asana and Drive fakes' created/modified times.

Matched by call: `time.time`, `time.time_ns`, `datetime.now/utcnow/today`,
`date.today`, however the owner is reached (`datetime.now`, `dt.datetime.now`),
under any name an import gives it (`import time as t; t.time()`,
`from datetime import datetime as dt; dt.now()`), and a clock function imported
by name and called bare (`from time import time; time()`).
`time.monotonic` and `time.perf_counter` measure durations and are untouched.

Fail-closed. The only way past is `# clock-lint: exempt <reason>` on the line.

Run: python -m lints.wall_clock [root]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from lints._core import SRC, Finding, cli, exempt, sources

NAME = "clock"
TITLE = "Wall clock"
GUIDANCE = "Take the time from ports.clock.Clock; `wall_time` is the one legitimate reader."

BANNED = {
    ("time", "time"),
    ("time", "time_ns"),
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("date", "today"),
}


MODULES = frozenset({"time", "datetime"})
"""The modules the clock reads live in; an import of either may rename it or pull a reader out by name."""


class _Names:
    """What each bare name in one file stands for, from its imports: a module, a class, or a clock function."""

    def __init__(self, tree: ast.Module) -> None:
        self.owners: dict[str, str] = {}
        self.functions: dict[str, tuple[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in MODULES and alias.asname:
                        self.owners[alias.asname] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module in MODULES and node.level == 0:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if (node.module, alias.name) in BANNED:
                        self.functions[bound] = (node.module, alias.name)
                    elif alias.name in {"datetime", "date"}:
                        self.owners[bound] = alias.name

    def owner(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return self.owners.get(node.id, node.id)
        return None

    def call(self, node: ast.Call) -> tuple[str, str] | None:
        """The (owner, function) a call reaches, when it is one of the clock reads."""
        if isinstance(node.func, ast.Name):
            return self.functions.get(node.func.id)
        if isinstance(node.func, ast.Attribute):
            owner = self.owner(node.func.value)
            if owner is not None and (owner, node.func.attr) in BANNED:
                return owner, node.func.attr
        return None


def run(root: Path = SRC) -> list[Finding]:
    findings: list[Finding] = []
    for source in sources(root):
        names = _Names(source.tree)
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.Call):
                continue
            reached = names.call(node)
            if reached is None or exempt(source.lines, NAME, node.lineno):
                continue
            owner, function = reached
            findings.append(
                Finding(
                    source.rel,
                    node.lineno,
                    f"{owner}.{function}() reads the machine clock; take the time from the run's clock",
                )
            )
    return findings


if __name__ == "__main__":
    sys.exit(cli(TITLE, run, sys.argv[1:], guidance=GUIDANCE))
