"""A provider holds nothing between requests: what it knows is in the store, never in a module, a closure or `self`.

The rule is CONTRIBUTING.md's ("Everything it knows is in the store. No module-level
state, no files, nothing held between requests"). Python accepts every shape below,
and each is a second copy of the world the log never sees: a fork replays the log
and finds the copy empty, a second run in the same process finds it full.

What fires, inside `minutehand/adapters/providers/<p>/`:

  module state    a module-level name mutated by code that runs while serving
                  (inside a function or method): `NAME[k] = v`, `del NAME[k]`,
                  `NAME.attr = v`, `NAME[k].append(v)`, or a container method
                  (`append`, `update`, `add`, `setdefault`, ...) called on a name
                  bound to a list/dict/set literal, comprehension or container
                  constructor; reached by its own name, by a name imported from a
                  sibling module of the same provider, or as `module.NAME`; and any
                  `global NAME` in a function.
  resident self   a class whose instance lives as long as the app — the class
                  `build()` returns, and every class constructed while the app is
                  built: in the body of `build()`, of a resident class's `__init__`
                  or `app`, or of a provider function those call (followed
                  transitively) — that rebinds `self.X` outside `__init__`, mutates
                  through it (`self.X[k] = v`, `self.X.y = v`), or calls a container
                  method on an `X` that `__init__` bound to a container. A closure
                  defined in `__init__` runs later, so it counts as outside.
  closure state   a build-time function (the same set) whose local container, or
                  any local named by `nonlocal`, is mutated by a function nested
                  in it: a handler's closure lives as long as the app.
  class state     a container bound in a plain class body (no bases, no decorator)
                  and mutated through `self.X` or `Cls.X` with no `self.X = ...`
                  shadowing it: one object shared by every instance, per-request
                  instances included.

What does not fire: a module-level constant nothing mutates while serving (tuples,
frozensets, dicts read as tables, compiled regexes, models, `MANIFEST`), a routing
table built once, anything mutated at import, a per-request object (one built in a
handler, not while the app is built) mutating its own attributes, and a local.

Exempt by structure, not by marker:

  - a provider whose manifest declares `state_outside_log` (read from the
    `state_outside_log=` argument in its `manifest.py`, never from its directory
    name): it has declared that it keeps state outside the log, and
    `application.rewind` refuses a fork across it on that declaration;
  - a resident attribute `__init__` annotates as a collection of `asyncio.Task`
    (`set[asyncio.Task[None]]`): tasks running in this process, which no store can
    hold and which end with it. `DeliversInBackground.delivering()` counts these.

What this cannot see: a mutation through another name (`x = self._seen; x.add(v)`),
through a method of our own class other than the container methods, through an
instance method a resident class inherits from a base, a resident class reached
only through a method other than `__init__`/`app`, and a memoising decorator
(`functools.cache`) on a function of the world.

A provider keeping its own user or account roster is NOT a separate check. Its
dangerous form — a roster that outlives the request — is a mutated container on a
module, a resident object, a closure or a class, and fires above. The per-request
memos the providers keep (`asana.app.View._users`, `youtrack.present.Presenter._users`)
read through the store and die with the request; a check keyed on a name like
`users` would fire on those and on nothing real.

Fail-closed. The only way past is `# state-lint: exempt <reason>` on the line.

Run: python -m lints.provider_state [root]
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from lints._core import PACKAGE, SRC, Finding, Source, cli, exempt, sources

NAME = "state"
TITLE = "Provider state"
GUIDANCE = (
    "A provider reads and writes the world only through ports.store.Store; build what a request needs\n"
    "inside the request. A provider that truly keeps state elsewhere declares it in Manifest.state_outside_log."
)

PROVIDERS = (PACKAGE, "adapters", "providers")

CONTAINER_CALLS = frozenset({"list", "dict", "set", "defaultdict", "deque", "OrderedDict", "Counter", "bytearray"})
CONTAINER_TYPES = CONTAINER_CALLS | {"MutableMapping", "MutableSequence", "MutableSet"}
MUTATORS = frozenset(
    {
        "append",
        "appendleft",
        "extend",
        "extendleft",
        "insert",
        "remove",
        "pop",
        "popleft",
        "popitem",
        "clear",
        "sort",
        "reverse",
        "rotate",
        "update",
        "setdefault",
        "add",
        "discard",
        "difference_update",
        "intersection_update",
        "symmetric_difference_update",
    }
)
TASK_TYPES = frozenset({"Task", "Future"})

Scoped = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda


# ---------------------------------------------------------------------------- reading the AST


def _container_value(value: ast.expr | None) -> bool:
    if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    return isinstance(value, ast.Call) and _last_name(value.func) in CONTAINER_CALLS


def _container_annotation(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    base = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    return _last_name(base) in CONTAINER_TYPES


def _task_collection(annotation: ast.expr | None) -> bool:
    """`set[asyncio.Task[None]]`, `list[Task[int]]`, `dict[str, asyncio.Future[bytes]]`: work running in this process."""
    if not isinstance(annotation, ast.Subscript) or not _container_annotation(annotation):
        return False
    args = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    element = args[-1]
    base = element.value if isinstance(element, ast.Subscript) else element
    return _last_name(base) in TASK_TYPES


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _chain(node: ast.expr) -> list[ast.expr]:
    """`a.b[c].d` as [a, a.b, a.b[c], a.b[c].d]: the Attribute/Subscript links from the root out, stopping at a call."""
    links = [node]
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
        links.append(node)
    return list(reversed(links))


def _own_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node of `body` that runs when it does: nested functions, lambdas and classes are left out."""
    skipped = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    stack: list[ast.AST] = [s for s in reversed(body) if not isinstance(s, skipped)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in reversed(list(ast.iter_child_nodes(node))) if not isinstance(c, skipped))


def _nested(body: list[ast.stmt]) -> Iterator[Scoped]:
    """The functions and lambdas defined anywhere inside `body`, at any depth."""
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                yield node


def _params(fn: Scoped) -> set[str]:
    a = fn.args
    names = {p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs]}
    names |= {p.arg for p in (a.vararg, a.kwarg) if p is not None}
    return names


def _locals(fn: Scoped) -> set[str]:
    """Names `fn` binds for itself: its parameters and what its own body assigns, less what it declares global/nonlocal."""
    names = _params(fn)
    if isinstance(fn, ast.Lambda):
        return names
    declared: set[str] = set()
    for node in _own_nodes(fn.body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    for node in fn.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names - declared


@dataclass(frozen=True)
class Touch:
    """One place state is changed: `root` is the name the changed expression hangs from."""

    node: ast.AST
    root: ast.expr
    """The link the change hangs from: a Name, or `module.NAME` / `self.X` as an Attribute."""
    rebinds: bool
    """True when the change binds `root` itself rather than changing what it holds."""
    container_only: bool
    """True when the change is a container method call, which only says something about a container."""


def _touches(nodes: Iterator[ast.AST]) -> Iterator[Touch]:
    for node in nodes:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = [t for target in node.targets for t in _unpacked(target)]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and (
            not isinstance(node, ast.AnnAssign) or node.value is not None
        ):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        for target in targets:
            links = _chain(target)
            yield Touch(node, links[0], rebinds=len(links) == 1, container_only=False)
            if len(links) > 1:
                yield Touch(node, links[1], rebinds=len(links) == 2, container_only=False)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATORS:
            links = _chain(node.func.value)
            yield Touch(node, links[0], rebinds=False, container_only=len(links) == 1)
            if len(links) > 1:
                yield Touch(node, links[1], rebinds=False, container_only=len(links) == 2)


def _unpacked(target: ast.expr) -> list[ast.expr]:
    if isinstance(target, (ast.Tuple, ast.List)):
        return [t for element in target.elts for t in _unpacked(element)]
    if isinstance(target, ast.Starred):
        return _unpacked(target.value)
    return [target]


# ---------------------------------------------------------------------------- one provider


@dataclass(frozen=True)
class Binding:
    module: str
    name: str
    line: int
    container: bool


@dataclass
class Module:
    source: Source
    bindings: dict[str, Binding] = field(default_factory=dict)
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)
    modules: dict[str, str] = field(default_factory=dict)
    """Local name → a sibling module of this provider it stands for."""
    imported: dict[str, tuple[str, str]] = field(default_factory=dict)
    """Local name → (sibling module, name in it)."""


class Provider:
    def __init__(self, package: str, files: list[Source]) -> None:
        self.package = package
        self.modules = {source.module: Module(source) for source in files}
        for module in self.modules.values():
            self._read(module)

    def _read(self, module: Module) -> None:
        tree = module.source.tree
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                module.classes[node.name] = node
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                module.functions[node.name] = node
        for node in _module_level(tree.body):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for t in _unpacked(target):
                        if isinstance(t, ast.Name):
                            module.bindings[t.id] = Binding(
                                module.source.module, t.id, node.lineno, _container_value(node.value)
                            )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                container = _container_value(node.value) or _container_annotation(node.annotation)
                module.bindings[node.target.id] = Binding(module.source.module, node.target.id, node.lineno, container)
            elif isinstance(node, ast.ImportFrom):
                self._import_from(module, node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and alias.name in self.modules:
                        module.modules[alias.asname] = alias.name

    def _import_from(self, module: Module, node: ast.ImportFrom) -> None:
        source = module.source
        if node.level:
            package = source.module if source.is_package else source.module.rpartition(".")[0]
            parts = package.split(".")
            keep = len(parts) - (node.level - 1)
            if keep < 0:
                return
            base = ".".join(parts[:keep] + ([node.module] if node.module else []))
        else:
            base = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if f"{base}.{alias.name}" in self.modules:
                module.modules[bound] = f"{base}.{alias.name}"
            elif base in self.modules:
                module.imported[bound] = (base, alias.name)

    def resolve(self, module: Module, node: ast.expr) -> tuple[str, str] | None:
        """The (module, name) a Name or `module.NAME` stands for, when it is one of this provider's."""
        if isinstance(node, ast.Name):
            if node.id in module.imported:
                return module.imported[node.id]
            if node.id in module.bindings or node.id in module.classes or node.id in module.functions:
                return module.source.module, node.id
            return None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in module.modules:
            return module.modules[node.value.id], node.attr
        return None

    def binding(self, at: tuple[str, str]) -> Binding | None:
        owner = self.modules.get(at[0])
        if owner is None:
            return None
        if at[1] in owner.bindings:
            return owner.bindings[at[1]]
        return self.binding(owner.imported[at[1]]) if at[1] in owner.imported else None

    def cls(self, at: tuple[str, str]) -> tuple[Module, ast.ClassDef] | None:
        owner = self.modules.get(at[0])
        if owner is None:
            return None
        if at[1] in owner.classes:
            return owner, owner.classes[at[1]]
        return self.cls(owner.imported[at[1]]) if at[1] in owner.imported else None

    def function(self, at: tuple[str, str]) -> tuple[Module, ast.FunctionDef | ast.AsyncFunctionDef] | None:
        owner = self.modules.get(at[0])
        if owner is None:
            return None
        if at[1] in owner.functions:
            return owner, owner.functions[at[1]]
        return self.function(owner.imported[at[1]]) if at[1] in owner.imported else None

    # ------------------------------------------------------------------------ what lives as long as the app

    def resident(self) -> Resident:
        """The classes constructed while the app is built, and the functions and methods that build it.

        A method of a resident class called on its instance while building (`api.route(...)` in `build_app`,
        `self._register()` in `__init__`) builds too, and what it constructs is resident as well.
        """
        classes: dict[int, tuple[Module, ast.ClassDef]] = {}
        builders: dict[int, tuple[Module, Scoped]] = {}
        methods: set[int] = set()
        todo: list[tuple[Module, Scoped, ast.ClassDef | None]] = []
        entry = self.modules.get(f"{self.package}.provider")
        if entry is not None and "build" in entry.functions:
            todo.append((entry, entry.functions["build"], None))
        while todo:
            module, fn, owner_cls = todo.pop()
            if id(fn) in builders or isinstance(fn, ast.Lambda):
                continue
            builders[id(fn)] = (module, fn)
            instances: dict[str, tuple[Module, ast.ClassDef]] = {}
            if owner_cls is not None and fn.args.args:
                instances[fn.args.args[0].arg] = (module, owner_cls)
            for node in _own_nodes(fn.body):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    at = self.resolve(module, node.value.func)
                    made = self.cls(at) if at is not None else None
                    if made is not None:
                        instances.update({t.id: made for t in node.targets if isinstance(t, ast.Name)})
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in instances:
                    owner, cls = instances[func.value.id]
                    method = _method(cls, func.attr)
                    if method is not None:
                        methods.add(id(method))
                        todo.append((owner, method, cls))
                    continue
                at = self.resolve(module, func)
                if at is None:
                    continue
                found = self.cls(at)
                if found is not None:
                    owner, cls = found
                    if id(cls) not in classes:
                        classes[id(cls)] = found
                        todo.extend(
                            (owner, m, cls) for m in (_method(cls, "__init__"), _method(cls, "app")) if m is not None
                        )
                    continue
                called = self.function(at)
                if called is not None:
                    todo.append((called[0], called[1], None))
        building = {id(n) for _, fn in builders.values() if not isinstance(fn, ast.Lambda) for n in _own_nodes(fn.body)}
        served = {
            node.attr
            for module in self.modules.values()
            for node in ast.walk(module.source.tree)
            if isinstance(node, ast.Attribute) and id(node) not in building
        }
        build_only = {
            m
            for _, cls in classes.values()
            for m in _methods(cls)
            if id(m) in methods and m.name != "app" and m.name not in served
        }
        return Resident(list(classes.values()), list(builders.values()), {id(m) for m in build_only})


@dataclass(frozen=True)
class Resident:
    classes: list[tuple[Module, ast.ClassDef]]
    builders: list[tuple[Module, Scoped]]
    build_only: set[int]
    """Methods (by id) called only while the app is built: they run once, like `__init__`."""


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    return next(
        (m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == name), None
    )


def _module_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements that run at import: the module body, including inside top-level `if`/`try`/`with`."""
    for node in body:
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            yield from _module_level(node.body)
            yield from _module_level(node.orelse if not isinstance(node, ast.With) else [])
            if isinstance(node, ast.Try):
                yield from _module_level(node.finalbody)
                for handler in node.handlers:
                    yield from _module_level(handler.body)


# ---------------------------------------------------------------------------- the rules


@dataclass
class Collector:
    findings: list[Finding] = field(default_factory=list)
    seen: set[tuple[str, int, str]] = field(default_factory=set)

    def add(self, source: Source, node: ast.AST, message: str) -> None:
        line: int = getattr(node, "lineno", 0)
        end: int | None = getattr(node, "end_lineno", None)
        key = (source.rel, line, message)
        if key in self.seen or exempt(source.lines, NAME, line, end_lineno=end):
            return
        self.seen.add(key)
        self.findings.append(Finding(source.rel, line, message))


def _functions(tree: ast.Module) -> Iterator[tuple[Scoped, list[Scoped]]]:
    """Every function in the file with the functions enclosing it, outermost first."""

    def visit(node: ast.AST, outer: list[Scoped]) -> Iterator[tuple[Scoped, list[Scoped]]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                yield child, outer
                yield from visit(child, [*outer, child])
            else:
                yield from visit(child, outer)

    yield from visit(tree, [])


def _module_state(provider: Provider, out: Collector) -> None:
    for module in provider.modules.values():
        source = module.source
        for fn, outer in _functions(source.tree):
            if isinstance(fn, ast.Lambda):
                own: Iterator[ast.AST] = ast.walk(fn.body)
            else:
                own = _own_nodes(fn.body)
                for node in _own_nodes(fn.body):
                    if isinstance(node, ast.Global):
                        out.add(
                            source,
                            node,
                            f"`global {', '.join(node.names)}` rebinds module state while serving; "
                            "it outlives the request — keep it in the store",
                        )
            shadowed = set().union(*(_locals(f) for f in [*outer, fn]))
            for touch in _touches(own):
                root = touch.root
                if isinstance(root, ast.Name) and root.id in shadowed:
                    continue
                if isinstance(root, ast.Attribute) and (
                    not isinstance(root.value, ast.Name) or root.value.id in shadowed
                ):
                    continue
                at = provider.resolve(module, root)
                bound = provider.binding(at) if at is not None else None
                if bound is None or (touch.container_only and not bound.container):
                    continue
                if touch.rebinds and isinstance(root, ast.Name):
                    continue  # a local of the same name; `global` is reported above
                where = provider.modules[bound.module].source.rel
                out.add(
                    source,
                    touch.node,
                    f"module-level {bound.name} ({where}:{bound.line}) is changed while serving; "
                    "it outlives the request — keep it in the store",
                )


def _instance_attrs(cls: ast.ClassDef) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, set[str], set[str]]:
    """`__init__`, the attributes it binds to a container, and those it binds to a collection of tasks."""
    init = next(
        (m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == "__init__"),
        None,
    )
    containers: set[str] = set()
    tasks: set[str] = set()
    if init is None or not init.args.args:
        return init, containers, tasks
    me = init.args.args[0].arg
    for node in _own_nodes(init.body):
        target: ast.expr | None = None
        value: ast.expr | None = None
        annotation: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value, annotation = node.target, node.value, node.annotation
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == me):
            continue
        if _container_value(value) or _container_annotation(annotation):
            containers.add(target.attr)
        if _task_collection(annotation):
            tasks.add(target.attr)
    return init, containers, tasks


def _methods(cls: ast.ClassDef) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for m in cls.body:
        if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) or not m.args.args:
            continue
        if any(_last_name(d) in {"staticmethod", "classmethod"} for d in m.decorator_list):
            continue
        yield m


def _self_touches(
    method: ast.FunctionDef | ast.AsyncFunctionDef, *, skip_own: bool
) -> Iterator[tuple[Touch, str, Scoped]]:
    """Each change rooted at `self.X` in `method` (or only in its closures, with `skip_own`), as (touch, X, where)."""
    me = method.args.args[0].arg
    scopes: list[tuple[Scoped, set[str]]] = [] if skip_own else [(method, set())]
    for fn, outer in _functions(ast.Module(body=method.body, type_ignores=[])):
        scopes.append((fn, set().union(*(_params(f) for f in [*outer, fn]))))
    for fn, rebound in scopes:
        if me in rebound:
            continue
        nodes = ast.walk(fn.body) if isinstance(fn, ast.Lambda) else _own_nodes(fn.body)
        for touch in _touches(nodes):
            root = touch.root
            if isinstance(root, ast.Attribute) and isinstance(root.value, ast.Name) and root.value.id == me:
                yield touch, root.attr, fn


def _resident_state(provider: Provider, out: Collector) -> list[tuple[Module, Scoped]]:
    resident = provider.resident()
    for module, cls in resident.classes:
        init, containers, tasks = _instance_attrs(cls)
        containers |= _class_containers(cls)
        for method in _methods(cls):
            once = method is init or id(method) in resident.build_only
            for touch, attr, _ in _self_touches(method, skip_own=once):
                if attr in tasks or (touch.container_only and attr not in containers):
                    continue
                verb = "rebinds" if touch.rebinds else "changes"
                out.add(
                    module.source,
                    touch.node,
                    f"{cls.name}.{method.name} {verb} self.{attr} outside __init__; the {cls.name} lives as long as "
                    f"the app, so self.{attr} carries state between requests — keep it in the store",
                )
    return resident.builders


def _closure_state(provider: Provider, builders: list[tuple[Module, Scoped]], out: Collector) -> None:
    for module, builder in builders:
        if isinstance(builder, ast.Lambda):
            continue
        cells: dict[str, bool] = {}
        for node in _own_nodes(builder.body):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for t in _unpacked(target):
                        if isinstance(t, ast.Name):
                            cells[t.id] = _container_value(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                cells[node.target.id] = _container_value(node.value) or _container_annotation(node.annotation)
        if not cells:
            continue
        for fn, outer in _functions(ast.Module(body=builder.body, type_ignores=[])):
            if not isinstance(fn, ast.Lambda):
                for node in _own_nodes(fn.body):
                    if isinstance(node, ast.Nonlocal):
                        named = [n for n in node.names if n in cells]
                        if named:
                            out.add(
                                module.source,
                                node,
                                f"`nonlocal {', '.join(named)}`: {builder.name} builds the app, so its locals "
                                "live as long as the app and carry state between requests — keep it in the store",
                            )
            shadowed = set().union(*(_locals(f) for f in [*outer, fn]))
            nodes = ast.walk(fn.body) if isinstance(fn, ast.Lambda) else _own_nodes(fn.body)
            for touch in _touches(nodes):
                root = touch.root
                if not isinstance(root, ast.Name) or root.id in shadowed or root.id not in cells or touch.rebinds:
                    continue
                if touch.container_only and not cells[root.id]:
                    continue
                out.add(
                    module.source,
                    touch.node,
                    f"{root.id} is a local of {builder.name}, which builds the app; a closure changing it carries "
                    "state between requests — keep it in the store",
                )


def _class_containers(cls: ast.ClassDef) -> set[str]:
    found: set[str] = set()
    for node in cls.body:
        if isinstance(node, ast.Assign) and _container_value(node.value):
            found |= {t.id for target in node.targets for t in _unpacked(target) if isinstance(t, ast.Name)}
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and _container_value(node.value)
        ):
            found.add(node.target.id)
    return found


def _class_state(provider: Provider, out: Collector) -> None:
    for module in provider.modules.values():
        for cls in (n for n in ast.walk(module.source.tree) if isinstance(n, ast.ClassDef)):
            if cls.bases or cls.keywords or cls.decorator_list:
                continue
            shared = _class_containers(cls)
            if not shared:
                continue
            on_self = {
                attr
                for method in _methods(cls)
                for touch, attr, _ in _self_touches(method, skip_own=False)
                if touch.rebinds
            }
            shared -= on_self
            for method in _methods(cls):
                for touch, attr, _ in _self_touches(method, skip_own=False):
                    if attr in shared:
                        out.add(module.source, touch.node, _shared(cls.name, attr))
            for node in ast.walk(cls):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for touch in _touches(_own_nodes(node.body)):
                    root = touch.root
                    if (
                        isinstance(root, ast.Attribute)
                        and isinstance(root.value, ast.Name)
                        and root.value.id == cls.name
                        and root.attr in shared
                    ):
                        out.add(module.source, touch.node, _shared(cls.name, root.attr))


def _shared(cls: str, attr: str) -> str:
    return (
        f"{cls}.{attr} is a class attribute, one object shared by every {cls}, changed while serving; "
        "it outlives the request — keep it in the store"
    )


# ---------------------------------------------------------------------------- the manifest's declaration


def _declares_state_outside_log(files: list[Source], package: str) -> bool:
    """True when the provider's `manifest.py` passes `state_outside_log=` something other than None."""
    manifest = next((s for s in files if s.module == f"{package}.manifest"), None)
    if manifest is None:
        return False
    for node in ast.walk(manifest.tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "state_outside_log":
                    return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
    return False


def run(root: Path = SRC) -> list[Finding]:
    prefix = ".".join(PROVIDERS) + "."
    by_provider: dict[str, list[Source]] = {}
    for source in sources(root):
        module = source.module
        if not module.startswith(prefix):
            continue
        name = module[len(prefix) :].split(".", 1)[0]
        if name.startswith("_"):
            continue
        by_provider.setdefault(prefix + name, []).append(source)
    out = Collector()
    for package, files in sorted(by_provider.items()):
        if _declares_state_outside_log(files, package):
            continue
        provider = Provider(package, files)
        _module_state(provider, out)
        builders = _resident_state(provider, out)
        _closure_state(provider, builders, out)
        _class_state(provider, out)
    return sorted(out.findings, key=lambda f: (f.file, f.line or 0, f.message))


if __name__ == "__main__":
    sys.exit(cli(TITLE, run, sys.argv[1:], guidance=GUIDANCE))
