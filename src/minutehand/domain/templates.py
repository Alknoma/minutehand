"""The one placeholder syntax new declarations use: `{namespace.name}` inside a string of a template.

    {person.key} {person.email} {person.name} {person.credential}   the person Minutehand acts as
    {item.id}                                                        the item a decision is made on
    {input.<name>}                                                   what the person gives with a decision
    {clock.now}                                                      the simulated moment, ISO 8601
    {page.cursor}                                                    the cursor of the page a list reads
    {run.port} {run.dir}                                             an agent file's: filled by `run` and `run-all`
    {run.id} {case.id}                                               reserved for declarations that need them
    {team.goal} {team.ask_email} ...                                 a team's values in a library scenario
                                                                     (`domain.library`)

A template is filled by replacing each placeholder with its value inside strings only: a body written as structure
keeps its structure, and a value is never parsed as JSON and needs no escaping. Which names a declaration may use
is said where it is declared; any other is refused when the file is loaded. The older declarations' placeholders
(`{reply_id}`, `{message_id}`, `{port}`, `{{start+P2D}}`, `{hex}`) are listed as debt in `docs/agent-contract.md`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import JsonValue

NAMESPACES = ("person", "item", "input", "clock", "page", "run", "case", "team")

PLACEHOLDER = re.compile(r"\{([a-z]+)\.([a-z][a-z0-9_]*)\}")

RUN_PORT = "{run.port}"
RUN_DIR = "{run.dir}"
RUN_FILLED = (RUN_PORT, RUN_DIR)
"""Filled in an agent file's text, and in each word of the agent's command, by the commands that start the agent:
`minutehand run` once per invocation and `run-all` once per scenario (`minutehand.run_all`). Any other reader of an
agent file (`env`, `doctor`, `run` with no command) refuses a file that still holds one."""

RUN_PORT_VARIABLE = "MINUTEHAND_RUN_PORT"
RUN_DIR_VARIABLE = "MINUTEHAND_RUN_DIR"
"""What `{run.port}` and `{run.dir}` were filled with, as the agent's command is told: kept with each run, so a fork
or a replay starts the agent listening, and keeping its files, where the run's agent file says it does."""

PERSON = ("person.key", "person.email", "person.name", "person.credential")


def named(value: JsonValue) -> list[str]:
    """Every `namespace.name` a template names, in order, from the strings inside it (keys are not templates)."""
    if isinstance(value, str):
        return [f"{m.group(1)}.{m.group(2)}" for m in PLACEHOLDER.finditer(value)]
    if isinstance(value, list):
        return [n for v in value for n in named(v)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in named(v)]
    return []


def refuse_unknown(where: str, value: JsonValue, allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(named(value)) - set(allowed))
    if unknown:
        raise ValueError(
            f"{where} names {', '.join('{' + u + '}' for u in unknown)}; it may name "
            + ", ".join("{" + a + "}" for a in allowed)
        )


def fill(template: JsonValue, values: Mapping[str, str]) -> JsonValue:
    """`template` with every `{namespace.name}` in `values` replaced inside its strings."""
    if isinstance(template, str):

        def one(found: re.Match[str]) -> str:
            key = f"{found.group(1)}.{found.group(2)}"
            return values[key] if key in values else found.group(0)

        return PLACEHOLDER.sub(one, template)
    if isinstance(template, list):
        return [fill(v, values) for v in template]
    if isinstance(template, dict):
        return {k: fill(v, values) for k, v in template.items()}
    return template
