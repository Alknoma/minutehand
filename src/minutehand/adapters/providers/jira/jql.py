"""JQL, read into a tree. What the tree means is `search.py`'s; this module only says what was written.

    query   := [or] [ORDER BY sort ("," sort)*]
    or      := and (OR and)*
    and     := not (AND not)*
    not     := NOT not | "(" or ")" | clause
    clause  := field op operand
    op      := = | != | ~ | !~ | > | >= | < | <= | IN | NOT IN | IS | IS NOT
    operand := value | "(" value ("," value)* ")" | EMPTY | NULL | name "(" [value ("," value)*] ")"
    sort    := field [ASC | DESC]

A field is a word, a quoted name, or `cf[10016]`. A value is a word or a quoted string; a relative date
(`-7d`, `2w`, `-1h`) is a word. Keywords are read in any case. Anything else is a 400 naming where it broke:
a query this fake cannot read is never answered with everything. The `WAS` and `CHANGED` operators, which Atlassian's
JQL reference documents (https://support.atlassian.com/jira-software-cloud/docs/jql-operators/), are refused by
name (`NotImplementedError`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from minutehand.adapters.providers.jira.wire import jql_error
from minutehand.domain.scenario import Model


class Op(StrEnum):
    EQ = "="
    NE = "!="
    LIKE = "~"
    NOT_LIKE = "!~"
    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="
    IN = "in"
    NOT_IN = "not in"
    IS = "is"
    IS_NOT = "is not"


class ValueKind(StrEnum):
    TEXT = "literal"
    FUNCTION = "function"
    EMPTY = "empty"


class Value(Model):
    kind: ValueKind
    text: str = ""
    args: list[str] = []
    quoted: bool = False


class Clause(Model):
    kind: Literal["clause"] = "clause"
    field: str
    op: Op
    values: list[Value]


class Not(Model):
    kind: Literal["not"] = "not"
    operand: Node


class And(Model):
    kind: Literal["and"] = "and"
    parts: list[Node]


class Or(Model):
    kind: Literal["or"] = "or"
    parts: list[Node]


Node = Annotated[Clause | Not | And | Or, Field(discriminator="kind")]


class Sort(Model):
    field: str
    ascending: bool


class Query(Model):
    where: Node | None = None
    order: list[Sort] = []


Not.model_rebuild()
And.model_rebuild()
Or.model_rebuild()
Query.model_rebuild()


class TokenKind(StrEnum):
    WORD = "word"
    STRING = "quoted"
    OP = "operator"
    OPEN = "open_paren"
    CLOSE = "close_paren"
    COMMA = "comma"
    END = "end"


class Token(Model):
    kind: TokenKind
    text: str
    at: int


_PUNCTUATION = {"(": TokenKind.OPEN, ")": TokenKind.CLOSE, ",": TokenKind.COMMA}
_OPERATORS = ("!=", ">=", "<=", "!~", "=", "~", ">", "<")
_WORD_END = set(" \t\r\n(),\"'=!~<>")


class Unmatched(Exception):
    """A query Jira reads but that names something it has not got (a field, a value, a function, an operator a
    field does not take, a date it cannot read): the public site answers such a query 200 with no issues, never a
    400 (recorded: `tests/providers/jira/data/observed/jql_*_unknown.http`, `jql_operator_unsupported.http`,
    `jql_date_invalid.http`, `jql_is_not_empty_value.http`, `jql_text_no_word.http`, `jql_period_invalid.http`)."""


SEARCH_INVALID = "Returned if the search request is invalid"
"""The search reference's words for its 400, for a malformed query whose sentence no recording gives."""


def _place(text: str, at: int) -> str:
    """Where `at` is, as Jira says it: `(line 1, character 11)`, both counted from 1."""
    line = text.count("\n", 0, at) + 1
    column = at - (text.rfind("\n", 0, at) + 1) + 1
    return f"(line {line}, character {column})"


def tokens(text: str) -> list[Token]:
    found: list[Token] = []
    at = 0
    while at < len(text):
        char = text[at]
        if char.isspace():
            at += 1
            continue
        if char in _PUNCTUATION:
            found.append(Token(kind=_PUNCTUATION[char], text=char, at=at))
            at += 1
            continue
        operator = next((o for o in _OPERATORS if text.startswith(o, at)), None)
        if operator is not None:
            found.append(Token(kind=TokenKind.OP, text=operator, at=at))
            at += len(operator)
            continue
        if char in "\"'":
            value, at = _quoted(text, at)
            found.append(Token(kind=TokenKind.STRING, text=value, at=at))
            continue
        start = at
        while at < len(text) and text[at] not in _WORD_END:
            if text[at] == "[":
                closing = text.find("]", at)
                if closing < 0:
                    raise jql_error(SEARCH_INVALID)
                at = closing
            at += 1
        found.append(Token(kind=TokenKind.WORD, text=text[start:at], at=start))
    found.append(Token(kind=TokenKind.END, text="", at=len(text)))
    return found


def _quoted(text: str, at: int) -> tuple[str, int]:
    begin = at
    quote = text[at]
    out: list[str] = []
    at += 1
    while at < len(text):
        char = text[at]
        if char == "\\" and at + 1 < len(text):
            out.append(text[at + 1])
            at += 2
            continue
        if char == quote:
            return "".join(out), at + 1
        out.append(char)
        at += 1
    raise jql_error(
        f"Error in the JQL Query: The quoted string '{''.join(out)}' has not been completed. {_place(text, begin)}"
    )


class _Reader:
    def __init__(self, text: str, found: list[Token]) -> None:
        self.text = text
        self._tokens = found
        self._at = 0

    def peek(self) -> Token:
        return self._tokens[self._at]

    def take(self) -> Token:
        token = self._tokens[self._at]
        if token.kind is not TokenKind.END:
            self._at += 1
        return token

    def keyword(self, *words: str) -> bool:
        """The next tokens are these words, in any case; they are taken when they are."""
        for offset, word in enumerate(words):
            token = self._tokens[min(self._at + offset, len(self._tokens) - 1)]
            if token.kind is not TokenKind.WORD or token.text.lower() != word:
                return False
        self._at += len(words)
        return True

    def expect(self, kind: TokenKind, what: str) -> Token:
        token = self.take()
        if token.kind is not kind:
            raise self.expected(what, token)
        return token

    def expected(self, what: str, token: Token) -> Exception:
        """Jira's own sentence where one is recorded (`data/observed/jql_expecting_field.http`,
        `jql_value_missing.http`, `jql_value_unexpected.http`, `jql_and_or_expected.http`,
        `jql_parenthesis_unclosed.http`); the search reference's words otherwise."""
        end = token.kind is TokenKind.END
        where = _place(self.text, token.at)
        if what == "a field name" and end:
            return jql_error("Error in the JQL Query: Expecting a field name at the end of the query.")
        if what == "a value" and end:
            return jql_error(
                "Error in JQL Query: Expecting either a value, list or function before the end of the query."
            )
        if what == "a value" and token.kind is TokenKind.OP:
            return jql_error(
                f"Error in JQL Query: Expecting either a value, list or function but got '{token.text}'. You must "
                f"surround '{token.text}' in quotation marks to use it as a value. {where}"
            )
        if what == "AND, OR or ORDER BY" and not end:
            return jql_error(f"Error in the JQL Query: Expecting either 'OR' or 'AND' but got '{token.text}'. {where}")
        if what == "')'" and end:
            return jql_error("Error in the JQL Query: Expecting ')' before the end of the query.")
        return jql_error(SEARCH_INVALID)


_RESERVED = {"and", "or", "not", "in", "is", "order", "by", "empty", "null"}


def parse(text: str) -> Query:
    reader = _Reader(text, tokens(text))
    where: Node | None = None
    if reader.peek().kind is not TokenKind.END and not _at_order(reader):
        where = _or(reader)
    order: list[Sort] = []
    if reader.keyword("order", "by"):
        while True:
            field = _field(reader)
            ascending = True
            if reader.keyword("desc"):
                ascending = False
            else:
                reader.keyword("asc")
            order.append(Sort(field=field, ascending=ascending))
            if reader.peek().kind is not TokenKind.COMMA:
                break
            reader.take()
    end = reader.take()
    if end.kind is not TokenKind.END:
        raise reader.expected("AND, OR or ORDER BY", end)
    return Query(where=where, order=order)


def _at_order(reader: _Reader) -> bool:
    token = reader.peek()
    return token.kind is TokenKind.WORD and token.text.lower() == "order"


def _or(reader: _Reader) -> Node:
    parts = [_and(reader)]
    while reader.keyword("or"):
        parts.append(_and(reader))
    return parts[0] if len(parts) == 1 else Or(parts=parts)


def _and(reader: _Reader) -> Node:
    parts = [_not(reader)]
    while reader.keyword("and"):
        parts.append(_not(reader))
    return parts[0] if len(parts) == 1 else And(parts=parts)


def _not(reader: _Reader) -> Node:
    if reader.keyword("not"):
        return Not(operand=_not(reader))
    if reader.peek().kind is TokenKind.OPEN:
        reader.take()
        inner = _or(reader)
        reader.expect(TokenKind.CLOSE, "')'")
        return inner
    return _clause(reader)


def _field(reader: _Reader) -> str:
    token = reader.take()
    if token.kind is TokenKind.STRING:
        return token.text
    if token.kind is not TokenKind.WORD or token.text.lower() in _RESERVED:
        raise reader.expected("a field name", token)
    return token.text


def _clause(reader: _Reader) -> Clause:
    field = _field(reader)
    op = _operator(reader)
    if op in (Op.IN, Op.NOT_IN) and reader.peek().kind is TokenKind.WORD:
        return Clause(field=field, op=op, values=[_value(reader)])
    if op in (Op.IN, Op.NOT_IN):
        reader.expect(TokenKind.OPEN, "'(' to start a list")
        values = [_value(reader)]
        while reader.peek().kind is TokenKind.COMMA:
            reader.take()
            values.append(_value(reader))
        reader.expect(TokenKind.CLOSE, "')' to end the list")
        return Clause(field=field, op=op, values=values)
    value = _value(reader)
    if op in (Op.IS, Op.IS_NOT) and value.kind is not ValueKind.EMPTY:
        raise Unmatched(f"{field} {op.value} takes only EMPTY or NULL")
    return Clause(field=field, op=op, values=[value])


def _operator(reader: _Reader) -> Op:
    token = reader.peek()
    if token.kind is TokenKind.OP:
        reader.take()
        return Op(token.text)
    if reader.keyword("not", "in"):
        return Op.NOT_IN
    if reader.keyword("in"):
        return Op.IN
    if reader.keyword("is", "not"):
        return Op.IS_NOT
    if reader.keyword("is"):
        return Op.IS
    if token.kind is TokenKind.WORD and token.text.lower() in ("was", "changed"):
        raise NotImplementedError(f"the JQL operator {token.text.upper()}: history is not searched here")
    raise reader.expected("an operator", token)


def _value(reader: _Reader) -> Value:
    token = reader.take()
    if token.kind is TokenKind.STRING:
        return Value(kind=ValueKind.TEXT, text=token.text, quoted=True)
    if token.kind is not TokenKind.WORD or token.text.lower() in _RESERVED - {"empty", "null"}:
        raise reader.expected("a value", token)
    if token.text.lower() in ("empty", "null"):
        return Value(kind=ValueKind.EMPTY)
    if reader.peek().kind is TokenKind.OPEN:
        reader.take()
        args: list[str] = []
        if reader.peek().kind is not TokenKind.CLOSE:
            while True:
                arg = reader.take()
                if arg.kind not in (TokenKind.WORD, TokenKind.STRING):
                    raise reader.expected("a function argument", arg)
                args.append(arg.text)
                if reader.peek().kind is not TokenKind.COMMA:
                    break
                reader.take()
        reader.expect(TokenKind.CLOSE, "')' to close the function's arguments")
        return Value(kind=ValueKind.FUNCTION, text=token.text, args=args)
    return Value(kind=ValueKind.TEXT, text=token.text)
