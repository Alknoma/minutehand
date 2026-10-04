"""A fake never reads the machine's clock; every timestamp comes from the run's clock.

The defect: the fakes stamped everything with the real date while the mission
clock ran eight days ahead, so a ticket filed on the mission's 30 August showed
as created on the 24th (run f431fc97f427). Instances: the Slack fake's message
`ts`, and the Asana and Drive fakes' created/modified times.

Matched by call: `time.time`, `time.time_ns`, `datetime.now/utcnow/today`,
`date.today`, however the owner is reached (`datetime.now`, `dt.datetime.now`).
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


def _owner(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def run(root: Path = SRC) -> list[Finding]:
    findings: list[Finding] = []
    for source in sources(root):
        for node in ast.walk(source.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner = _owner(node.func.value)
            if (owner, node.func.attr) not in BANNED:
                continue
            if exempt(source.lines, NAME, node.lineno):
                continue
            findings.append(
                Finding(
                    source.rel,
                    node.lineno,
                    f"{owner}.{node.func.attr}() reads the machine clock; take the time from the run's clock",
                )
            )
    return findings


if __name__ == "__main__":
    sys.exit(cli(TITLE, run, sys.argv[1:], guidance=GUIDANCE))
