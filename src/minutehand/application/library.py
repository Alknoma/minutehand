"""The scenario library: situations in the world every proactive agent meets, shipped inside the package
(`minutehand/library/`, one YAML file each), listed, and written out with one team's people filled in.

A written-out scenario is an ordinary scenario file, headed by a comment saying what it is for; the team owns it from
then on and may edit it like any other.
"""

from __future__ import annotations

import textwrap
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import ValidationError

from minutehand.application.files import FileRefused, read_yaml
from minutehand.domain.library import LibraryScenario, TeamValues
from minutehand.domain.scenario import WrittenScenario

FOLDER = "library"
"""The library's folder inside the `minutehand` package."""


class NotInLibrary(LookupError):
    """A name no library scenario has."""


def entries() -> list[LibraryScenario]:
    """Every library scenario, by name."""
    found: list[LibraryScenario] = []
    for item in sorted(files("minutehand").joinpath(FOLDER).iterdir(), key=lambda i: i.name):
        if not item.name.endswith(".yaml"):
            continue
        said = f"{FOLDER}/{item.name}"
        try:
            entry = LibraryScenario.model_validate(read_yaml(item.read_text(encoding="utf-8"), said))
        except ValidationError as e:
            raise FileRefused(f"{said}: not a valid library scenario:\n{e}") from e
        if f"{entry.name}.yaml" != item.name:
            raise FileRefused(f"{said}: names its scenario {entry.name!r}; the file is named after its scenario")
        found.append(entry)
    return found


def entry(name: str) -> LibraryScenario:
    """One library scenario by name, or the names there are."""
    every = entries()
    found = next((e for e in every if e.name == name), None)
    if found is None:
        raise NotInLibrary(f"no library scenario {name!r}; there are {', '.join(e.name for e in every)}")
    return found


def written(found: LibraryScenario, team: TeamValues) -> tuple[WrittenScenario, str]:
    """The scenario with the team's values in it, and the file's text: a comment saying what it is for, then the
    scenario. Refused, naming the scenario, when the values make it invalid."""
    try:
        scenario = found.written(team)
    except ValidationError as e:
        raise FileRefused(f"{found.name}, filled with these values, is not a valid scenario:\n{e}") from e
    return scenario, header(found) + yaml.safe_dump(found.filled(team), sort_keys=False, allow_unicode=True, width=116)


def header(found: LibraryScenario) -> str:
    """What the written file says of itself, as YAML comments."""
    paragraphs = [
        f"{found.name}, from the Minutehand scenario library (`minutehand scenarios show {found.name}`).",
        f"Situation: {found.situation}",
        "A world: it hands the agent no work. The agent brings its own (its prompt, the state and items it sets), and "
        "every run is assessed against its instructions and what this file declares, with nothing more to write "
        "(docs/assessments.md).",
    ]
    lines: list[str] = []
    for paragraph in paragraphs:
        lines += [*textwrap.wrap(paragraph, 114), ""]
    return "".join(f"# {line}".rstrip() + "\n" for line in lines[:-1])


def write(found: LibraryScenario, team: TeamValues, folder: Path, *, replace: bool) -> Path:
    """The filled scenario written to `<folder>/<name>.yaml`; an existing file is refused unless `replace`."""
    _, text = written(found, team)
    path = folder / f"{found.name}.yaml"
    if path.exists() and not replace:
        raise FileExistsError(f"{path} exists; give --force to replace it, or --out another folder")
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
