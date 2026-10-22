"""The agent's own environment, as the agent file declares it (`domain.agent.AgentEnvironment`), resolved when
Minutehand starts the agent's process (docs/agent-environment.md).

Three sources, combined in the declared order, a later one overriding an earlier one: `inherit`, the environment
Minutehand was started in; `files`, dotenv files Minutehand reads itself, as Compose reads `env_file`; `only`, an
allowlist of the inherited one. Minutehand's own variables (the proxy, its CA, the receiver, the signing secrets)
apply last, and no source overrides them. Minutehand's own credentials (`withheld`) are never handed out, whatever
the source. What is kept with the run is the declaration and the NAMES each source supplied (`EnvironmentRecord`),
never a value; the values go from here to the process and, as `Resolved.declared`, into the record's redaction.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import Field

from minutehand.application.refusals import RunRefused
from minutehand.domain.agent import (
    AgentEnvironment,
    EnvironmentFiles,
    EnvironmentSourceKind,
    InheritedEnvironment,
    OnlyFromInherited,
)
from minutehand.domain.scenario import Model

ENVIRONMENT_RECORD = "environment.json"
"""`EnvironmentRecord`, kept beside each run: the declaration and the names, never a value."""

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
"""A variable's name as Compose accepts one in a dotenv file (dots and dashes included, which a shell refuses)."""

_DOUBLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}


class EnvironmentRefused(RunRefused):
    """A declared source cannot be read as it is: a file missing or unreadable, a line that is not `KEY=value`, a
    value that asks for interpolation, or a name that is Minutehand's own."""


class SourceRecord(Model):
    """One source as it was read at the start: its kind, the file when it is one, and the names it supplied."""

    kind: EnvironmentSourceKind
    path: str | None = Field(default=None, description="The dotenv file, for `files`; None for the other kinds")
    supplied: list[str] = Field(description="The variables it supplied, by name, in the file's or the list's order")


class EnvironmentRecord(Model):
    """What the run keeps of the agent's environment: never a value."""

    sources: list[SourceRecord]
    own: list[str] = Field(description="Minutehand's own variables, applied last: the proxy, its CA, the secrets")
    withheld: list[str] = Field(description="Minutehand's own credentials a source held and the agent was not given")


class SourceCheck(Model):
    """`minutehand doctor`: whether a declared source can be read, and the names it supplies; never a value."""

    kind: EnvironmentSourceKind
    path: str | None = Field(default=None, description="The dotenv file, for `files`")
    readable: bool
    names: list[str] = Field(description="The variables it supplies; for `only`, those of its list that are set")
    problem: str | None = Field(default=None, description="Why it cannot be read, when it cannot")


@dataclass(frozen=True)
class Resolved:
    """The agent's environment, resolved: the values its process is started with, and what the run keeps of it."""

    values: dict[str, str] = field(repr=False)
    record: EnvironmentRecord
    declared: frozenset[str] = field(repr=False)
    """Every value a `files` or `only` source supplied: what joins the record's redaction."""


def parse_dotenv(text: str, *, where: str) -> dict[str, str]:
    """A dotenv file as Compose reads `env_file`: `KEY=value` a line, an `export ` prefix allowed; a blank line or
    one starting with `#` skipped; an unquoted value trimmed, ending at ` #`; a single-quoted value kept as it is; a
    double-quoted value with `\\n`, `\\t`, `\\"`, `\\\\` read as escapes. A later line wins over an earlier one of
    the same name. Refused, naming the file and the line: a line with no `=`, a name no variable can have, an
    unterminated quote, and `${...}` in a value (interpolation is not performed: write the value out)."""
    found: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export") :].strip()
        name, equals, rest = line.partition("=")
        name = name.strip()
        if not equals or not _NAME.match(name):
            raise EnvironmentRefused(
                f"{where}, line {number}: not `NAME=value` (a dotenv file holds one variable a line, `export` allowed)"
            )
        value = _value(rest.strip(), where=where, number=number)
        if "${" in value:
            raise EnvironmentRefused(
                f"{where}, line {number}: {name} asks for interpolation (`${{...}}`), which is not performed: write "
                "the value out"
            )
        found[name] = value
    return found


def _value(rest: str, *, where: str, number: int) -> str:
    if rest.startswith("'"):
        end = rest.find("'", 1)
        if end < 0:
            raise EnvironmentRefused(f"{where}, line {number}: a single quote is opened and never closed")
        return rest[1:end]
    if rest.startswith('"'):
        out: list[str] = []
        i = 1
        while i < len(rest):
            char = rest[i]
            if char == "\\" and i + 1 < len(rest):
                out.append(_DOUBLE_ESCAPES.get(rest[i + 1], "\\" + rest[i + 1]))
                i += 2
                continue
            if char == '"':
                return "".join(out)
            out.append(char)
            i += 1
        raise EnvironmentRefused(f"{where}, line {number}: a double quote is opened and never closed")
    comment = re.search(r"\s#", rest)
    return (rest[: comment.start()] if comment else rest).strip()


def _read(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise EnvironmentRefused(f"the agent's environment names {path}, which does not exist") from None
    except OSError as e:
        raise EnvironmentRefused(f"the agent's environment names {path}, which cannot be read: {e.strerror}") from e
    return parse_dotenv(text, where=str(path))


def resolve(
    declared: AgentEnvironment,
    *,
    inherited: Mapping[str, str],
    own: Mapping[str, str],
    withheld: Collection[str],
) -> Resolved:
    """The environment the agent's process is started with: each source in order, then `own`, Minutehand's. A name
    in `withheld` (Minutehand's own credentials) is dropped from every source and listed in the record; one an
    `only` source asks for by name is refused, since the file asked for what no agent is given."""
    values: dict[str, str] = {}
    sources: list[SourceRecord] = []
    held_back: list[str] = []
    from_declared: set[str] = set()
    for source in declared.sources:
        if isinstance(source, InheritedEnvironment):
            taken = {k: v for k, v in inherited.items() if k not in withheld}
            held_back += [k for k in inherited if k in withheld]
            sources.append(SourceRecord(kind=EnvironmentSourceKind.INHERIT, supplied=sorted(taken)))
            values.update(taken)
        elif isinstance(source, EnvironmentFiles):
            for named in source.paths:
                path = Path(named)
                read = _read(path)
                taken = {k: v for k, v in read.items() if k not in withheld}
                held_back += [k for k in read if k in withheld]
                sources.append(SourceRecord(kind=EnvironmentSourceKind.FILES, path=str(path), supplied=list(taken)))
                values.update(taken)
                from_declared.update(taken.values())
        else:
            asked = [n for n in source.names if n in withheld]
            if asked:
                raise EnvironmentRefused(
                    f"the agent's environment asks for {', '.join(asked)} by name, which is Minutehand's own "
                    "credential and is never handed to the agent"
                )
            taken = {n: inherited[n] for n in source.names if n in inherited}
            sources.append(SourceRecord(kind=EnvironmentSourceKind.ONLY, supplied=list(taken)))
            values.update(taken)
            from_declared.update(taken.values())
    values.update(own)
    return Resolved(
        values=values,
        record=EnvironmentRecord(sources=sources, own=sorted(own), withheld=sorted(set(held_back))),
        declared=frozenset(from_declared),
    )


def inspect(
    declared: AgentEnvironment, *, inherited: Mapping[str, str], withheld: Collection[str]
) -> list[SourceCheck]:
    """Each declared source, readable or not, with the names it supplies: what `minutehand doctor` reports."""
    checks: list[SourceCheck] = []
    for source in declared.sources:
        if isinstance(source, InheritedEnvironment):
            names = sorted(k for k in inherited if k not in withheld)
            checks.append(SourceCheck(kind=EnvironmentSourceKind.INHERIT, readable=True, names=names))
        elif isinstance(source, EnvironmentFiles):
            for named in source.paths:
                try:
                    read = _read(Path(named))
                except EnvironmentRefused as e:
                    checks.append(SourceCheck(kind=source.kind, path=named, readable=False, names=[], problem=str(e)))
                    continue
                checks.append(
                    SourceCheck(
                        kind=source.kind, path=named, readable=True, names=[k for k in read if k not in withheld]
                    )
                )
        else:
            missing = [n for n in source.names if n not in inherited]
            problem = f"not set in Minutehand's environment: {', '.join(missing)}" if missing else None
            checks.append(
                SourceCheck(
                    kind=source.kind,
                    readable=True,
                    names=[n for n in source.names if n in inherited and n not in withheld],
                    problem=problem,
                )
            )
    return checks


def written(values: Mapping[str, str]) -> str:
    """The environment as the file the agent's command sources inside the sandbox (`ENVIRONMENT_FILE_VARIABLE`):
    one `export NAME=value` a line, each value quoted for a POSIX shell; a name a shell cannot assign (a dotenv
    file's `a.b`) is left out, since no shell could read it."""
    lines = [
        f"export {name}={shlex.quote(value)}"
        for name, value in values.items()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
    ]
    return "\n".join(lines) + "\n"


def relative_to(declared: AgentEnvironment, base: Path) -> AgentEnvironment:
    """The declaration with every file's path made absolute from `base`, the agent file's folder, so a fork that
    reads the agent back from its run's folder reads the same files."""
    sources: list[InheritedEnvironment | EnvironmentFiles | OnlyFromInherited] = [
        s.model_copy(update={"paths": [str((base / p).resolve()) for p in s.paths]})
        if isinstance(s, EnvironmentFiles)
        else s
        for s in declared.sources
    ]
    return declared.model_copy(update={"sources": sources})


def files_of(declared: AgentEnvironment) -> Sequence[str]:
    """Every dotenv file the declaration names, in order."""
    return [p for s in declared.sources if isinstance(s, EnvironmentFiles) for p in s.paths]
