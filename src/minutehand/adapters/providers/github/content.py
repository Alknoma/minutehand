"""What a repository's files look like from the outside: refs, directories, trees, languages.

Every branch and every commit of a seeded repository shows the same files, the head's: history is a list of
commits, not a sequence of trees. `README.md` says so.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from minutehand.adapters.providers.github import wire

CONTENTS_INLINE_LIMIT = 1024 * 1024
"""Past 1 MiB the contents endpoint carries no bytes (`"content": ""`, `"encoding": "none"`)."""
SEARCH_INDEX_LIMIT = 384 * 1024
"""Code search indexes no file larger than this."""

# Linguist's names for the extensions a code reader meets, and which of them it leaves out of a repository's
# language breakdown: prose and data are not code.
LANGUAGES: dict[str, str] = {
    "py": "Python",
    "pyi": "Python",
    "js": "JavaScript",
    "mjs": "JavaScript",
    "cjs": "JavaScript",
    "jsx": "JavaScript",
    "ts": "TypeScript",
    "tsx": "TSX",
    "go": "Go",
    "rb": "Ruby",
    "java": "Java",
    "kt": "Kotlin",
    "rs": "Rust",
    "c": "C",
    "h": "C",
    "cc": "C++",
    "cpp": "C++",
    "hpp": "C++",
    "cs": "C#",
    "php": "PHP",
    "swift": "Swift",
    "scala": "Scala",
    "sh": "Shell",
    "bash": "Shell",
    "html": "HTML",
    "css": "CSS",
    "scss": "SCSS",
    "sql": "SQL",
    "md": "Markdown",
    "yml": "YAML",
    "yaml": "YAML",
    "json": "JSON",
    "toml": "TOML",
}
NOT_CODE = {"Markdown", "YAML", "JSON", "TOML"}
VENDORED = ("vendor/", "node_modules/", "third_party/")
DOCUMENTATION = ("docs/", "doc/")

HEAD = "HEAD"
BRANCH_PREFIX = "refs/heads/"


def language_of(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    _, dot, extension = name.rpartition(".")
    return LANGUAGES.get(extension.lower()) if dot else None


def _counted(path: str) -> bool:
    lowered = path.lower()
    if lowered.startswith(VENDORED + DOCUMENTATION) or lowered.endswith(".min.js"):
        return False
    language = language_of(path)
    return language is not None and language not in NOT_CODE


def breakdown(files: list[wire.StoredFile]) -> list[tuple[str, int]]:
    """Bytes per language, largest first, the way linguist counts them: no prose, data, vendored or docs."""
    totals: dict[str, int] = {}
    for file in files:
        if _counted(file.path):
            language = language_of(file.path)
            assert language is not None
            totals[language] = totals.get(language, 0) + file.size
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def is_binary(raw: bytes) -> bool:
    if b"\0" in raw[:8000]:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def resolve(repository: wire.StoredRepository, ref: str | None) -> wire.StoredCommit | None:
    """The commit a ref names: HEAD, a branch (bare or under refs/heads/), or a commit's sha or its unique prefix
    of at least seven characters. None for an empty repository, or a ref that names nothing."""
    if not repository.commits:
        return None
    head = repository.commits[0]
    if ref is None or ref == HEAD:
        return head
    branch = ref.removeprefix(BRANCH_PREFIX)
    if branch == repository.default_branch or branch in repository.branches:
        return head
    lowered = ref.lower()
    if len(lowered) >= 7 and all(c in "0123456789abcdef" for c in lowered):
        found = [c for c in repository.commits if c.sha.startswith(lowered)]
        if len(found) == 1:
            return found[0]
    return None


def tree_sha(repository: wire.StoredRepository, directory: str) -> str:
    """A directory's tree id; the root's is ""."""
    return hashlib.sha1(f"tree\0{repository.full_name.lower()}\0{directory}".encode()).hexdigest()


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    file: wire.StoredFile | None
    """None for a directory."""


def directories(files: list[wire.StoredFile]) -> list[str]:
    found: set[str] = set()
    for file in files:
        parts = file.path.split("/")
        for depth in range(1, len(parts)):
            found.add("/".join(parts[:depth]))
    return sorted(found)


def children(files: list[wire.StoredFile], directory: str) -> list[Entry]:
    """The entries directly inside `directory` ("" for the root), in git's order: by name."""
    prefix = f"{directory}/" if directory else ""
    found: dict[str, Entry] = {}
    for file in files:
        if not file.path.startswith(prefix):
            continue
        rest = file.path[len(prefix) :]
        name, slash, _ = rest.partition("/")
        if slash:
            found.setdefault(name, Entry(name=name, path=prefix + name, file=None))
        else:
            found[name] = Entry(name=name, path=file.path, file=file)
    return [found[name] for name in sorted(found)]


def under(files: list[wire.StoredFile], directory: str) -> list[Entry]:
    """Every directory and file below `directory`, recursively, in git's order: by path."""
    prefix = f"{directory}/" if directory else ""
    entries = [Entry(name=d.rsplit("/", 1)[-1], path=d, file=None) for d in directories(files) if d.startswith(prefix)]
    entries += [Entry(name=f.path.rsplit("/", 1)[-1], path=f.path, file=f) for f in files if f.path.startswith(prefix)]
    return sorted(entries, key=lambda e: e.path)
