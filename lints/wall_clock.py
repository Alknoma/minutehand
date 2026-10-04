"""A fake never reads the machine's clock; every timestamp comes from the run's clock.

The defect: the fakes stamped everything with the real date while the mission
clock ran eight days ahead, so a ticket filed on the mission's 30 August showed
as created on the 24th (run f431fc97f427). Instances: the Slack fake's message
`ts`, and the Asana and Drive fakes' created/modified times.

Fail-closed. The only way past is `# clock-lint: exempt <reason>` on the line.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

BANNED = {
    ("time", "time"), ("time", "time_ns"),
    ("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"),
    ("date", "today"),
}
MARKER = "# clock-lint: exempt"


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    message: str


def run(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        if {"tests", "__pycache__", ".venv"} & set(path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner = node.func.value
            name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", None)
            if (name, node.func.attr) not in BANNED:
                continue
            line = lines[node.lineno - 1]
            if MARKER in line and line.split(MARKER, 1)[1].strip(" :—-"):
                continue
            findings.append(
                Finding(
                    path.relative_to(root).as_posix(),
                    node.lineno,
                    f"{name}.{node.func.attr}() reads the machine clock; take the time from the run's clock",
                )
            )
    return findings


if __name__ == "__main__":
    found = run(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("src"))
    for f in found:
        print(f"{f.file}:{f.line}: {f.message}")
    print(f"{len(found)} finding(s)")
    sys.exit(1 if found else 0)
