"""YouTrack's search query and command languages, as far as this fake reads them. Parsing only: what a value names
is decided against the instance by the app, which refuses a value nothing in scope has.

Search (`GET /api/issues?query=`, `POST /api/issuesGetter/count`):

- `attribute: value` for `project`, `issue id`, `tag`, `for`, `reporter`/`by`, `created`, `updated`,
  `resolved date`, `summary`, `description`, `has`, and every custom field by its name (`State`, `Assignee`,
  `Priority`, `Type`, `Due Date`, …). A value with a space is braced (`State: {In Progress}`); several values are a
  comma list matching any of them; `-` alone is "no value"; `-value` excludes it; `low .. high` is a range, either
  end `*`; dates are `2026-08-24`, `2026-08-24T10:00`, `Today`, `Yesterday` or `Tomorrow`.
- `#value` (`#Unresolved`, `#Resolved`, `#me`, `#Bug`) and `-#value`.
- Free text, word by word or `"as a phrase"`, and `-word` to exclude one.
- `or` between terms; every term of one side must hold.
- `sort by: field [asc|desc], …` last.

A parenthesis, an attribute nothing is called, or a value left open is refused with YouTrack's `invalid_query`
rather than read as text and answered.

Commands (`POST /api/commands`): `<field> <value>`, `for <login>`, `tag <name>`, `untag <name>`, any number in one
query, each value running until the next command word or braced.
"""

from __future__ import annotations

import re
from enum import StrEnum

from minutehand.adapters.providers.youtrack.wire import bad_request, unparsed_query
from minutehand.domain.scenario import Model

ME = "me"
UNASSIGNED = "unassigned"
EMPTY = "-"


class Keyword(StrEnum):
    """The attributes that are not custom fields."""

    PROJECT = "project"
    ISSUE_ID = "issue id"
    TAG = "tag"
    FOR = "for"
    REPORTER = "reporter"
    CREATED = "created"
    UPDATED = "updated"
    RESOLVED_DATE = "resolved date"
    SUMMARY = "summary"
    DESCRIPTION = "description"
    HAS = "has"


_KEYWORD_ALIASES: dict[str, Keyword] = {
    **{k.value: k for k in Keyword},
    "by": Keyword.REPORTER,
    "created by": Keyword.REPORTER,
    "tags": Keyword.TAG,
}


class Item(Model):
    """One value of an attribute: a name, `-` (no value), `-name` (not it), or a range."""

    text: str
    negated: bool = False
    empty: bool = False
    upper: str | None = None


class Clause(Model):
    """`attribute: value, value`: an issue matches when any value does (and none it excludes)."""

    attribute: str
    keyword: Keyword | None = None
    items: list[Item]


class Shortcut(Model):
    name: str
    negated: bool = False


class Word(Model):
    text: str
    negated: bool = False


class Conjunction(Model):
    clauses: list[Clause] = []
    shortcuts: list[Shortcut] = []
    words: list[Word] = []


class SortKey(Model):
    attribute: str
    keyword: Keyword | None
    descending: bool


class Search(Model):
    alternatives: list[Conjunction]
    sort: list[SortKey] = []


_SORT = re.compile(r"(?i)(?:^|\s)sort\s+by\s*:")


def _normal(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def parse_search(text: str | None, field_names: list[str]) -> Search:
    """The query as terms. `field_names` are the custom fields the instance defines, which may hold spaces."""
    if text is None or not text.strip():
        return Search(alternatives=[Conjunction()])
    sort: list[SortKey] = []
    found = _SORT.search(text)
    body = text
    if found is not None:
        body = text[: found.start()]
        sort = _sort_keys(text, text[found.end() :], field_names)
    reader = _Reader(text, body, {_normal(n): n for n in field_names})
    return Search(alternatives=reader.read(), sort=sort)


def _attribute(name: str, fields: dict[str, str]) -> tuple[str, Keyword | None] | None:
    normal = _normal(name)
    if normal in fields:
        return fields[normal], None
    if normal in _KEYWORD_ALIASES:
        return normal, _KEYWORD_ALIASES[normal]
    return None


def _sort_keys(text: str, terms: str, field_names: list[str]) -> list[SortKey]:
    fields = {_normal(n): n for n in field_names}
    keys: list[SortKey] = []
    for part in terms.split(","):
        part = part.strip()
        if not part:
            raise unparsed_query(text, "sort by names no field")
        descending = False
        lowered = part.lower()
        for direction in (" asc", " desc"):
            if lowered.endswith(direction):
                descending = direction == " desc"
                part = part[: -len(direction)].strip()
                break
        name = part[1:-1] if part.startswith("{") and part.endswith("}") else part
        named = _attribute(name, fields)
        if named is None:
            raise unparsed_query(text, f"cannot sort by {name!r}")
        keys.append(SortKey(attribute=named[0], keyword=named[1], descending=descending))
    return keys


class _Reader:
    def __init__(self, text: str, body: str, fields: dict[str, str]) -> None:
        self._text = text
        self._body = body
        self._fields = fields
        self._at = 0

    def _refuse(self, why: str) -> Exception:
        return unparsed_query(self._text, why)

    def _skip(self) -> None:
        while self._at < len(self._body) and self._body[self._at].isspace():
            self._at += 1

    def _done(self) -> bool:
        self._skip()
        return self._at >= len(self._body)

    def read(self) -> list[Conjunction]:
        alternatives: list[Conjunction] = []
        current = Conjunction()
        while not self._done():
            char = self._body[self._at]
            if char in "()":
                raise self._refuse("parentheses are not read")
            if char == '"':
                close = self._body.find('"', self._at + 1)
                if close < 0:
                    raise self._refuse("a quote is left open")
                phrase = self._body[self._at + 1 : close].strip()
                current = _with(current, words=[Word(text=phrase)])
                self._at = close + 1
                continue
            if char == "#" or self._body.startswith("-#", self._at):
                negated = char == "-"
                self._at += 2 if negated else 1
                current = _with(current, shortcuts=[Shortcut(name=self._value_text(), negated=negated)])
                continue
            clause = self._clause()
            if clause is not None:
                current = _with(current, clauses=[clause])
                continue
            word = self._token()
            if word.lower() == "or":
                alternatives.append(current)
                current = Conjunction()
            elif word.lower() == "and":
                continue
            elif word.startswith("-") and len(word) > 1:
                current = _with(current, words=[Word(text=word[1:], negated=True)])
            else:
                current = _with(current, words=[Word(text=word)])
        alternatives.append(current)
        if any(c == Conjunction() for c in alternatives) and len(alternatives) > 1:
            raise self._refuse("`or` needs a term on each side")
        return alternatives

    def _clause(self) -> Clause | None:
        colon = self._body.find(":", self._at)
        if colon < 0:
            return None
        name = self._body[self._at : colon]
        if "{" in name or "}" in name:
            return None
        named = _attribute(name, self._fields)
        if named is None:
            if name.strip() and not any(c.isspace() for c in name.strip()):
                raise self._refuse(f"no field is called {name.strip()!r}")
            return None
        self._at = colon + 1
        items = [self._item()]
        while True:
            self._skip()
            if self._at < len(self._body) and self._body[self._at] == ",":
                self._at += 1
                items.append(self._item())
                continue
            break
        return Clause(attribute=named[0], keyword=named[1], items=items)

    def _item(self) -> Item:
        self._skip()
        if self._at >= len(self._body):
            raise self._refuse("an attribute has no value")
        if self._body[self._at] == "-":
            following = self._body[self._at + 1 : self._at + 2]
            if following == "" or following.isspace() or following == ",":
                self._at += 1
                return Item(text=EMPTY, empty=True)
            self._at += 1
            return Item(text=self._value_text(), negated=True)
        low = self._value_text()
        mark = self._at
        self._skip()
        if self._body.startswith("..", self._at):
            self._at += 2
            self._skip()
            return Item(text=low, upper=self._value_text())
        self._at = mark
        return Item(text=low)

    def _value_text(self) -> str:
        if self._at < len(self._body) and self._body[self._at] == "{":
            close = self._body.find("}", self._at)
            if close < 0:
                raise self._refuse("a brace is left open")
            value = self._body[self._at + 1 : close].strip()
            self._at = close + 1
            return value
        start = self._at
        while self._at < len(self._body):
            char = self._body[self._at]
            if char.isspace() or char in ",()" or self._body.startswith("..", self._at):
                break
            self._at += 1
        value = self._body[start : self._at]
        if not value:
            raise self._refuse("an attribute has no value")
        return value

    def _token(self) -> str:
        start = self._at
        while self._at < len(self._body) and not self._body[self._at].isspace():
            if self._body[self._at] in "()":
                break
            self._at += 1
        return self._body[start : self._at]


def _with(
    conjunction: Conjunction,
    *,
    clauses: list[Clause] | None = None,
    shortcuts: list[Shortcut] | None = None,
    words: list[Word] | None = None,
) -> Conjunction:
    return Conjunction(
        clauses=[*conjunction.clauses, *(clauses or [])],
        shortcuts=[*conjunction.shortcuts, *(shortcuts or [])],
        words=[*conjunction.words, *(words or [])],
    )


# --------------------------------------------------------------------------- commands


class CommandWord(StrEnum):
    FIELD = "field"
    FOR = "for"
    TAG = "tag"
    UNTAG = "untag"


class Command(Model):
    word: CommandWord
    field: str | None = None
    value: str


_WORD = re.compile(r"\{[^}]*\}|\S+")


def parse_command(text: str | None, field_names: list[str]) -> list[Command]:
    """Every command in one query, in order. A query that is not one of them is refused."""
    if text is None or not text.strip():
        raise bad_request("Command query is required")
    names = {_normal(n): n for n in field_names}
    tokens = _WORD.findall(text)
    commands: list[Command] = []
    current: tuple[CommandWord, str | None] | None = None
    value: list[str] = []
    at = 0
    while at < len(tokens):
        started = _command_word(tokens, at, names)
        if started is not None:
            word, field, width = started
            if current is not None:
                commands.append(_finished(text, current, value))
            current, value = (word, field), []
            at += width
            continue
        if current is None:
            raise bad_request(f"Unknown command: {text.strip()}")
        token = tokens[at]
        value.append(token[1:-1].strip() if token.startswith("{") and token.endswith("}") else token)
        at += 1
    if current is None:
        raise bad_request(f"Unknown command: {text.strip()}")
    commands.append(_finished(text, current, value))
    return commands


def _command_word(tokens: list[str], at: int, names: dict[str, str]) -> tuple[CommandWord, str | None, int] | None:
    if tokens[at].startswith("{"):
        return None
    for width in range(min(4, len(tokens) - at), 0, -1):
        candidate = _normal(" ".join(tokens[at : at + width]))
        if candidate in names:
            return CommandWord.FIELD, names[candidate], width
    simple = tokens[at].lower()
    if simple in (CommandWord.FOR.value, CommandWord.TAG.value, CommandWord.UNTAG.value):
        return CommandWord(simple), None, 1
    return None


def _finished(text: str, current: tuple[CommandWord, str | None], value: list[str]) -> Command:
    joined = " ".join(v for v in value if v)
    if not joined:
        raise bad_request(f"Command {current[1] or current[0].value} needs a value: {text.strip()}")
    return Command(word=current[0], field=current[1], value=joined)
