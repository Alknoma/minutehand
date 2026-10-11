"""The agent's program as a run started it: its command, the folder it ran in, and a digest of its code, so a fork
whose agent's code changed says so instead of reading as a rerun.

The code is what git holds in the repository the agent ran in (its commit, the changes not yet committed, the files
not yet added and not ignored), and every file the command names that lies outside that repository. Outside a
repository only the files the command names are its code. The state directory is never part of it, nor bytecode a
run leaves behind.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from minutehand.domain.scenario import Model

PROGRAM_RECORD = "program.json"
_SKIPPED = ("__pycache__",)


class AgentProgram(Model):
    command: list[str]
    directory: str = Field(description="The folder the command ran in")
    commit: str | None = Field(description="The git commit the folder was at; None outside a repository")
    uncommitted: bool = Field(description="Changes or new files not yet committed were part of the code")
    code: str | None = Field(description="SHA-256 of the code as described above; None when nothing named any")


def _git(directory: Path, *args: str) -> bytes | None:
    try:
        done = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _skipped(path: Path, state: Path) -> bool:
    return path.suffix == ".pyc" or any(p in _SKIPPED for p in path.parts) or path.resolve().is_relative_to(state)


def program_of(command: Sequence[str], directory: Path, state: Path) -> AgentProgram:
    """The program `command` is, run in `directory`, with `state` (Minutehand's own) left out of its code."""
    state = state.resolve()
    digest = hashlib.sha256()
    named = False
    top = _git(directory, "rev-parse", "--show-toplevel")
    root = Path(top.decode().strip()).resolve() if top else None
    commit: str | None = None
    uncommitted = False
    if root is not None:
        head = _git(root, "rev-parse", "HEAD")
        commit = head.decode().strip() if head else None
        changed = _git(root, "diff", "HEAD", "--binary", "--", ".", f":(exclude){state}") or b""
        added = [
            root / p
            for p in (_git(root, "ls-files", "--others", "--exclude-standard", "-z") or b"").decode().split("\0")
            if p and not _skipped(root / p, state)
        ]
        uncommitted = bool(changed or added)
        digest.update(b"commit\0" + (commit or "").encode() + b"\0diff\0" + changed)
        for path in sorted(added):
            digest.update(b"new\0" + str(path.relative_to(root)).encode() + b"\0" + _read(path))
        named = True
    for word in command:
        path = (directory / word).resolve()
        if path.is_file() and (root is None or not path.is_relative_to(root)) and not _skipped(path, state):
            digest.update(b"file\0" + str(path).encode() + b"\0" + _read(path))
            named = True
    return AgentProgram(
        command=list(command),
        directory=str(directory.resolve()),
        commit=commit,
        uncommitted=uncommitted,
        code=digest.hexdigest() if named else None,
    )


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def program_changes(parent: AgentProgram | None, fork: AgentProgram | None) -> list[str]:
    """What changed in the agent's program from its parent's run to the fork's, in words; nothing when either was
    not started by Minutehand or the code could not be told."""
    if parent is None or fork is None:
        return []
    said: list[str] = []
    if parent.command != fork.command:
        said.append(f"its command: {' '.join(parent.command)} -> {' '.join(fork.command)}")
    if parent.code is not None and fork.code is not None and parent.code != fork.code:
        before, after = _version(parent), _version(fork)
        if before == after:
            before, after = f"{before} (code {parent.code[:12]})", f"{after} (code {fork.code[:12]})"
        said.append(f"its program's code: {before} -> {after}")
    return said


def _version(program: AgentProgram) -> str:
    if program.commit is None:
        return f"code {program.code[:12] if program.code else 'unknown'}"
    return program.commit[:12] + (" with uncommitted changes" if program.uncommitted else "")
