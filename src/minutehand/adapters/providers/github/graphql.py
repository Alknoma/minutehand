"""GitHub's GraphQL API, for the part of its schema a code reader queries.

A query is parsed (operations, variables with defaults, aliases, arguments, inline fragments), validated against
the schema below before anything runs, as GitHub validates it, and then executed. A field the schema does not
hold is refused the way GitHub refuses one, with `undefinedField`, and no `data`.

The schema answered, from `Query.viewer` (the token's user: `login`, `name`) and `Query.repository(owner:, name:)`:

    Repository   id name nameWithOwner description homepageUrl url isPrivate stargazerCount forkCount
                 primaryLanguage languages(first|last) repositoryTopics(first|last) defaultBranchRef licenseInfo
                 object(expression:) refs(refPrefix:, first|last, orderBy, query)
    Blob         oid abbreviatedOid byteSize isBinary isTruncated text
    Tree         oid abbreviatedOid entries { name path type oid }
    Commit       oid abbreviatedOid message messageHeadline committedDate
    Ref          name prefix target
    Language     name          Topic   name          License   key name spdxId

Not answered: named fragments, directives, mutations and subscriptions, and every other type.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import JsonValue

from minutehand.adapters.providers.github import content, wire

# --------------------------------------------------------------------------- the language


class ParseError(Exception):
    def __init__(self, message: str, line: int, column: int) -> None:
        super().__init__(message)
        self.line = line
        self.column = column


class Lexeme(StrEnum):
    """The kinds of token, named as the lexer's groups are."""

    IGNORED = "ignored"
    SPREAD = "spread"
    PUNCT = "punct"
    NAME = "name"
    FLOAT = "float"
    INT = "int"
    STRING = "string"
    END = "end"


@dataclass(frozen=True)
class Token:
    kind: Lexeme
    text: str
    line: int
    column: int


_LEXEME = re.compile(
    r"(?P<ignored>[\s,﻿]+|#[^\n\r]*)"
    r"|(?P<spread>\.\.\.)"
    r"|(?P<punct>[!$()\:=@\[\]{|}])"
    r"|(?P<name>[_A-Za-z][_0-9A-Za-z]*)"
    r"|(?P<float>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+))"
    r"|(?P<int>-?(?:0|[1-9][0-9]*))"
    r'|(?P<string>"(?:[^"\\\n\r]|\\.)*")'
)


def _lex(source: str) -> list[Token]:
    found: list[Token] = []
    position, line, line_start = 0, 1, 0
    while position < len(source):
        match = _LEXEME.match(source, position)
        if match is None:
            raise ParseError(
                f'Parse error on "{source[position]}" at [{line}, {position - line_start + 1}]',
                line,
                position - line_start + 1,
            )
        assert match.lastgroup is not None
        kind = Lexeme(match.lastgroup)
        text = match.group(0)
        if kind is not Lexeme.IGNORED:
            found.append(Token(kind, text, line, position - line_start + 1))
        for offset, char in enumerate(text):
            if char == "\n":
                line, line_start = line + 1, position + offset + 1
        position = match.end()
    found.append(Token(Lexeme.END, "", line, position - line_start + 1))
    return found


@dataclass(frozen=True)
class Variable:
    name: str


@dataclass(frozen=True)
class Enum:
    name: str


Value = Variable | Enum | str | int | float | bool | None | list["Value"] | dict[str, "Value"]


@dataclass
class Field:
    name: str
    alias: str | None
    arguments: dict[str, Value]
    selections: list[Selection] | None
    line: int
    column: int

    @property
    def key(self) -> str:
        return self.alias or self.name


@dataclass
class Fragment:
    on: str
    selections: list[Selection]
    line: int
    column: int


Selection = Field | Fragment


@dataclass(frozen=True)
class Declared:
    name: str
    required: bool
    default: Value
    has_default: bool


@dataclass
class Operation:
    kind: str
    declared: list[Declared]
    selections: list[Selection]


class _Parser:
    def __init__(self, source: str) -> None:
        self._tokens = _lex(source)
        self._at = 0

    @property
    def _peek(self) -> Token:
        return self._tokens[self._at]

    def _next(self) -> Token:
        token = self._tokens[self._at]
        self._at += 1
        return token

    def _fail(self, token: Token) -> ParseError:
        shown = token.text or "end of file"
        return ParseError(f'Parse error on "{shown}" at [{token.line}, {token.column}]', token.line, token.column)

    def _expect(self, text: str) -> Token:
        token = self._next()
        if token.text != text or token.kind is Lexeme.STRING:
            raise self._fail(token)
        return token

    def _is(self, text: str) -> bool:
        return self._peek.text == text and self._peek.kind in (Lexeme.PUNCT, Lexeme.SPREAD, Lexeme.NAME)

    def _name(self) -> Token:
        token = self._next()
        if token.kind is not Lexeme.NAME:
            raise self._fail(token)
        return token

    def document(self) -> Operation:
        start = self._peek
        if self._is("{"):
            operation = Operation("query", [], self._selections())
        elif start.kind is Lexeme.NAME and start.text in ("query", "mutation", "subscription"):
            self._next()
            if self._peek.kind is Lexeme.NAME:
                self._next()
            declared = self._declarations() if self._is("(") else []
            if self._is("@"):
                raise ParseError("Directives are not answered by this GitHub", self._peek.line, self._peek.column)
            operation = Operation(start.text, declared, self._selections())
        elif start.kind is Lexeme.NAME and start.text == "fragment":
            raise ParseError("Named fragments are not answered by this GitHub", start.line, start.column)
        else:
            raise self._fail(start)
        if self._peek.kind is not Lexeme.END:
            raise ParseError("This GitHub answers one operation per document", self._peek.line, self._peek.column)
        return operation

    def _declarations(self) -> list[Declared]:
        self._expect("(")
        found: list[Declared] = []
        while not self._is(")"):
            self._expect("$")
            name = self._name().text
            self._expect(":")
            required = self._type()
            has_default = self._is("=")
            default: Value = None
            if has_default:
                self._next()
                default = self._value(constant=True)
            found.append(Declared(name, required, default, has_default))
        self._expect(")")
        return found

    def _type(self) -> bool:
        if self._is("["):
            self._next()
            self._type()
            self._expect("]")
        else:
            self._name()
        if self._is("!"):
            self._next()
            return True
        return False

    def _selections(self) -> list[Selection]:
        self._expect("{")
        found: list[Selection] = []
        while not self._is("}"):
            found.append(self._selection())
        self._expect("}")
        if not found:
            raise self._fail(self._tokens[self._at - 1])
        return found

    def _selection(self) -> Selection:
        start = self._peek
        if start.kind is Lexeme.SPREAD:
            self._next()
            if self._peek.kind is Lexeme.NAME and self._peek.text == "on":
                self._next()
                on = self._name().text
                return Fragment(on, self._selections(), start.line, start.column)
            raise ParseError("Named fragments are not answered by this GitHub", start.line, start.column)
        first = self._name()
        alias, name = None, first
        if self._is(":"):
            self._next()
            alias, name = first.text, self._name()
        arguments: dict[str, Value] = {}
        if self._is("("):
            self._next()
            while not self._is(")"):
                argument = self._name().text
                self._expect(":")
                arguments[argument] = self._value(constant=False)
            self._expect(")")
        if self._is("@"):
            raise ParseError("Directives are not answered by this GitHub", self._peek.line, self._peek.column)
        selections = self._selections() if self._is("{") else None
        return Field(name.text, alias, arguments, selections, name.line, name.column)

    def _value(self, *, constant: bool) -> Value:
        token = self._next()
        if token.kind is Lexeme.PUNCT and token.text == "$" and not constant:
            return Variable(self._name().text)
        if token.kind is Lexeme.INT:
            return int(token.text)
        if token.kind is Lexeme.FLOAT:
            return float(token.text)
        if token.kind is Lexeme.STRING:
            decoded: object = json.loads(token.text)
            assert isinstance(decoded, str)
            return decoded
        if token.kind is Lexeme.NAME:
            return {"true": True, "false": False, "null": None}.get(token.text, Enum(token.text))
        if token.text == "[":
            items: list[Value] = []
            while not self._is("]"):
                items.append(self._value(constant=constant))
            self._next()
            return items
        if token.text == "{":
            fields: dict[str, Value] = {}
            while not self._is("}"):
                key = self._name().text
                self._expect(":")
                fields[key] = self._value(constant=constant)
            self._next()
            return fields
        raise self._fail(token)


def parse(source: str) -> Operation:
    return _Parser(source).document()


# --------------------------------------------------------------------------- the schema


@dataclass(frozen=True)
class FieldType:
    returns: str | None
    """The object type it returns; None for a scalar."""
    arguments: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()


def _f(returns: str | None = None, *arguments: str, required: tuple[str, ...] = ()) -> FieldType:
    return FieldType(returns, frozenset(arguments) | frozenset(required), frozenset(required))


_CONNECTION = ("first", "last", "after", "before")
_GIT_OBJECT = {"oid": _f(), "abbreviatedOid": _f()}

SCHEMA: dict[str, dict[str, FieldType]] = {
    "Query": {
        "repository": _f("Repository", "followRenames", required=("owner", "name")),
        "viewer": _f("User"),
    },
    "User": {"login": _f(), "name": _f()},
    "Repository": {
        "id": _f(),
        "name": _f(),
        "nameWithOwner": _f(),
        "description": _f(),
        "homepageUrl": _f(),
        "url": _f(),
        "isPrivate": _f(),
        "stargazerCount": _f(),
        "forkCount": _f(),
        "primaryLanguage": _f("Language"),
        "languages": _f("LanguageConnection", "orderBy", *_CONNECTION),
        "repositoryTopics": _f("RepositoryTopicConnection", *_CONNECTION),
        "defaultBranchRef": _f("Ref"),
        "licenseInfo": _f("License"),
        "object": _f("GitObject", "expression", "oid"),
        "refs": _f("RefConnection", "orderBy", "query", "direction", *_CONNECTION, required=("refPrefix",)),
    },
    "Language": {"name": _f(), "color": _f()},
    "LanguageConnection": {"nodes": _f("Language"), "totalCount": _f(), "totalSize": _f()},
    "RepositoryTopicConnection": {"nodes": _f("RepositoryTopic"), "totalCount": _f()},
    "RepositoryTopic": {"topic": _f("Topic")},
    "Topic": {"name": _f()},
    "License": {"key": _f(), "name": _f(), "spdxId": _f()},
    "RefConnection": {"nodes": _f("Ref"), "totalCount": _f()},
    "Ref": {"name": _f(), "prefix": _f(), "target": _f("GitObject")},
    "GitObject": dict(_GIT_OBJECT),
    "Blob": {**_GIT_OBJECT, "byteSize": _f(), "isBinary": _f(), "isTruncated": _f(), "text": _f()},
    "Tree": {**_GIT_OBJECT, "entries": _f("TreeEntry")},
    "TreeEntry": {"name": _f(), "path": _f(), "type": _f(), "oid": _f()},
    "Commit": {**_GIT_OBJECT, "message": _f(), "messageHeadline": _f(), "committedDate": _f()},
}
POSSIBLE: dict[str, set[str]] = {"GitObject": {"Blob", "Tree", "Commit"}}
PAGE_LIMIT = 100


def _where(at: Field | Fragment) -> list[wire.GraphErrorLocation]:
    return [wire.GraphErrorLocation(line=at.line, column=at.column)]


def _validate(selections: list[Selection], on: str, path: list[str], errors: list[wire.GraphError]) -> None:
    for selection in selections:
        if isinstance(selection, Fragment):
            if selection.on != on and selection.on not in POSSIBLE.get(on, set()):
                errors.append(
                    wire.GraphError(
                        path=path,
                        locations=_where(selection),
                        extensions={"code": "fragmentSpreadImpossible", "fragmentName": "...", "typeName": on},
                        message=f"Fragment on {selection.on} can't be spread inside {on}",
                    )
                )
                continue
            _validate(selection.selections, selection.on, path, errors)
            continue
        here = [*path, selection.key]
        if selection.name == "__typename":
            continue
        fields = SCHEMA[on]
        if selection.name not in fields:
            errors.append(
                wire.GraphError(
                    path=here,
                    locations=_where(selection),
                    extensions={"code": "undefinedField", "typeName": on, "fieldName": selection.name},
                    message=f"Field '{selection.name}' doesn't exist on type '{on}'",
                )
            )
            continue
        declared = fields[selection.name]
        for argument in selection.arguments:
            if argument not in declared.arguments:
                errors.append(
                    wire.GraphError(
                        path=here,
                        locations=_where(selection),
                        extensions={"code": "argumentNotAccepted", "name": selection.name, "argumentName": argument},
                        message=f"Field '{selection.name}' doesn't accept argument '{argument}'",
                    )
                )
        missing = sorted(declared.required - set(selection.arguments))
        if missing:
            errors.append(
                wire.GraphError(
                    path=here,
                    locations=_where(selection),
                    extensions={"code": "missingRequiredArguments", "name": selection.name},
                    message=f"Field '{selection.name}' is missing required arguments: {', '.join(missing)}",
                )
            )
        if declared.returns is None and selection.selections is not None:
            errors.append(
                wire.GraphError(
                    path=here,
                    locations=_where(selection),
                    extensions={"code": "selectionMismatch", "nodeName": f"field '{selection.name}'"},
                    message=f"Selections can't be made on scalars (field '{selection.name}' has selections)",
                )
            )
        elif declared.returns is not None and selection.selections is None:
            errors.append(
                wire.GraphError(
                    path=here,
                    locations=_where(selection),
                    extensions={"code": "selectionMismatch", "nodeName": f"field '{selection.name}'"},
                    message=f"Field must have selections (field '{selection.name}' returns {declared.returns} "
                    "but has no selections)",
                )
            )
        elif declared.returns is not None and selection.selections is not None:
            _validate(selection.selections, declared.returns, here, errors)


def _used(value: Value, into: set[str]) -> None:
    if isinstance(value, Variable):
        into.add(value.name)
    elif isinstance(value, list):
        for item in value:
            _used(item, into)
    elif isinstance(value, dict):
        for item in value.values():
            _used(item, into)


def _variables_used(selections: list[Selection], into: set[str]) -> None:
    for selection in selections:
        if isinstance(selection, Field):
            for value in selection.arguments.values():
                _used(value, into)
        if selection.selections is not None:
            _variables_used(selection.selections, into)


# --------------------------------------------------------------------------- execution


@dataclass(frozen=True)
class Visible:
    """A repository the caller may read, with its files read only if a field needs them."""

    repository: wire.StoredRepository
    files: Callable[[], list[wire.StoredFile]]


Finder = Callable[[str, str], Visible | None]
"""Owner and name to the repository, or None when it does not exist or the caller may not see it."""


@dataclass(frozen=True)
class _Blob:
    file: wire.StoredFile


@dataclass(frozen=True)
class _Tree:
    path: str


@dataclass(frozen=True)
class _Ref:
    name: str


GitObject = _Blob | _Tree | wire.StoredCommit


@dataclass
class _Run:
    variables: dict[str, JsonValue]
    find: Finder
    viewer: wire.StoredAccount
    errors: list[wire.GraphError] = field(default_factory=list)
    seen: list[wire.StoredRepository] = field(default_factory=list)

    def argument(self, at: Field, name: str) -> JsonValue:
        return self._value(at.arguments[name]) if name in at.arguments else None

    def _value(self, value: Value) -> JsonValue:
        if isinstance(value, Variable):
            return self.variables[value.name] if value.name in self.variables else None
        if isinstance(value, Enum):
            return value.name
        if isinstance(value, list):
            return [self._value(v) for v in value]
        if isinstance(value, dict):
            return {k: self._value(v) for k, v in value.items()}
        return value

    def select(self, selections: list[Selection], on: str, resolve: Callable[[Field], JsonValue]) -> JsonValue:
        answer: dict[str, JsonValue] = {}
        for selection in selections:
            if isinstance(selection, Fragment):
                if selection.on == on:
                    nested = self.select(selection.selections, on, resolve)
                    assert isinstance(nested, dict)
                    answer.update(nested)
                continue
            answer[selection.key] = on if selection.name == "__typename" else resolve(selection)
        return answer

    def page(self, at: Field, path: list[str], connection: str) -> int | None:
        """How many nodes a connection may answer, or None after recording why it answers none."""
        first, last = self.argument(at, "first"), self.argument(at, "last")
        wanted = first if first is not None else last
        if wanted is None:
            self.errors.append(
                wire.GraphError(
                    type="MISSING_PAGINATION_BOUNDARIES",
                    path=[*path, at.key],
                    locations=_where(at),
                    message=f"You must provide a `first` or `last` value to properly paginate the `{connection}` "
                    "connection.",
                )
            )
            return None
        if not isinstance(wanted, int) or wanted < 0 or wanted > PAGE_LIMIT:
            self.errors.append(
                wire.GraphError(
                    type="EXCESSIVE_PAGINATION",
                    path=[*path, at.key],
                    locations=_where(at),
                    message=f"Requesting {wanted} records on the `{connection}` connection exceeds the limit of "
                    f"{PAGE_LIMIT} records.",
                )
            )
            return None
        return wanted

    # ------------------------------------------------------------------ types

    def query(self, selections: list[Selection]) -> JsonValue:
        return self.select(selections, "Query", self._query_field)

    def _query_field(self, at: Field) -> JsonValue:
        if at.name == "viewer":
            assert at.selections is not None
            return self.select(at.selections, "User", self._viewer)
        owner, name = self.argument(at, "owner"), self.argument(at, "name")
        found = self.find(owner, name) if isinstance(owner, str) and isinstance(name, str) else None
        if found is None:
            self.errors.append(
                wire.GraphError(
                    type="NOT_FOUND",
                    path=[at.key],
                    locations=_where(at),
                    message=f"Could not resolve to a Repository with the name '{owner}/{name}'.",
                )
            )
            return None
        self.seen.append(found.repository)
        assert at.selections is not None
        return self.select(at.selections, "Repository", lambda f: self._repository(found, f, [at.key]))

    def _viewer(self, at: Field) -> JsonValue:
        if at.name == "login":
            return self.viewer.login
        return self.viewer.name

    def _repository(self, visible: Visible, at: Field, path: list[str]) -> JsonValue:
        repository = visible.repository
        nested = at.selections or []
        name = at.name
        if name == "id":  # enum-lint: exempt GraphQL's own field name, not a seed fact
            return wire.node_id("R", repository.id)
        if name == "name":  # enum-lint: exempt a field of GitHub's GraphQL schema
            return repository.name
        if name == "nameWithOwner":
            return repository.full_name
        if name == "description":  # enum-lint: exempt a GitHub GraphQL Repository field name, its wire vocabulary
            return repository.description
        if name == "homepageUrl":
            return repository.homepage
        if name == "url":  # enum-lint: exempt a GraphQL field name
            return f"{wire.WEB}/{repository.full_name}"
        if name == "isPrivate":
            return repository.private
        if name == "stargazerCount":
            return repository.stargazers_count
        if name == "forkCount":
            return repository.forks_count
        if name == "primaryLanguage":
            languages = content.breakdown(visible.files())
            if not languages:
                return None
            return self.select(nested, "Language", lambda f: self._language(languages[0][0], f))
        if name == "languages":
            wanted = self.page(at, path, "languages")
            if wanted is None:
                return None
            languages = content.breakdown(visible.files())
            return self.select(nested, "LanguageConnection", lambda f: self._languages(languages, wanted, f))
        if name == "repositoryTopics":
            wanted = self.page(at, path, "repositoryTopics")
            if wanted is None:
                return None
            return self.select(
                nested, "RepositoryTopicConnection", lambda f: self._topics(repository.topics, wanted, f)
            )
        if name == "defaultBranchRef":
            if not repository.commits:
                return None
            return self.select(nested, "Ref", lambda f: self._ref(visible, _Ref(repository.default_branch), f))
        if name == "licenseInfo":
            license = repository.license
            if license is None:
                return None
            return self.select(nested, "License", lambda f: _license(license, f))
        if name == "object":
            found = _object(visible, self.argument(at, "expression"))
            return None if found is None else self._git_object(visible, found, nested)
        wanted = self.page(at, path, "refs")
        if wanted is None:
            return None
        prefix = self.argument(at, "refPrefix")
        names = [repository.default_branch, *repository.branches] if repository.commits else []
        refs = sorted(names) if prefix == content.BRANCH_PREFIX else []
        return self.select(nested, "RefConnection", lambda f: self._refs(visible, refs, wanted, f))

    def _language(self, language: str, at: Field) -> JsonValue:
        return language if at.name == "name" else None

    def _languages(self, languages: list[tuple[str, int]], wanted: int, at: Field) -> JsonValue:
        if at.name == "totalCount":
            return len(languages)
        if at.name == "totalSize":
            return sum(size for _, size in languages)
        nested = at.selections or []
        return [self.select(nested, "Language", lambda f, n=n: self._language(n, f)) for n, _ in languages[:wanted]]

    def _topics(self, topics: list[str], wanted: int, at: Field) -> JsonValue:
        if at.name == "totalCount":
            return len(topics)
        nested = at.selections or []
        return [self.select(nested, "RepositoryTopic", lambda f, t=t: self._topic(t, f)) for t in topics[:wanted]]

    def _topic(self, topic: str, at: Field) -> JsonValue:
        nested = at.selections or []
        return self.select(nested, "Topic", lambda f: topic if f.name == "name" else None)

    def _refs(self, visible: Visible, refs: list[str], wanted: int, at: Field) -> JsonValue:
        if at.name == "totalCount":
            return len(refs)
        nested = at.selections or []
        return [self.select(nested, "Ref", lambda f, r=r: self._ref(visible, _Ref(r), f)) for r in refs[:wanted]]

    def _ref(self, visible: Visible, ref: _Ref, at: Field) -> JsonValue:
        if at.name == "name":
            return ref.name
        if at.name == "prefix":
            return content.BRANCH_PREFIX
        return self._git_object(visible, visible.repository.commits[0], at.selections or [])

    def _git_object(self, visible: Visible, found: GitObject, nested: list[Selection]) -> JsonValue:
        if isinstance(found, _Blob):
            return self.select(nested, "Blob", lambda f: _blob(found.file, f))
        if isinstance(found, _Tree):
            return self.select(nested, "Tree", lambda f: self._tree(visible, found, f))
        return self.select(nested, "Commit", lambda f: _commit(found, f))

    def _tree(self, visible: Visible, tree: _Tree, at: Field) -> JsonValue:
        if at.name in ("oid", "abbreviatedOid"):
            oid = content.tree_sha(visible.repository, tree.path)
            return oid if at.name == "oid" else oid[:7]
        nested = at.selections or []
        entries = content.children(visible.files(), tree.path)
        return [self.select(nested, "TreeEntry", lambda f, e=e: _entry(visible.repository, e, f)) for e in entries]


def _license(license: wire.License, at: Field) -> JsonValue:
    return {"key": license.key, "name": license.name, "spdxId": license.spdx_id}[at.name]


def _blob(file: wire.StoredFile, at: Field) -> JsonValue:
    raw = _raw(file)
    binary = content.is_binary(raw)
    if at.name == "oid":
        return file.sha
    if at.name == "abbreviatedOid":
        return file.sha[:7]
    if at.name == "byteSize":
        return file.size
    if at.name == "isBinary":
        return binary
    if at.name == "isTruncated":
        return False
    return None if binary else raw.decode("utf-8")


def _commit(commit: wire.StoredCommit, at: Field) -> JsonValue:
    if at.name == "oid":
        return commit.sha
    if at.name == "abbreviatedOid":
        return commit.sha[:7]
    if at.name == "message":
        return commit.message
    if at.name == "messageHeadline":
        return commit.message.split("\n", 1)[0]
    return commit.date


def _entry(repository: wire.StoredRepository, entry: content.Entry, at: Field) -> JsonValue:
    if at.name == "name":
        return entry.name
    if at.name == "path":
        return entry.path
    if at.name == "type":
        return "tree" if entry.file is None else "blob"
    return content.tree_sha(repository, entry.path) if entry.file is None else entry.file.sha


def _raw(file: wire.StoredFile) -> bytes:
    return base64.b64decode(file.content)


def _object(visible: Visible, expression: JsonValue) -> GitObject | None:
    """`REV:PATH` names a blob or a tree at a revision; a bare `REV` names its commit."""
    if not isinstance(expression, str):
        return None
    revision, colon, path = expression.partition(":")
    commit = content.resolve(visible.repository, revision or None)
    if commit is None:
        return None
    if not colon:
        return commit
    path = path.strip("/")
    files = visible.files()
    if path == "":
        return _Tree("")
    found = next((f for f in files if f.path == path), None)
    if found is not None:
        return _Blob(found)
    if path in content.directories(files):
        return _Tree(path)
    return None


@dataclass(frozen=True)
class Answer:
    body: bytes
    seen: list[wire.StoredRepository]
    """The repositories the query read: what the world records the agent as having looked at."""


def _errors_only(errors: list[wire.GraphError]) -> bytes:
    return wire.GraphErrorsOut(errors=errors).model_dump_json(exclude_none=True).encode()


def execute(request: wire.GraphQLIn, find: Finder, viewer: wire.StoredAccount) -> Answer:
    """GitHub's answer to one query: `errors` alone when it does not parse or validate; else `data`, with
    `errors` beside it for what could not be resolved."""
    try:
        operation = parse(request.query)
    except ParseError as error:
        located = wire.GraphError(
            locations=[wire.GraphErrorLocation(line=error.line, column=error.column)], message=str(error)
        )
        return Answer(_errors_only([located]), [])
    if operation.kind != "query":
        refused = wire.GraphError(message=f"This GitHub answers queries; a {operation.kind} is not answered.")
        return Answer(_errors_only([refused]), [])

    errors: list[wire.GraphError] = []
    _validate(operation.selections, "Query", [], errors)
    used: set[str] = set()
    _variables_used(operation.selections, used)
    declared = {d.name: d for d in operation.declared}
    for name in sorted(used - set(declared)):
        errors.append(
            wire.GraphError(
                extensions={"code": "variableNotDefined", "variableName": name},
                message=f"Variable ${name} is used by anonymous query but not declared",
            )
        )
    given = request.variables or {}
    variables: dict[str, JsonValue] = {}
    for name, declaration in declared.items():
        if name in given and given[name] is not None:
            variables[name] = given[name]
        elif declaration.has_default:
            variables[name] = _Run({}, find, viewer)._value(declaration.default)
        elif declaration.required:
            errors.append(
                wire.GraphError(
                    extensions={"code": "variableMismatch", "variableName": name},
                    message=f"Variable ${name} of type non-null was provided invalid value",
                )
            )
    if errors:
        return Answer(_errors_only(errors), [])

    run = _Run(variables, find, viewer)
    data = run.query(operation.selections)
    answer: dict[str, JsonValue] = {"data": data}
    if run.errors:
        answer["errors"] = [e.model_dump(exclude_none=True) for e in run.errors]
    return Answer(json.dumps(answer, separators=(",", ":")).encode(), run.seen)
