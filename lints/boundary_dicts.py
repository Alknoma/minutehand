"""No `dict[str, Any]`, `Dict[str, Any]` or bare `dict` in a signature or a Pydantic field.

pyright cannot object to these: the annotation SAYS any key is fine, so the type
system invites the `dict.get` typo the norm forbids. The parent repo's version
of this rule went unenforced until it stood at 699 signatures.

What is read: every function signature (each parameter, `*args`, `**kwargs`, the
return) and every class-level field of a Pydantic model — a class whose bases
reach `BaseModel`, `RootModel` or `domain.scenario.Model` through classes in the
tree. Anywhere inside the annotation counts (`list[dict[str, Any]] | None`), and
so does a string annotation. A dict built and used inside one function body is
local reasoning and is not read.

What is flagged: a `dict`/`Dict` whose value type is `Any`, and `dict`/`Dict`
with no parameters at all. `dict[str, str]` says what it holds and is legal.

The one exception is a file named `wire.py` under `adapters/providers/`: a
provider's request and response bodies are someone else's wire format, and that
file is where they are parsed into typed snapshots.

Fail-closed, no baseline: the repo is new and starts at zero. The only way past is
`# dict-lint: exempt <reason>` on a line of the annotation.

Run: python -m lints.boundary_dicts [root]
"""

from __future__ import annotations

import ast
import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path

from lints._core import SRC, Finding, Source, cli, exempt, sources

NAME = "dict"
TITLE = "Boundary dicts"
GUIDANCE = (
    "A dict[str, Any] at a boundary is a payload without a contract. Type it as a Model;\n"
    "a provider's own JSON crosses as text and is parsed in its wire.py."
)

DICTS = {"dict", "Dict"}
PYDANTIC_ROOTS = {"BaseModel", "RootModel", "Model"}


def _head(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _offending(annotation: ast.expr | None) -> Iterator[str]:
    """A description of every untyped dict inside an annotation."""
    if annotation is None:
        return
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        with contextlib.suppress(SyntaxError):
            yield from _offending(ast.parse(annotation.value, mode="eval").body)
        return
    if isinstance(annotation, ast.Subscript):
        args = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
        if _head(annotation.value) in DICTS and len(args) == 2 and _head(args[1]) == "Any":
            yield ast.unparse(annotation)
        for arg in args:
            yield from _offending(arg)
        return
    if isinstance(annotation, ast.BinOp):
        yield from _offending(annotation.left)
        yield from _offending(annotation.right)
        return
    if isinstance(annotation, (ast.Tuple, ast.List)):
        for element in annotation.elts:
            yield from _offending(element)
        return
    if _head(annotation) in DICTS:
        yield ast.unparse(annotation)


def _is_wire(rel: str) -> bool:
    parts = Path(rel).parts
    return parts[-1] == "wire.py" and any(parts[i : i + 2] == ("adapters", "providers") for i in range(len(parts) - 1))


def _pydantic_classes(parsed: list[Source]) -> set[str]:
    """Every class name in the tree whose bases reach a Pydantic root, to a fixed point."""
    bases: dict[str, set[str]] = {}
    for source in parsed:
        for node in ast.walk(source.tree):
            if isinstance(node, ast.ClassDef):
                bases.setdefault(node.name, set()).update(
                    head for head in (_head(b) for b in node.bases) if head is not None
                )
    models = set(PYDANTIC_ROOTS)
    grew = True
    while grew:
        grew = False
        for name, parents in bases.items():
            if name not in models and parents & models:
                models.add(name)
                grew = True
    return models


def _annotations(source: Source, models: set[str]) -> Iterator[tuple[str, ast.expr]]:
    """(where, annotation) for every signature slot and Pydantic field in the file."""
    for node in ast.walk(source.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
                if arg is not None and arg.annotation is not None:
                    yield f"parameter `{arg.arg}` of {node.name}()", arg.annotation
            if node.returns is not None:
                yield f"return of {node.name}()", node.returns
        elif isinstance(node, ast.ClassDef) and {_head(b) for b in node.bases} & models:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    yield f"field `{node.name}.{stmt.target.id}`", stmt.annotation


def run(root: Path = SRC) -> list[Finding]:
    parsed = [source for source in sources(root, containing=("dict", "Dict")) if not _is_wire(source.rel)]
    models = _pydantic_classes(list(sources(root)))
    findings: list[Finding] = []
    for source in parsed:
        for where, annotation in _annotations(source, models):
            for found in _offending(annotation):
                if exempt(source.lines, NAME, annotation.lineno, end_lineno=annotation.end_lineno):
                    continue
                findings.append(
                    Finding(source.rel, annotation.lineno, f"{where} is `{found}`, a dict without a contract")
                )
    return findings


if __name__ == "__main__":
    sys.exit(cli(TITLE, run, sys.argv[1:], guidance=GUIDANCE))
