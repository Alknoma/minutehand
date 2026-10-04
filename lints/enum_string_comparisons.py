"""Compare against an enum MEMBER, never against its value spelled out.

`finding.kind == "fail"` type-checks (a StrEnum IS a str) and is wrong in the one
way that never announces itself: misspell the value, or rename the member, and
every branch answers False in silence. Here that means a failed run exits 0.

Two detections, because a vocabulary alone cannot see the typo:

1. VALUE — the literal is the value of a StrEnum in the tree (`== "fail"` while
   `FindingKind.FAIL = "fail"` exists). Ported from alknoma-cloud: the vocabulary
   is derived from every `class X(StrEnum)` / `class X(str, Enum)` under the root,
   and the finding names the member to use. A file that defines an enum may
   compare against its own values (validators, `_missing_`). Narrowed from the
   parent: an expression DECLARED to hold any string (`name: str`, a field that is
   `str` on every model declaring it) is not judged, so `person.name == "agent"`
   is a name, not `Actor.AGENT`.

2. TYPED — the compared expression's declared type is closed and the literal is
   not in it (`kind == "fial"`). The declared type is read from annotations, not
   inferred: a name annotated in the enclosing function (parameter or
   `x: FindingKind = ...`), or an attribute whose every class-level declaration
   in the tree is a StrEnum or a `Literal[...]` (`kind` is `FindingKind` on one
   model and `Literal["ticket"]` on another, so `.kind` is closed over both).
   An attribute declared `str` anywhere is open and is never judged.

Both read `==`, `!=`, `in`, `not in` (with tuple/list/set operands) and `match`
`case "..."` patterns. The left side must be a name or attribute: a subscript
(`payload["type"] == "message"`) is someone else's JSON, which this repo parses
only inside a provider, and is not ours to judge.

Fail-closed. The only way past is `# enum-lint: exempt <reason naming the real
vocabulary>` on the line, for a literal that belongs to someone else's wire
format or to a vocabulary of ours that merely spells a word the same way.

Run: python -m lints.enum_string_comparisons [root]
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from lints._core import SRC, Finding, Source, cli, exempt, sources

NAME = "enum"
TITLE = "Enum comparisons"
GUIDANCE = (
    "Name the MEMBER, not the value. If the literal is someone else's vocabulary,\n"
    "say so on the line: `# enum-lint: exempt <reason naming the real vocabulary>`."
)


@dataclass(frozen=True)
class Offence:
    """The lint's own record: the literal, and what it should have named."""

    path: str
    line: int
    literal: str
    should: str

    def finding(self) -> Finding:
        return Finding(self.path, self.line, f'"{self.literal}" {self.should}')


@dataclass(frozen=True)
class Declared:
    """What the annotations say an expression holds.

    `closed` is every string it can hold, when every declaration is a StrEnum or a
    Literal. `open` is True when every declaration admits any string (`str`, a model):
    then a literal compared to it is a string, not a member, and is not judged.
    Neither is a mix, or no declaration at all: nothing is known.
    """

    closed: frozenset[str] | None = None
    open: bool = False


UNKNOWN = Declared()


@dataclass
class Vocabulary:
    enums: dict[str, dict[str, str]]
    """Enum class -> value -> member name."""
    attributes: dict[str, Declared]
    """Attribute name -> what its class-level declarations across the tree admit."""

    def members_valued(self, literal: str) -> list[str]:
        return sorted(
            f"{enum}.{member}"
            for enum, values in self.enums.items()
            for value, member in values.items()
            if value == literal
        )


def _base_names(node: ast.ClassDef) -> set[str]:
    return {b.id for b in node.bases if isinstance(b, ast.Name)} | {
        b.attr for b in node.bases if isinstance(b, ast.Attribute)
    }


def _is_str_enum(node: ast.ClassDef) -> bool:
    bases = _base_names(node)
    return "StrEnum" in bases or {"str", "Enum"} <= bases


def _enum_values(node: ast.ClassDef) -> dict[str, str]:
    values: dict[str, str] = {}
    for stmt in node.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            values[stmt.value.value] = stmt.targets[0].id
    return values


def _closed_values(annotation: ast.expr | None, enums: dict[str, dict[str, str]]) -> set[str] | None:
    """Every string an annotation admits, or None when it admits strings it does not list.

    `X | None` and `Optional[X]` admit X's values; `Literal["a", "b"]` admits a and b;
    a StrEnum admits its values. Anything else (str, a model, Any) is open.
    """
    if annotation is None:
        return None
    if isinstance(annotation, ast.Constant):
        if annotation.value is None:
            return set()
        if isinstance(annotation.value, str):
            try:
                return _closed_values(ast.parse(annotation.value, mode="eval").body, enums)
            except SyntaxError:
                return None
        return None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left = _closed_values(annotation.left, enums)
        right = _closed_values(annotation.right, enums)
        return None if left is None or right is None else left | right
    if isinstance(annotation, ast.Subscript):
        head = _name(annotation.value)
        args = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
        if head == "Literal":
            if all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args):
                return {a.value for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
            return None
        if head in ("Optional", "Union"):
            out: set[str] = set()
            for arg in args:
                values = _closed_values(arg, enums)
                if values is None:
                    return None
                out |= values
            return out
        return None
    name = _name(annotation)
    if name is not None and name in enums:
        return set(enums[name])
    return None


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _vocabulary(parsed: list[Source]) -> Vocabulary:
    enums: dict[str, dict[str, str]] = {}
    for source in parsed:
        for node in ast.walk(source.tree):
            if isinstance(node, ast.ClassDef) and _is_str_enum(node):
                enums.setdefault(node.name, {}).update(_enum_values(node))
    declared: dict[str, list[set[str] | None]] = defaultdict(list)
    for source in parsed:
        for node in ast.walk(source.tree):
            if not isinstance(node, ast.ClassDef) or _is_str_enum(node):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    declared[stmt.target.id].append(_closed_values(stmt.annotation, enums))
    attributes: dict[str, Declared] = {}
    for attr, kinds in declared.items():
        closed = [k for k in kinds if k is not None]
        if len(closed) == len(kinds):
            attributes[attr] = Declared(closed=frozenset().union(*closed))
        elif not closed:
            attributes[attr] = Declared(open=True)
        else:
            attributes[attr] = UNKNOWN
    return Vocabulary(enums=enums, attributes=attributes)


def _declared_by(annotation: ast.expr | None, enums: dict[str, dict[str, str]]) -> Declared:
    if annotation is None:
        return UNKNOWN
    closed = _closed_values(annotation, enums)
    return Declared(open=True) if closed is None else Declared(closed=frozenset(closed))


def _string_literals(node: ast.expr) -> Iterator[ast.Constant]:
    candidates = node.elts if isinstance(node, (ast.Tuple, ast.List, ast.Set)) else [node]
    for candidate in candidates:
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            yield candidate


def _comparisons(tree: ast.Module) -> Iterator[tuple[ast.expr, ast.Constant, ast.AST]]:
    """(compared expression, literal, function it sits in) for every comparison to a string literal."""

    def visit(node: ast.AST, scope: ast.AST) -> Iterator[tuple[ast.expr, ast.Constant, ast.AST]]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            scope = node
        if isinstance(node, ast.Compare) and isinstance(node.left, (ast.Name, ast.Attribute)):
            for op, comparator in zip(node.ops, node.comparators, strict=False):
                if isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                    for literal in _string_literals(comparator):
                        yield node.left, literal, scope
        if isinstance(node, ast.Match) and isinstance(node.subject, (ast.Name, ast.Attribute)):
            for case in node.cases:
                for pattern in ast.walk(case.pattern):
                    if isinstance(pattern, ast.MatchValue):
                        for literal in _string_literals(pattern.value):
                            yield node.subject, literal, scope
        for child in ast.iter_child_nodes(node):
            yield from visit(child, scope)

    yield from visit(tree, tree)


def _local_annotations(scope: ast.AST) -> dict[str, ast.expr]:
    """name -> annotation, for the parameters and annotated assignments of one function."""
    found: dict[str, ast.expr] = {}
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = scope.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            if arg.annotation is not None:
                found[arg.arg] = arg.annotation
        for node in ast.walk(scope):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found[node.target.id] = node.annotation
    return found


def _scan(source: Source, vocabulary: Vocabulary) -> list[Offence]:
    own = {
        value
        for node in ast.walk(source.tree)
        if isinstance(node, ast.ClassDef) and _is_str_enum(node)
        for value in _enum_values(node)
    }
    annotations: dict[int, dict[str, ast.expr]] = {}
    offences: list[Offence] = []
    for subject, literal, scope in _comparisons(source.tree):
        value = literal.value
        assert isinstance(value, str)
        if isinstance(subject, ast.Name):
            local = annotations.setdefault(id(scope), _local_annotations(scope))
            declared = _declared_by(local.get(subject.id), vocabulary.enums)
        else:
            assert isinstance(subject, ast.Attribute)
            declared = vocabulary.attributes.get(subject.attr, UNKNOWN)
        should: str | None = None
        members = vocabulary.members_valued(value)
        if members and value not in own and not declared.open:
            should = f"is a member's value spelled out; use {' | '.join(members)}"
        elif declared.closed is not None and value not in declared.closed:
            legal = sorted(declared.closed)
            shown = ", ".join(legal[:6]) + (f", … {len(legal) - 6} more" if len(legal) > 6 else "")
            should = (
                f"is not a value `{ast.unparse(subject)}` can hold (it holds {shown or 'nothing'}); "
                "this compares False always"
            )
        if should is None:
            continue
        if exempt(source.lines, NAME, literal.lineno):
            continue
        offences.append(Offence(source.rel, literal.lineno, value, should))
    return offences


def run(root: Path = SRC) -> list[Finding]:
    """Derive the vocabulary from every StrEnum and class annotation under root, then check every comparison."""
    parsed = list(sources(root))
    vocabulary = _vocabulary(parsed)
    return [offence.finding() for source in parsed for offence in _scan(source, vocabulary)]


if __name__ == "__main__":
    sys.exit(cli(TITLE, run, sys.argv[1:], guidance=GUIDANCE))
