"""Run every lint in this directory and fail if any one of them fails.

There is no registration: a lint is a module here, not underscore-prefixed, that
defines `TITLE` and `run(root) -> list[Finding]`. In the parent repo a lint sat in
the directory for three weeks gating nothing because the workflow named each lint
by hand. Every lint runs even after one fails, so a change sees the whole list.

A module here that cannot be imported, lacks `run`, or raises did not RUN. That is
reported apart from findings and fails the run: a lint that never looked must not
read as a clean one.

    python -m lints                # every lint over src/
    python -m lints.<name> [root]  # one lint
"""

from __future__ import annotations

import importlib
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from lints._core import SRC, Finding, base_for, report

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class LintRun:
    name: str
    findings: list[Finding] = field(default_factory=list)
    blocked: str = ""

    @property
    def ok(self) -> bool:
        return not (self.findings or self.blocked)


def discover(package: Path = HERE) -> list[str]:
    """Every lint module in the package, sorted; underscore-prefixed files are not lints."""
    return sorted(p.stem for p in package.glob("*.py") if not p.name.startswith("_"))


def run_one(name: str, root: Path = SRC) -> LintRun:
    try:
        module = importlib.import_module(f"lints.{name}")
        title: str = module.TITLE
        guidance: str = getattr(module, "GUIDANCE", "")
        findings: list[Finding] = module.run(root)
    except Exception:  # noqa: BLE001 — any failure to run is reported as blocked, never swallowed
        reason = traceback.format_exc().strip().splitlines()[-1]
        print(f"{name}: COULD NOT RUN — {reason}")
        return LintRun(name, blocked=reason)
    report(title, findings, base=base_for(root), guidance=guidance)
    return LintRun(name, findings=list(findings))


def main(root: Path = SRC) -> int:
    runs: list[LintRun] = []
    for name in discover():
        print(f"=== {name} ===")
        runs.append(run_one(name, root))
        print()
    failed = [r.name for r in runs if not r.ok]
    if failed:
        print(f"{len(failed)} of {len(runs)} lint(s) FAILED: {', '.join(failed)}")
        return 1
    print(f"All {len(runs)} lint(s) clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
