"""Code search's `q`: the terms and qualifiers it is made of, and whether a file matches them.

The REST endpoint takes the legacy code search syntax, not the newer one of GitHub's web search
(https://docs.github.com/en/search-github/searching-on-github/searching-code). Answered, in any combination: bare
terms and quoted phrases (all must match, each as whole tokens, in any case), `AND` between them, and the
qualifiers `repo:` (repeatable: any of them), `user:`, `org:`, `language:`, `path:`, `filename:`, `extension:`,
`in:` (`file`, `path` or both; the contents alone when it is left out), `size:` (the file's bytes: `n`, `>n`,
`>=n`, `<n`, `<=n`, `n..n`, `n..*` or `*..n`) and `fork:` (`true` or `only`; no repository here is a fork). A word
with a colon that is none of these is searched for as text. A query needs a term unless it names a `filename:`.

Refused by name (`NotServed`), because the legacy syntax documents nothing of what they do: `OR`, `NOT`, a negated
qualifier (`-path:…`), `is:` and `symbol:` (each documented only for the newer syntax or for search other than
code), a `size:` or `fork:` value other than those above, and a second `size:`.

Tokens are what the index sees: runs of letters, digits and underscores, folded to lower case. `check` does not
match `checkout`, and `.`, `/`, `(` and the like separate tokens rather than being searched for.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from minutehand.adapters.providers.github import content, wire
from minutehand.domain.errors import NotServed

SECTION = "/search/search#search-code"
_PART = re.compile(r'-?[A-Za-z_]+:"[^"]*"|"[^"]*"|\S+')
_QUALIFIER = re.compile(r"^(-?)([A-Za-z_]+):(.+)$")
_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_SIZE = re.compile(r"^(?:(>=|<=|>|<)(\d+)|(\d+)\.\.(\d+|\*)|\*\.\.(\d+)|(\d+))$")

ANSWERED = {"repo", "user", "org", "language", "path", "filename", "extension", "in", "size", "fork"}
NEWER_SYNTAX = {"is", "symbol"}
# `size:`'s comparisons, GitHub's own spelling, as inclusive bounds in bytes.
_COMPARED: dict[str, Callable[[int], tuple[int, int | None]]] = {
    ">": lambda n: (n + 1, None),
    ">=": lambda n: (n, None),
    "<": lambda n: (0, n - 1),
    "<=": lambda n: (0, n),
}
FORKS = {"true", "only"}
SCOPES = {"file", "path"}

# The language names and aliases `language:` takes, folded, to linguist's name.
_LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "golang": "go",
    "rb": "ruby",
    "rs": "rust",
    "c++": "c++",
    "cpp": "c++",
    "csharp": "c#",
    "sh": "shell",
    "bash": "shell",
    "yml": "yaml",
}


def tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN.finditer(text)]


def _refused(message: str) -> wire.Refusal:
    return wire.validation_failed(
        SECTION, wire.FieldError(resource="Search", field="q", code="invalid", message=message)
    )


@dataclass
class CodeQuery:
    terms: list[list[str]] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    filenames: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    scopes: set[str] = field(default_factory=lambda: {"file"})
    sizes: tuple[int, int | None] | None = None
    forks_only: bool = False

    def matches(self, file: wire.StoredFile, text: str) -> bool:
        name = file.path.rsplit("/", 1)[-1].lower()
        if self.forks_only:  # no repository here is a fork (`RepositoryOut.fork`)
            return False
        if self.sizes is not None:
            low, high = self.sizes
            if file.size < low or (high is not None and file.size > high):
                return False
        if self.languages and (content.language_of(file.path) or "").lower() not in self.languages:
            return False
        if self.paths and not any(_in_path(file.path, p) for p in self.paths):
            return False
        if self.filenames and name not in self.filenames:
            return False
        if self.extensions and not any(name.endswith("." + e) for e in self.extensions):
            return False
        seen: list[str] = []
        if "file" in self.scopes:
            seen += tokens(text)
        if "path" in self.scopes:
            seen += tokens(file.path)
        return all(_holds(seen, term) for term in self.terms)


def _in_path(path: str, wanted: str) -> bool:
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    wanted = wanted.strip("/")
    return wanted == "" or directory == wanted or directory.startswith(wanted + "/") or path == wanted


def _holds(seen: list[str], term: list[str]) -> bool:
    width = len(term)
    return any(seen[i : i + width] == term for i in range(len(seen) - width + 1))


def parse(q: str) -> CodeQuery:
    """`q` as terms and qualifiers, GitHub's 422 for what it cannot be read as, or `NotServed` naming what it holds
    that the legacy syntax does not document."""
    if not q.strip():
        raise wire.validation_failed(SECTION, wire.FieldError(resource="Search", field="q", code="missing"))
    query = CodeQuery()
    for part in _PART.findall(q):
        if part in ("OR", "NOT"):
            raise NotServed(
                f"the {part} operator in q: the legacy code search syntax this endpoint takes documents none"
            )
        if part == "AND":
            continue
        qualifier = _QUALIFIER.match(part)
        if qualifier is not None and qualifier.group(2).lower() in ANSWERED | NEWER_SYNTAX:
            negated, key, value = qualifier.group(1), qualifier.group(2).lower(), qualifier.group(3).strip('"')
            if negated:
                raise NotServed(
                    f"the negated qualifier {part} in q: the legacy code search syntax documents no negation"
                )
            if key in NEWER_SYNTAX:
                raise NotServed(
                    f"the qualifier {part} in q: it is the newer code search syntax's, not the legacy one's"
                )
            _qualify(query, key, value)
            continue
        found = tokens(part.strip('"'))
        if found:
            query.terms.append(found)
    if not query.terms and not query.filenames:
        raise _refused("Must include at least one search term.")
    return query


def _qualify(query: CodeQuery, key: str, value: str) -> None:
    lowered = value.lower()
    if key == "repo":
        if lowered.count("/") != 1:
            raise _refused(f"The repo qualifier takes owner/name, not {value}.")
        query.repos.append(lowered)
    elif key in ("user", "org"):
        query.owners.append(lowered)
    elif key == "language":
        query.languages.append(_LANGUAGE_ALIASES.get(lowered, lowered))
    elif key == "path":
        query.paths.append(value)
    elif key == "filename":
        query.filenames.append(lowered)
    elif key == "extension":
        query.extensions.append(lowered.lstrip("."))
    elif key == "size":
        query.sizes = _size(query, value)
    elif key == "fork":
        if lowered not in FORKS:
            raise NotServed(f"fork:{value} in q: the legacy code search syntax documents only fork:true and fork:only")
        query.forks_only = lowered == "only"
    else:
        scopes = {s.strip() for s in lowered.split(",")}
        if not scopes or not scopes <= SCOPES:
            raise _refused(f"The in qualifier takes file, path or both, not {value}.")
        query.scopes = scopes


def _size(query: CodeQuery, value: str) -> tuple[int, int | None]:
    """`size:`'s bounds in bytes, both inclusive, with `None` for no upper one."""
    if query.sizes is not None:
        raise NotServed(f"a second size qualifier, size:{value}, in q: the legacy syntax documents one")
    found = _SIZE.match(value)
    if found is None:
        raise NotServed(f"size:{value} in q: the legacy syntax documents n, >n, >=n, <n, <=n, n..n, n..* and *..n")
    above, bound, low, high, upto, exactly = found.groups()
    if exactly is not None:
        return int(exactly), int(exactly)
    if upto is not None:
        return 0, int(upto)
    if low is not None:
        return int(low), None if high == "*" else int(high)
    return _COMPARED[above](int(bound))


@dataclass(frozen=True)
class TermAt:
    text: str
    start: int
    end: int


def text_match(query: CodeQuery, text: str) -> tuple[str, list[TermAt]]:
    """The fragment a text match shows: the first line holding one of the query's terms, and where each term sits
    in it. A file that matched on its path alone shows its first line and no terms."""
    lines = text.splitlines()
    for line in lines:
        spans = [(m.group(0).lower(), m.start(), m.end()) for m in _TOKEN.finditer(line)]
        seen = [token for token, _, _ in spans]
        found: list[TermAt] = []
        for term in query.terms:
            width = len(term)
            for i in range(len(seen) - width + 1):
                if seen[i : i + width] == term:
                    start, end = spans[i][1], spans[i + width - 1][2]
                    found.append(TermAt(text=line[start:end], start=start, end=end))
        if found:
            return line, sorted(found, key=lambda at: at.start)
    return (lines[0] if lines else ""), []
