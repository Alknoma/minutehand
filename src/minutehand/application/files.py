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
from pydantic import BaseModel, JsonValue, ValidationError

from minutehand.domain.agent import AgentUnderTest
from minutehand.domain.experiment import Fork
from minutehand.domain.prices import Prices
from minutehand.domain.scenario import Seed, WrittenScenario
from minutehand.domain.templates import RUN_DIR, RUN_FILLED, RUN_PORT

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
    return with_documents_from(path, _load(path, WrittenScenario, called="Scenario"))


def load_agent(path: Path, *, text: str | None = None) -> AgentUnderTest:
    """An agent file, its `checks` made absolute from the file's own folder, so a fork that reads the agent back
    from its run's folder finds them. `text`, when given, is read in place of the file's own (the file as `run-all`
    fills it), and the file still names it and places its relative paths."""
    if text is None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise FileRefused(f"{path}: cannot be read: {e.strerror}") from e
    unfilled = [p for p in RUN_FILLED if p in text]
    if unfilled:
        raise FileRefused(
            f"{path}: holds {' and '.join(unfilled)}, which only `minutehand run` and `run-all` fill, when they start "
            "the agent's command: give the command after --, or write the port and folder out"
        )
    raw = _parsed(path, text, AgentUnderTest)
    return _with_checks_from(path, _validate(path, raw, AgentUnderTest))


def _with_checks_from(path: Path, agent: AgentUnderTest) -> AgentUnderTest:
    base = path.resolve().parent
    return agent.model_copy(
        update={
            "checks": [str((base / c).resolve()) for c in agent.checks],
            "watches": [str((base / w).resolve()) for w in agent.watches],
        }
    )


_S = TypeVar("_S", WrittenScenario, Seed)


def with_documents_from(path: Path, scenario: _S) -> _S:
    """A scenario whose services' OpenAPI documents are named by file, each made absolute from the scenario's own
    folder, so a fork that reads the scenario back from its run's folder finds them. A URL is left as written."""
    base = path.resolve().parent
    services = [
        s.model_copy(update={"openapi": str((base / s.openapi).resolve())})
        if s.openapi is not None and not s.openapi.startswith(("http://", "https://"))
        else s
        for s in scenario.services
    ]
    return scenario.model_copy(update={"services": services})


def load_document(where: str) -> JsonValue:
    """An OpenAPI document from a file, JSON or YAML."""
    path = Path(where)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise FileRefused(f"{where}: the service's OpenAPI document could not be read: {e}") from e
    found = read_yaml(text, where)
    if not isinstance(found, dict):
        raise FileRefused(f"{where}: an OpenAPI document is a mapping at its top")
    return json.loads(json.dumps(found, default=str))


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
    return _parsed(path, text, model, called=called)


def _parsed(path: Path, text: str, model: type[BaseModel], *, called: str | None = None) -> object:
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


def read_yaml(text: str, said: str) -> object:
    """YAML text as this module reads every file: no implicit timestamps or base-60 numbers. `said` names it in a
    refusal."""
    try:
        return yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as e:
        raise FileRefused(f"{said}: not valid YAML: {e}") from e


def _validate(path: Path, raw: object, model: type[_M], *, called: str | None = None) -> _M:
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise FileRefused(f"{path}: not a valid {called or model.__name__}:\n{e}") from e


def load_prices(path: Path) -> Prices:
    """A prices file: what each named model costs per million tokens, for `model_calls.cost`."""
    return _load(path, Prices, called="prices")


def load_seed(path: Path) -> Seed:
    """A standing world's seed: a scenario file with nothing to achieve."""
    return with_documents_from(path, _load(path, Seed))


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
    """What a file is from what it holds: people and a window to watch the agent for (`runs_for`) make a scenario, as
    do people and an older scenario's goal and owner; people alone a seed, a standing world's; anything else an agent
    file."""
    if isinstance(raw, dict) and "people" in raw:
        run = "runs_for" in raw or ("goal" in raw and "owner" in raw)
        return FileKind.SCENARIO if run else FileKind.SEED
    return FileKind.AGENT


def where(location: tuple[int | str, ...]) -> str:
    """A place in a file as a path a reader follows: `inboxes[0].pending.items`."""
    out = ""
    for step in location:
        out += f"[{step}]" if isinstance(step, int) else (f".{step}" if out else str(step))
    return out or "(the whole file)"


def filled_as_run(text: str, path: Path) -> str:
    """An agent file's text with `{run.port}` and `{run.dir}` filled with stand-ins of their kind: the lowest port
    `run-all` hands out and the file's own folder."""
    return text.replace(RUN_PORT, "20000").replace(RUN_DIR, str(path.resolve().parent))


def problems(path: Path, kind: FileKind | None = None) -> tuple[FileKind | None, BaseModel | None, list[str]]:
    """Every load-time problem of one file, each naming its place in the file; the model read when there is none."""
    try:
        model = MODELS[kind] if kind is not None else AgentUnderTest
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise FileRefused(f"{path}: cannot be read: {e.strerror}") from e
        # read as `run` and `run-all` read it, each placeholder they fill filled, so a field that checks what it
        # holds (a URL's port) is checked as the run will see it
        raw = _parsed(path, filled_as_run(text, path), model)
    except FileRefused as e:
        return kind, None, [str(e)]
    kind = kind or kind_of(raw)
    try:
        read = MODELS[kind].model_validate(raw)
        return kind, _with_checks_from(path, read) if isinstance(read, AgentUnderTest) else read, []
    except ValidationError as e:
        return kind, None, [f"{path}: {where(tuple(err['loc']))}: {err['msg']}" for err in e.errors()]
