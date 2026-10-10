"""docs/approvals.md quotes its examples, never paraphrases them: every YAML and SQL block names the file under
tests/agents/approvals it comes from, and is that file whole or a run of its lines."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "approvals.md"

BLOCK = re.compile(r"<!-- (file|excerpt): (?P<path>[^ ]+) -->\n```(?P<lang>yaml|sql)\n(?P<body>.*?)```\n", re.DOTALL)


def guide_blocks() -> list[tuple[str, Path, str]]:
    return [(m.group(1), ROOT / m.group("path"), m.group("body")) for m in BLOCK.finditer(GUIDE.read_text())]


def test_every_yaml_and_sql_block_of_the_guide_is_one_of_the_example_files() -> None:
    text = GUIDE.read_text()
    blocks = guide_blocks()
    fenced = len(re.findall(r"^```(yaml|sql)$", text, flags=re.MULTILINE))
    assert fenced == len(blocks), "every yaml and sql block names the file it comes from"
    assert len(blocks) >= 15
    for kind, path, body in blocks:
        whole = path.read_text()
        if kind == "file":
            assert body == whole, f"{path} differs from its block in the guide"
        else:
            assert body in whole, f"a block of the guide is not a run of lines of {path}"
            assert body.endswith("\n") and (whole.startswith(body) or f"\n{body}" in whole), path
