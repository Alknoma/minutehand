"""The contract imports nothing that implements it, and a provider never imports another provider.

  minutehand.domain       imports nothing from application, adapters, ports
  minutehand.ports        imports nothing from application, adapters
  minutehand.application  imports nothing from adapters
  minutehand.checks       imports only domain and ports (and itself)
  minutehand.adapters.providers.<a>  imports nothing from minutehand.adapters.providers.<b>

`ports` imports `domain` and never the reverse, which is why domain may not import
ports. Python accepts every one of these imports; only the direction is wrong.

Read from the AST, not by regex, so every spelling is one import: `import a.b`,
`from a import b`, `from a.b import c`, a relative `from ..adapters import x`, an
import inside a function or under `if TYPE_CHECKING:`. The parent repo's regex
missed package-level `from X.adapters import f` for months; there is no pattern
here to miss it with. What this cannot see: an import spelled as a string
(`importlib.import_module`, an entry point in pyproject.toml).

Fail-closed. The only way past is `# import-lint: exempt <reason>` on a line of
the import statement.

Run: python -m lints.import_boundaries [root]
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

from lints._core import PACKAGE, SRC, Finding, Source, cli, exempt, sources

NAME = "import"
TITLE = "Import boundaries"
GUIDANCE = (
    "The contract (domain, ports) is what every part is written against; it cannot depend on a part.\n"
    "If the domain needs something an adapter has, it belongs in domain or behind a port."
)

PROVIDERS = f"{PACKAGE}.adapters.providers"


@dataclass(frozen=True)
class Rule:
    layer: str
    forbidden: tuple[str, ...] = ()
    allowed: tuple[str, ...] | None = None
    """When set, every import of the package must sit under one of these."""


def _pkg(name: str) -> str:
    return f"{PACKAGE}.{name}"


RULES = (
    Rule(_pkg("domain"), forbidden=(_pkg("application"), _pkg("adapters"), _pkg("ports"))),
    Rule(_pkg("ports"), forbidden=(_pkg("application"), _pkg("adapters"))),
    Rule(_pkg("application"), forbidden=(_pkg("adapters"),)),
    Rule(_pkg("checks"), allowed=(_pkg("domain"), _pkg("ports"), _pkg("checks"))),
)


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _providers(root: Path) -> frozenset[str]:
    """Every provider in the tree: a package or module directly under adapters/providers/.

    Read from disk because `from minutehand.adapters.providers import x` may name a
    provider or something the package itself defines, and only the tree knows which.
    """
    base = root.joinpath(*PROVIDERS.split("."))
    if not base.is_dir():
        return frozenset()
    return frozenset(
        entry.stem
        for entry in base.iterdir()
        if not entry.name.startswith(("_", "."))
        and ((entry.is_dir() and entry.name != "__pycache__") or entry.suffix == ".py")
    )


def _provider(module: str, providers: frozenset[str]) -> str | None:
    """The provider a module belongs to, or None when it is not inside one."""
    if not module.startswith(PROVIDERS + "."):
        return None
    name = module[len(PROVIDERS) + 1 :].split(".", 1)[0]
    return name if name in providers else None


def _targets(node: ast.Import | ast.ImportFrom, source: Source) -> list[tuple[str, ...]]:
    """Per imported name, the modules it may be — most specific first.

    `from a import b` may import the submodule `a.b` or the name `b` defined in
    `a`; nothing in the AST says which, so both are candidates.
    """
    if isinstance(node, ast.Import):
        return [(alias.name,) for alias in node.names]
    if node.level:
        package = source.module if source.is_package else source.module.rpartition(".")[0]
        parts = package.split(".") if package else []
        keep = len(parts) - (node.level - 1)
        if keep < 0:
            return []
        base = ".".join(parts[:keep] + ([node.module] if node.module else []))
    else:
        base = node.module or ""
    return [(f"{base}.{alias.name}" if base else alias.name, base) for alias in node.names]


def _violation(module: str, candidates: tuple[str, ...], providers: frozenset[str]) -> str | None:
    internal = [c for c in candidates if _under(c, PACKAGE)]
    if not internal:
        return None
    for rule in RULES:
        if not _under(module, rule.layer):
            continue
        for target in internal:
            for prefix in rule.forbidden:
                if _under(target, prefix):
                    return f"{rule.layer} imports {target}: {rule.layer} never imports {prefix}"
        if rule.allowed is not None and not any(
            _under(target, prefix) for target in internal for prefix in rule.allowed
        ):
            return f"{rule.layer} imports {internal[0]}: {rule.layer} imports only {', '.join(rule.allowed)}"
    mine = _provider(module, providers)
    if mine is not None:
        for target in internal:
            theirs = _provider(target, providers)
            if theirs is not None and theirs != mine:
                return f"provider {mine} imports {target}: a provider never imports another provider"
    return None


def run(root: Path = SRC) -> list[Finding]:
    providers = _providers(root)
    findings: list[Finding] = []
    for source in sources(root):
        module = source.module
        for node in ast.walk(source.tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for candidates in _targets(node, source):
                message = _violation(module, candidates, providers)
                if message is None:
                    continue
                if exempt(source.lines, NAME, node.lineno, end_lineno=node.end_lineno):
                    continue
                findings.append(Finding(source.rel, node.lineno, message))
    return findings


if __name__ == "__main__":
    sys.exit(cli(TITLE, run, sys.argv[1:], guidance=GUIDANCE))
