"""YouTrack's search query and command languages, as far as this fake reads them.

Search (`GET /api/issues?query=`): `project:`, `for:` and `Assignee:` (a login,
`me` or `Unassigned`), `State:`, `issue id:`, `#Resolved`, `#Unresolved`, and
free text. A value with a space is braced (`State: {In Progress}`), several values
are a comma list (`State: Open, Fixed`) matching any of them, and every term must
hold. Anything else is free text, matched word by word against the summary,
description and readable id.

Commands (`POST /api/commands`): `State <value>`, `Assignee <login>` and
`for <login>`, any number in one query, each value running until the next
command word or braced.
"""

from __future__ import annotations

import re
from enum import StrEnum

from minutehand.adapters.providers.youtrack.wire import bad_request
from minutehand.domain.scenario import Model

ME = "me"
UNASSIGNED = "unassigned"


class Attribute(StrEnum):
    PROJECT = "project"
    ASSIGNEE = "assignee"
    STATE = "state"
    ISSUE_ID = "issue id"


_ATTRIBUTE_NAMES: dict[str, Attribute] = {
    "project": Attribute.PROJECT,
    "for": Attribute.ASSIGNEE,
    "assignee": Attribute.ASSIGNEE,
    "state": Attribute.STATE,
    "issue id": Attribute.ISSUE_ID,
}

_VALUE = r"(?:\{[^}]*\}|[^\s,{}]+)"
_TERM = re.compile(
    rf"(?<![\w-])(project|for|assignee|state|issue\s+id)\s*:\s*({_VALUE}(?:\s*,\s*{_VALUE})*)",
    re.IGNORECASE,
)
_RESOLUTION = re.compile(r"(?<!\S)#(resolved|unresolved)(?!\S)", re.IGNORECASE)
_ONE_VALUE = re.compile(_VALUE)
_WORD = re.compile(r"\{[^}]*\}|\S+")


class Clause(Model):
    """One `attribute: value, value` term; an issue matches when any value does."""

    attribute: Attribute
    values: list[str]


class Search(Model):
    clauses: list[Clause] = []
    resolved: bool | None = None
    words: list[str] = []


def _unbrace(value: str) -> str:
    value = value.strip()
    return value[1:-1].strip() if value.startswith("{") and value.endswith("}") else value


def parse_search(text: str | None) -> Search:
    if text is None or not text.strip():
        return Search()
    clauses: list[Clause] = []
    for match in _TERM.finditer(text):
        name = re.sub(r"\s+", " ", match.group(1).lower())
        values = [_unbrace(v) for v in _ONE_VALUE.findall(match.group(2))]
        clauses.append(Clause(attribute=_ATTRIBUTE_NAMES[name], values=values))
    rest = _TERM.sub(" ", text)
    resolved: bool | None = None
    for match in _RESOLUTION.finditer(rest):
        resolved = match.group(1).lower() == "resolved"
    rest = _RESOLUTION.sub(" ", rest)
    words = [w for w in (_unbrace(w) for w in _WORD.findall(rest)) if w]
    return Search(clauses=clauses, resolved=resolved, words=words)


class CommandWord(StrEnum):
    STATE = "state"
    ASSIGNEE = "assignee"
    FOR = "for"


class Command(Model):
    word: CommandWord
    value: str


_COMMAND_WORDS: dict[str, CommandWord] = {member.value: member for member in CommandWord}


def parse_command(text: str | None) -> list[Command]:
    """Every command in one query, in order. A query that is not one of them is refused."""
    if text is None or not text.strip():
        raise bad_request("Command query is required")
    commands: list[Command] = []
    word: CommandWord | None = None
    value: list[str] = []
    for token in _WORD.findall(text):
        named = _COMMAND_WORDS.get(token.lower()) if not token.startswith("{") else None
        if named is not None:
            if word is not None:
                commands.append(_finished(text, word, value))
            word, value = named, []
            continue
        if word is None:
            raise bad_request(f"Unknown command: {text.strip()}")
        value.append(_unbrace(token))
    if word is None:
        raise bad_request(f"Unknown command: {text.strip()}")
    commands.append(_finished(text, word, value))
    return commands


def _finished(text: str, word: CommandWord, value: list[str]) -> Command:
    joined = " ".join(v for v in value if v)
    if not joined:
        raise bad_request(f"Command {word.value} needs a value: {text.strip()}")
    return Command(word=word, value=joined)
