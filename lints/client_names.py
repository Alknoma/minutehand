"""Nothing in this repo names the client that adopts Minutehand, outside the one page about adopting it.

The defect: an export's patience in `application/agent_state.py` was tuned on one client's agent and its docstring
said so by name, and a reference scenario carried the client's own goal and company name into `docs/design.md`
and five test files. Minutehand is for any team's agent; a fake, a check or a constant shaped by one agent and
labelled with it reads as that agent's harness, which is what Minutehand replaced.

The names are held as SHA-256 hashes of their lower-cased spelling, so this file does not name them either. Every
word of every text file in the repository is read (letters and digits, starting with a letter), lower-cased, and
hashed when its length is one of the names'. `python -m lints` hands every lint `src/`; this one reads the whole
repository that holds it, since docs and tests carry names as easily as code. Given any other root (a test's
planted tree), it reads that tree.

Fail-closed, with no inline marker: Markdown, JSON and YAML have no comment a marker could sit in. The only place a
name may stand is `ALLOWED`.

Run: python -m lints.client_names [root]
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from lints._core import SRC, Finding, cli

TITLE = "Client names"
GUIDANCE = "Say what the thing is for any agent, or move what is about the client to docs/adopting-in-alknoma-cloud.md."

NAMES = frozenset(
    {
        "8326ec49f0f2e058510957e1827604842b82bdfc53e7c4f4f63ff53926f61f1e",
        "1ee3d596a2538c31c3ed88f6afb344eb27de90c31cf721a980a29c72d051fb1f",
    }
)
"""SHA-256 of each lower-cased name: the client's company, and the one-letter misspelling its agent wrote."""

NAME_LENGTHS = frozenset({5})
"""The names' lengths: a word is hashed only at one of them, so a scan of the repository takes a second, not ten."""

ALLOWED = frozenset({"CLAUDE.md", "docs/adopting-in-alknoma-cloud.md"})
"""Paths, relative to the repository, where a name may stand: the rule that forbids them, and the adoption page."""

SKIPPED_DIRS = frozenset({".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".ruff_cache", ".claude"})
"""Directories that are no part of the repository's text: tools' caches, environments, another checkout's state."""

WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def hashed(word: str) -> str:
    return hashlib.sha256(word.lower().encode()).hexdigest()


def _files(repo: Path) -> Iterator[Path]:
    """Every file under `repo`, never entering a skipped directory: an environment alone holds tens of thousands."""
    for directory, subdirectories, files in os.walk(repo):
        subdirectories[:] = sorted(d for d in subdirectories if d not in SKIPPED_DIRS and d != ".worktrees")
        for name in sorted(files):
            yield Path(directory) / name


def run(root: Path, names: frozenset[str] = NAMES) -> list[Finding]:
    """Every line outside `ALLOWED` holding a word whose hash is in `names`."""
    repo = root.resolve().parent if root.resolve() == SRC else root.resolve()
    findings: list[Finding] = []
    for path in _files(repo):
        rel = path.relative_to(repo).as_posix()
        if rel in ALLOWED:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not text: an image, a compiled file
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in WORD.finditer(line):
                word = match.group()
                if len(word) in NAME_LENGTHS and hashed(word) in names:
                    shown = Path(os.path.relpath(path, root.resolve())).as_posix()
                    findings.append(Finding(shown, lineno, f"names the client (column {match.start() + 1})"))
                    break
    return findings


if __name__ == "__main__":
    sys.exit(cli(TITLE, run, sys.argv[1:], guidance=GUIDANCE))
