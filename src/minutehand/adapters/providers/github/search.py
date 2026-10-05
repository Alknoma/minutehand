"""Code search's `q`: the terms and qualifiers it is made of, and whether a file matches them.

Answered, in any combination: bare terms and quoted phrases (all must match, each as whole tokens, in any case),
`AND` between them, and the qualifiers `repo:` (repeatable: any of them), `user:`, `org:`, `language:`, `path:`,
`filename:`, `extension:` and `in:` (`file`, `path` or both). Refused with GitHub's 422, because they are not
evaluated here: `OR`, `NOT`, a negated qualifier (`-path:…`), and the qualifiers `size:`, `fork:`, `is:` and
`symbol:`. A word with a colon that is none of these is searched for as text.

Tokens are what the index sees: runs of letters, digits and underscores, folded to lower case. `check` does not
match `checkout`, and `.`, `/`, `(` and the like separate tokens rather than being searched for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from minutehand.adapters.providers.github import content, wire

SECTION = "/search/search#search-code"
_PART = re.compile(r'-?[A-Za-z_]+:"[^"]*"|"[^"]*"|\S+')
_QUALIFIER = re.compile(r"^(-?)([A-Za-z_]+):(.+)$")
_TOKEN = re.compile(r"[A-Za-z0-9_]+")

ANSWERED = {"repo", "user", "org", "language", "path", "filename", "extension", "in"}
REFUSED = {"size", "fork", "is", "symbol"}
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
    scopes: set[str] = field(default_factory=lambda: set(SCOPES))

    def matches(self, file: wire.StoredFile, text: str) -> bool:
        name = file.path.rsplit("/", 1)[-1].lower()
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
    """`q` as terms and qualifiers, or GitHub's 422 for what it cannot be read as."""
    if not q.strip():
        raise wire.validation_failed(SECTION, wire.FieldError(resource="Search", field="q", code="missing"))
    query = CodeQuery()
    for part in _PART.findall(q):
        if part in ("OR", "NOT"):
            raise _refused(f"The {part} operator is not answered by this GitHub; search for each term separately.")
        if part == "AND":
            continue
        qualifier = _QUALIFIER.match(part)
        if qualifier is not None and qualifier.group(2).lower() in ANSWERED | REFUSED:
            negated, key, value = qualifier.group(1), qualifier.group(2).lower(), qualifier.group(3).strip('"')
            if negated or key in REFUSED:
                raise _refused(f"The qualifier {part} is not answered by this GitHub.")
            _qualify(query, key, value)
            continue
        found = tokens(part.strip('"'))
        if found:
            query.terms.append(found)
    if not query.terms:
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
    else:
        scopes = {s.strip() for s in lowered.split(",")}
        if not scopes or not scopes <= SCOPES:
            raise _refused(f"The in qualifier takes file, path or both, not {value}.")
        query.scopes = scopes
