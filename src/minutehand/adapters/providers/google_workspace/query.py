"""Drive's `q` search language: parsed into a typed tree, then evaluated against one file at a time.

Supported, combined with `and`, `or`, `not` and parentheses:

| term | operators |
|---|---|
| `name` | `=`, `!=`, `contains` (a prefix of the name or of a word in it, any case) |
| `fullText` | `contains` (whole words of the name, description and text, any case; `"..."` is a phrase) |
| `mimeType` | `=`, `!=`, `contains` |
| `'<id>' in parents` | |
| `trashed`, `starred` | `=`, `!=` against `true` / `false` |
| `createdTime`, `modifiedTime` | `=`, `!=`, `<`, `<=`, `>`, `>=` against an RFC 3339 literal |

A query Drive would reject — an unterminated literal, an unknown term, an operator a
term does not take, a value of the wrong type — is `QueryError` (Drive's 400 `invalid`).
A term real Drive evaluates and this fake does not (`owners`, `sharedWithMe`,
`properties`, …) is `QueryNotSupported`, so it is never answered as though it matched
nothing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from minutehand.domain.scenario import Model


class QueryError(ValueError):
    """Not a Drive query: real Drive answers 400 `invalid`."""


class QueryNotSupported(ValueError):
    """A real Drive term this fake does not evaluate."""


class Term(StrEnum):
    NAME = "name"
    FULL_TEXT = "fullText"
    MIME_TYPE = "mimeType"
    TRASHED = "trashed"
    STARRED = "starred"
    CREATED_TIME = "createdTime"
    MODIFIED_TIME = "modifiedTime"


class Op(StrEnum):
    EQ = "="
    NE = "!="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    CONTAINS = "contains"


UNSUPPORTED_TERMS = frozenset(
    {
        "owners",
        "writers",
        "readers",
        "sharedWithMe",
        "visibility",
        "properties",
        "appProperties",
        "shortcutDetails.targetId",
        "memberCount",
        "organizerCount",
        "hidden",
        "viewedByMeTime",
        "parents",
    }
)
"""Terms in Drive's grammar that this fake refuses to evaluate rather than answer wrongly."""

_TAKES: dict[Term, frozenset[Op]] = {
    Term.NAME: frozenset({Op.EQ, Op.NE, Op.CONTAINS}),
    Term.FULL_TEXT: frozenset({Op.CONTAINS}),
    Term.MIME_TYPE: frozenset({Op.EQ, Op.NE, Op.CONTAINS}),
    Term.TRASHED: frozenset({Op.EQ, Op.NE}),
    Term.STARRED: frozenset({Op.EQ, Op.NE}),
    Term.CREATED_TIME: frozenset({Op.EQ, Op.NE, Op.LT, Op.LE, Op.GT, Op.GE}),
    Term.MODIFIED_TIME: frozenset({Op.EQ, Op.NE, Op.LT, Op.LE, Op.GT, Op.GE}),
}
_BOOLEAN_TERMS = frozenset({Term.TRASHED, Term.STARRED})
_TIME_TERMS = frozenset({Term.CREATED_TIME, Term.MODIFIED_TIME})


class TextCompare(Model):
    kind: Literal["text"] = "text"
    term: Term
    op: Op
    value: str


class FlagCompare(Model):
    kind: Literal["flag"] = "flag"
    term: Term
    op: Op
    value: bool


class TimeCompare(Model):
    kind: Literal["time"] = "time"
    term: Term
    op: Op
    value: datetime


class InParents(Model):
    kind: Literal["in_parents"] = "in_parents"
    folder: str


class AllOf(Model):
    kind: Literal["all"] = "all"
    terms: list[Query]


class AnyOf(Model):
    kind: Literal["any"] = "any"
    terms: list[Query]


class Negated(Model):
    kind: Literal["not"] = "not"
    term: Query


type Query = Annotated[
    TextCompare | FlagCompare | TimeCompare | InParents | AllOf | AnyOf | Negated, Field(discriminator="kind")
]


class Candidate(Model):
    """What a query can see of one file."""

    name: str
    mime_type: str
    parents: list[str]
    trashed: bool
    starred: bool
    created: datetime
    modified: datetime
    full_text: str = Field(description="Name, description and readable content, joined")


# --------------------------------------------------------------------------- scanning


class _Kind(StrEnum):
    LITERAL = "literal"
    WORD = "word"
    OP = "op"
    OPEN = "open"
    CLOSE = "close"


_TOKEN = re.compile(r"\s+|\(|\)|<=|>=|!=|=|<|>|[A-Za-z_][A-Za-z0-9_.]*")


def _scan(text: str) -> list[tuple[_Kind, str]]:
    tokens: list[tuple[_Kind, str]] = []
    at = 0
    while at < len(text):
        if text[at] == "'":
            at += 1
            chars: list[str] = []
            while True:
                if at >= len(text):
                    raise QueryError("unterminated string literal")
                if text[at] == "\\" and at + 1 < len(text):
                    chars.append(text[at + 1])
                    at += 2
                    continue
                if text[at] == "'":
                    at += 1
                    break
                chars.append(text[at])
                at += 1
            tokens.append((_Kind.LITERAL, "".join(chars)))
            continue
        found = _TOKEN.match(text, at)
        if found is None:
            raise QueryError(f"unexpected character {text[at]!r}")
        at = found.end()
        token = found.group(0)
        if token.isspace():
            continue
        if token == "(":
            tokens.append((_Kind.OPEN, token))
        elif token == ")":
            tokens.append((_Kind.CLOSE, token))
        elif token in ("=", "!=", "<", "<=", ">", ">="):
            tokens.append((_Kind.OP, token))
        else:
            tokens.append((_Kind.WORD, token))
    return tokens


# --------------------------------------------------------------------------- parsing


class _Parser:
    def __init__(self, tokens: list[tuple[_Kind, str]]) -> None:
        self._tokens = tokens
        self._at = 0

    def _peek(self) -> tuple[_Kind, str] | None:
        return self._tokens[self._at] if self._at < len(self._tokens) else None

    def _take(self) -> tuple[_Kind, str]:
        token = self._peek()
        if token is None:
            raise QueryError("the query ends early")
        self._at += 1
        return token

    def _keyword(self, word: str) -> bool:
        token = self._peek()
        if token is not None and token[0] is _Kind.WORD and token[1] == word:
            self._at += 1
            return True
        return False

    def parse(self) -> Query:
        query = self._any()
        left = self._peek()
        if left is not None:
            raise QueryError(f"unexpected {left[1]!r}")
        return query

    def _any(self) -> Query:
        terms = [self._all()]
        while self._keyword("or"):
            terms.append(self._all())
        return terms[0] if len(terms) == 1 else AnyOf(terms=terms)

    def _all(self) -> Query:
        terms = [self._unary()]
        while self._keyword("and"):
            terms.append(self._unary())
        return terms[0] if len(terms) == 1 else AllOf(terms=terms)

    def _unary(self) -> Query:
        if self._keyword("not"):
            return Negated(term=self._unary())
        token = self._take()
        if token[0] is _Kind.OPEN:
            inner = self._any()
            if self._take()[0] is not _Kind.CLOSE:
                raise QueryError("unbalanced parentheses")
            return inner
        if token[0] is _Kind.LITERAL:
            return self._membership(token[1])
        if token[0] is _Kind.WORD:
            return self._comparison(token[1])
        raise QueryError(f"unexpected {token[1]!r}")

    def _membership(self, value: str) -> Query:
        if not self._keyword("in"):
            raise QueryError("a literal must be followed by 'in'")
        kind, collection = self._take()
        if kind is not _Kind.WORD:
            raise QueryError("'in' must be followed by a collection")
        if collection == "parents":
            return InParents(folder=value)
        if collection in UNSUPPORTED_TERMS:
            raise QueryNotSupported(f"'{collection}' is a Drive search term this simulation does not evaluate")
        raise QueryError(f"unknown term {collection!r}")

    def _comparison(self, word: str) -> Query:
        if word in UNSUPPORTED_TERMS:
            raise QueryNotSupported(f"'{word}' is a Drive search term this simulation does not evaluate")
        try:
            term = Term(word)
        except ValueError as error:
            raise QueryError(f"unknown term {word!r}") from error
        kind, spelled = self._take()
        if kind is _Kind.WORD and spelled == "contains":
            op = Op.CONTAINS
        elif kind is _Kind.OP:
            op = Op(spelled)
        else:
            raise QueryError(f"{term.value} must be followed by an operator")
        if op not in _TAKES[term]:
            raise QueryError(f"{term.value} does not take {op.value!r}")
        kind, value = self._take()
        if term in _BOOLEAN_TERMS:
            if kind is not _Kind.WORD or value not in ("true", "false"):
                raise QueryError(f"{term.value} compares with true or false")
            return FlagCompare(term=term, op=op, value=value == "true")
        if kind is not _Kind.LITERAL:
            raise QueryError(f"{term.value} compares with a quoted string")
        if term in _TIME_TERMS:
            try:
                moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise QueryError(f"{value!r} is not an RFC 3339 date-time") from error
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return TimeCompare(term=term, op=op, value=moment)
        return TextCompare(term=term, op=op, value=value)


def parse(text: str) -> Query | None:
    """The query, or None for an empty `q` (everything)."""
    tokens = _scan(text)
    if not tokens:
        return None
    return _Parser(tokens).parse()


# --------------------------------------------------------------------------- evaluating


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def name_contains(name: str, needle: str) -> bool:
    """A prefix of the name, or of a word inside it: "Quarterly Plan" contains 'Plan', not 'uarter'."""
    haystack, target = name.lower(), needle.lower()
    if not target:
        return True
    return any(
        haystack.startswith(target, at) for at in range(len(haystack)) if at == 0 or not haystack[at - 1].isalnum()
    )


def full_text_contains(text: str, needle: str) -> bool:
    """Whole words: every word of the needle appears; a needle in double quotes is a phrase, in order."""
    haystack = _words(text)
    stripped = needle.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
        phrase = _words(stripped[1:-1])
        width = len(phrase)
        return bool(phrase) and any(haystack[at : at + width] == phrase for at in range(len(haystack) - width + 1))
    wanted = _words(stripped)
    present = set(haystack)
    return bool(wanted) and all(word in present for word in wanted)


def _ordered(left: datetime, op: Op, right: datetime) -> bool:
    if op is Op.EQ:
        return left == right
    if op is Op.NE:
        return left != right
    if op is Op.LT:
        return left < right
    if op is Op.LE:
        return left <= right
    if op is Op.GT:
        return left > right
    return left >= right


def _text(candidate: Candidate, compare: TextCompare) -> bool:
    if compare.term is Term.FULL_TEXT:
        return full_text_contains(candidate.full_text, compare.value)
    field = candidate.name if compare.term is Term.NAME else candidate.mime_type
    if compare.op is Op.CONTAINS:
        return name_contains(field, compare.value) if compare.term is Term.NAME else compare.value in field
    return (field == compare.value) == (compare.op is Op.EQ)


def matches(query: Query | None, candidate: Candidate) -> bool:
    if query is None:
        return True
    if isinstance(query, AllOf):
        return all(matches(term, candidate) for term in query.terms)
    if isinstance(query, AnyOf):
        return any(matches(term, candidate) for term in query.terms)
    if isinstance(query, Negated):
        return not matches(query.term, candidate)
    if isinstance(query, InParents):
        return query.folder in candidate.parents
    if isinstance(query, FlagCompare):
        flag = candidate.trashed if query.term is Term.TRASHED else candidate.starred
        return (flag == query.value) == (query.op is Op.EQ)
    if isinstance(query, TimeCompare):
        stamp = candidate.created if query.term is Term.CREATED_TIME else candidate.modified
        return _ordered(stamp, query.op, query.value)
    return _text(candidate, query)


def resolved(query: Query | None, alias: str, folder: str) -> Query | None:
    """The query with every `'<alias>' in parents` naming `folder` instead: Drive's `root` is My Drive's id."""
    if query is None:
        return None
    if isinstance(query, AllOf):
        return AllOf(terms=[t for t in (resolved(term, alias, folder) for term in query.terms) if t is not None])
    if isinstance(query, AnyOf):
        return AnyOf(terms=[t for t in (resolved(term, alias, folder) for term in query.terms) if t is not None])
    if isinstance(query, Negated):
        inner = resolved(query.term, alias, folder)
        assert inner is not None
        return Negated(term=inner)
    if isinstance(query, InParents) and query.folder == alias:
        return InParents(folder=folder)
    return query
