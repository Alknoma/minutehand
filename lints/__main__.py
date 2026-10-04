"""Run every lint in this directory. There is no registration: a lint is a module here with `run(root)`."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent / "src"


def main() -> int:
    failed = 0
    for path in sorted(HERE.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = importlib.import_module(f"lints.{path.stem}")
        findings = module.run(ROOT)
        for f in findings:
            print(f"{path.stem}: {f.file}:{f.line}: {f.message}")
        print(f"{path.stem}: {len(findings)} finding(s)")
        failed += bool(findings)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
