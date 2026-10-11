"""`minutehand mcp`: the command line, served over MCP.

Every command of the parser (`cli._parser`) is a tool of the same name, except those it marks `cli.OVER_MCP` false,
which serve until stopped; a command with commands of its own (`scenarios show`) is a tool per one, named with a
hyphen (`scenarios-show`). Each of a command's arguments is a parameter, named for its long flag (or its name, when
positional) and described by its help; a command that takes a program after `--` (`cli.TAKES_COMMAND`) takes it as
`command`. A call runs the command as a person types it (`python -m minutehand.cli <command> <arguments>`), in a
process of its own, and answers its exit code and what it printed.

Nothing here names a command or an option: one added to the command line is a tool, or a parameter, at once.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from minutehand import cli

COMMAND = "command"
STATE = "state"


@dataclass(frozen=True)
class Parameter:
    """One argument of a command, as a tool's parameter."""

    name: str
    flag: str | None  # None for a positional argument
    many: bool  # a list: nargs `*`/`+`, or a flag given once per value (`append`)
    repeated: bool  # the flag is given again before each value
    switch: bool  # takes no value: given or not
    schema: dict[str, object]
    required: bool


@dataclass(frozen=True)
class Tool:
    """One command: the words that name it, its parameters, and whether it takes a program after `--`."""

    words: tuple[str, ...]
    description: str
    parameters: tuple[Parameter, ...]
    takes_command: bool

    @property
    def name(self) -> str:
        return "-".join(self.words)

    def schema(self) -> dict[str, object]:
        properties: dict[str, object] = {p.name: p.schema for p in self.parameters}
        if self.takes_command:
            properties[COMMAND] = {
                "type": "array",
                "items": {"type": "string"},
                "description": "the program after `--`, one word each",
            }
        required = [p.name for p in self.parameters if p.required]
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    def argv(self, given: dict[str, object], state: Path) -> list[str]:
        """The command line this call is: the command's words, each argument as typed, the server's state directory
        when the call names none, and the program after `--`."""
        words = list(self.words)
        for p in self.parameters:
            value = given.get(p.name)
            if value is None or value is False:
                continue
            values = [str(v) for v in value] if isinstance(value, list) else [str(value)]
            if p.flag is None:
                words += values
            elif p.switch:
                words.append(p.flag)
            elif p.repeated:
                words += [w for v in values for w in (p.flag, v)]
            else:
                words += [p.flag, *values] if p.many else [f"{p.flag}={values[0]}"]
        if given.get(STATE) is None and any(p.name == STATE for p in self.parameters):
            words += ["--state", str(state)]
        program = given.get(COMMAND)
        if self.takes_command and isinstance(program, list) and program:
            words += ["--", *[str(w) for w in program]]
        return words


def _kind(action: argparse.Action) -> dict[str, object]:
    if action.choices is not None:
        return {"type": "string", "enum": [str(c) for c in action.choices]}
    if action.type is int:
        return {"type": "integer"}
    if action.type is float:
        return {"type": "number"}
    return {"type": "string"}


def _help(action: argparse.Action, prog: str) -> str:
    text = action.help or ""
    if text == argparse.SUPPRESS:
        return ""
    try:
        return text % {**vars(action), "prog": prog}
    except (KeyError, TypeError, ValueError):
        return text


def _parameter(action: argparse.Action, prog: str) -> Parameter:
    switch = action.nargs == 0
    repeated = isinstance(action, argparse._AppendAction)
    many = repeated or action.nargs in ("*", "+") or (isinstance(action.nargs, int) and action.nargs > 1)
    if action.option_strings:
        flag = max(action.option_strings, key=len)
        name = flag.lstrip("-").replace("-", "_")
    else:
        flag, name = None, action.dest
    if switch:
        schema: dict[str, object] = {"type": "boolean"}
    elif many:
        schema = {"type": "array", "items": _kind(action)}
    else:
        schema = _kind(action)
    described = _help(action, prog)
    if described:
        schema["description"] = described
    required = action.required if flag is not None else action.nargs not in ("?", "*")
    return Parameter(name, flag, many, repeated, switch, schema, bool(required))


def tools(parser: argparse.ArgumentParser) -> list[Tool]:
    """Every command the parser has that MCP serves, as a tool."""
    found: list[Tool] = []

    def walk(sub: argparse.ArgumentParser, words: tuple[str, ...], said: str) -> None:
        if sub.get_default(cli.OVER_MCP) is False:
            return
        own = [
            a
            for a in sub._actions
            if not isinstance(a, argparse._HelpAction | argparse._VersionAction | argparse._SubParsersAction)
            and a.dest != argparse.SUPPRESS
        ]
        nested = next((a for a in sub._actions if isinstance(a, argparse._SubParsersAction)), None)
        description = " ".join(part for part in (said, sub.description or "") if part)
        found.append(
            Tool(
                words,
                description,
                tuple(_parameter(a, sub.prog) for a in own),
                bool(sub.get_default(cli.TAKES_COMMAND)),
            )
        )
        if nested is not None:
            helps = {a.dest: a.help or "" for a in nested._choices_actions}
            for name, child in nested.choices.items():
                walk(child, (*words, name), helps.get(name, ""))

    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {a.dest: a.help or "" for a in commands._choices_actions}
    for name, sub in commands.choices.items():
        walk(sub, (name,), helps.get(name, ""))
    return found


async def run(argv: Sequence[str]) -> dict[str, object]:
    """The command run as typed, in a process of its own: its exit code, and what it printed to each stream."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "minutehand.cli",
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    return {
        "exit_code": process.returncode,
        "stdout": out.decode("utf-8", errors="replace"),
        "stderr": err.decode("utf-8", errors="replace"),
    }


INSTRUCTIONS = (
    "Each tool is the `minutehand` command of the same name, its parameters the command's flags; a call runs the "
    "command and answers its exit code and what it printed, exactly as on the command line.\n\n" + (cli.__doc__ or "")
)


def build(state: Path) -> Server:
    """The MCP server over one state directory: the parser's commands as tools."""
    server: Server = Server("minutehand", instructions=INSTRUCTIONS)
    served = {t.name: t for t in tools(cli._parser())}

    @server.list_tools()
    async def listed() -> list[types.Tool]:
        return [types.Tool(name=t.name, description=t.description, inputSchema=t.schema()) for t in served.values()]

    @server.call_tool()
    async def called(name: str, arguments: dict[str, object]) -> list[types.TextContent]:
        tool = served.get(name)
        if tool is None:
            raise ValueError(f"no command {name!r}: the tools are {', '.join(served)}")
        answered = await run(tool.argv(arguments, state))
        return [types.TextContent(type="text", text=json.dumps(answered))]

    return server


def serve(state: Path) -> None:
    """Serve the commands over stdio until the client closes the connection."""
    server = build(state)

    async def serving() -> None:
        async with stdio_server() as (reading, writing):
            await server.run(reading, writing, server.create_initialization_options())

    asyncio.run(serving())
