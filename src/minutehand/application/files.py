"""Scenario, agent and fork-changes files, YAML or JSON, read into their models. Every error names the file.

YAML is read without its implicit timestamps and base-60 numbers: `opens: 09:00` would otherwise arrive as
the integer 540 and validate as nine minutes past midnight. Every such value reaches pydantic as the text
it was written as, and pydantic parses it against the field's own type.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.experiment import Fork
from minutehand.domain.scenario import Seed, WrittenScenario

_M = TypeVar("_M", bound=BaseModel)

_INT = "tag:yaml.org,2002:int"
_FLOAT = "tag:yaml.org,2002:float"
_TIMESTAMP = "tag:yaml.org,2002:timestamp"


class _Loader(yaml.SafeLoader):
    """SafeLoader with no timestamps and no base-60 integers or floats."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag not in (_INT, _FLOAT, _TIMESTAMP)]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    _INT,
    re.compile(r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+)$"),
    list("-+0123456789"),
)
_Loader.add_implicit_resolver(
    _FLOAT,
    re.compile(
        r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9_]+(?:[eE][-+][0-9]+)?"
        r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
    ),
    list("-+0123456789."),
)


class FileRefused(ValueError):
    """A scenario or agent file that could not be read or does not describe a valid model."""


def load_scenario(path: Path) -> WrittenScenario:
    """A scenario file; one without `starts_at` starts when the run does."""
    return _load(path, WrittenScenario, called="Scenario")


def load_agent(path: Path) -> AgentUnderTest:
    """An agent file, its `checks` made absolute from the file's own folder, so a fork that reads the agent back
    from its run's folder finds them."""
    return _with_checks_from(path, _load(path, AgentUnderTest))


def _with_checks_from(path: Path, agent: AgentUnderTest) -> AgentUnderTest:
    base = path.resolve().parent
    return agent.model_copy(update={"checks": [str((base / c).resolve()) for c in agent.checks]})


def load_fork(path: Path, *, parent_run: str, at_seq: int) -> Fork:
    """A fork's changes: a file holding `overrides` and optionally `samples`. Which run and where come from
    whoever asks for the fork, so a file naming `parent_run` or `at_seq` itself is refused."""
    raw = _read(path, Fork)
    if not isinstance(raw, dict):
        raise FileRefused(f"{path}: a fork's changes file is a mapping with 'overrides', not {type(raw).__name__}")
    named = sorted(k for k in ("parent_run", "at_seq") if k in raw)
    if named:
        raise FileRefused(f"{path}: names {', '.join(named)}; the run and the seq to fork at are given on the command")
    return _validate(path, {**raw, "parent_run": parent_run, "at_seq": at_seq}, Fork)


def _load(path: Path, model: type[_M], *, called: str | None = None) -> _M:
    """`called` is what the file is to its reader, when the model's own name is not that."""
    return _validate(path, _read(path, model, called=called), model, called=called)


def _read(path: Path, model: type[BaseModel], *, called: str | None = None) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise FileRefused(f"{path}: cannot be read: {e.strerror}") from e
    suffix = path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            raw: object = yaml.load(text, Loader=_Loader)
        elif suffix == ".json":
            raw = json.loads(text)
        else:
            raise FileRefused(
                f"{path}: a {called or model.__name__} file is .yaml, .yml or .json, not {suffix or 'no suffix'}"
            )
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        raise FileRefused(f"{path}: not valid {suffix.lstrip('.').upper()}: {e}") from e
    return raw


def _validate(path: Path, raw: object, model: type[_M], *, called: str | None = None) -> _M:
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise FileRefused(f"{path}: not a valid {called or model.__name__}:\n{e}") from e


def load_seed(path: Path) -> Seed:
    """A standing world's seed: a scenario file with nothing to achieve."""
    return _load(path, Seed)


class FileKind(StrEnum):
    """What a declaration file is, for `minutehand schema` and `minutehand validate`."""

    AGENT = "agent"
    SCENARIO = "scenario"
    SEED = "seed"


MODELS: dict[FileKind, type[BaseModel]] = {
    FileKind.AGENT: AgentUnderTest,
    FileKind.SCENARIO: WrittenScenario,
    FileKind.SEED: Seed,
}
"""The model each kind of file is read as: its JSON Schema is the file's (`schemas/<kind>.schema.json`)."""

SCHEMA_BASE = "https://raw.githubusercontent.com/Alknoma/minutehand/integration-main/schemas"
"""Where the published schemas are read from by an editor (`# yaml-language-server: $schema=<base>/agent.schema.json`)."""


def schema(kind: FileKind) -> dict[str, object]:
    """The JSON Schema (2020-12) of one kind of file, as Pydantic generates it from the model."""
    found = MODELS[kind].model_json_schema(mode="validation")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{SCHEMA_BASE}/{kind.value}.schema.json",
        **found,
    }


def kind_of(raw: object) -> FileKind:
    """What a file is from what it holds: people and a goal make a scenario, people alone a seed, anything else an
    agent file."""
    if isinstance(raw, dict) and "people" in raw:
        return FileKind.SCENARIO if "goal" in raw and "owner" in raw else FileKind.SEED
    return FileKind.AGENT


def where(location: tuple[int | str, ...]) -> str:
    """A place in a file as a path a reader follows: `inboxes[0].pending.items`."""
    out = ""
    for step in location:
        out += f"[{step}]" if isinstance(step, int) else (f".{step}" if out else str(step))
    return out or "(the whole file)"


def problems(path: Path, kind: FileKind | None = None) -> tuple[FileKind | None, BaseModel | None, list[str]]:
    """Every load-time problem of one file, each naming its place in the file; the model read when there is none."""
    try:
        model = MODELS[kind] if kind is not None else AgentUnderTest
        raw = _read(path, model)
    except FileRefused as e:
        return kind, None, [str(e)]
    kind = kind or kind_of(raw)
    try:
        read = MODELS[kind].model_validate(raw)
        return kind, _with_checks_from(path, read) if isinstance(read, AgentUnderTest) else read, []
    except ValidationError as e:
        return kind, None, [f"{path}: {where(tuple(err['loc']))}: {err['msg']}" for err in e.errors()]
