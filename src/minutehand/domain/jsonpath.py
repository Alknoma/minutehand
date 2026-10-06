"""JSONPath (RFC 9535) for reading values out of a JSON answer, in the subset a declaration needs.

    $.items[*]          every item of `items`
    $.id                one member
    $['odd key']        a member whose name is not an identifier
    $.data[0].name      an index; negative counts from the end
    $..id               every `id` at any depth
    $.items[*].actions  a member of each item

Supported: the root `$`; child segments by name (`.name`, `['name']`, `["name"]`), index (`[0]`, `[-1]`),
wildcard (`.*`, `[*]`) and several selectors in one bracket (`['a', 'b']`, `[0, 2]`); descendant segments (`..`)
with the same selectors. Refused at load with what is not supported: slices (`[1:3]`) and filter selectors
(`[?...]`), which no declaration here has needed. A query returns a node list, in document order, possibly empty.

The capture and emulator declarations (`domain.outbound.BodyPath`, `domain.emulator.BodyPath`) read an older
dotted syntax without `$`; it is listed as debt in `docs/agent-contract.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator, Field

_NAME = re.compile(r"[A-Za-z_\u0080-￿][A-Za-z0-9_\u0080-￿]*")
_INT = re.compile(r"-?(0|[1-9][0-9]*)")


@dataclass(frozen=True)
class _Selector:
    name: str | None = None
    index: int | None = None
    wildcard: bool = False


@dataclass(frozen=True)
class _Segment:
    selectors: tuple[_Selector, ...]
    descendant: bool


class JsonPathError(ValueError):
    """A query that is not JSONPath, or uses what this reader does not support."""


def _string(text: str, at: int) -> tuple[str, int]:
    quote = text[at]
    out: list[str] = []
    i = at + 1
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            following = text[i + 1]
            out.append({"n": "\n", "t": "\t", "b": "\b", "f": "\f", "r": "\r", "/": "/"}.get(following, following))
            i += 2
            continue
        if c == quote:
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise JsonPathError(f"{text!r}: a quoted name at {at} is never closed")


def _bracket(text: str, at: int) -> tuple[tuple[_Selector, ...], int]:
    """Selectors inside `[...]` starting just after `[`."""
    selectors: list[_Selector] = []
    i = at
    while True:
        while i < len(text) and text[i] == " ":
            i += 1
        if i >= len(text):
            raise JsonPathError(f"{text!r}: a bracket is never closed")
        c = text[i]
        if c in "'\"":
            name, i = _string(text, i)
            selectors.append(_Selector(name=name))
        elif c == "*":
            selectors.append(_Selector(wildcard=True))
            i += 1
        elif c == "?":
            raise JsonPathError(f"{text!r}: filter selectors ([?...]) are not supported here")
        else:
            found = _INT.match(text, i)
            if found is None:
                raise JsonPathError(f"{text!r}: expected a name in quotes, an index or * at {i}")
            i = found.end()
            if i < len(text) and text[i] == ":":
                raise JsonPathError(f"{text!r}: slices ([start:end]) are not supported here")
            selectors.append(_Selector(index=int(found.group())))
        while i < len(text) and text[i] == " ":
            i += 1
        if i < len(text) and text[i] == ",":
            i += 1
            continue
        if i < len(text) and text[i] == "]":
            return tuple(selectors), i + 1
        raise JsonPathError(f"{text!r}: expected , or ] at {i}")


def parse(text: str) -> tuple[_Segment, ...]:
    """The segments of a query, or `JsonPathError` naming what is wrong."""
    if not text.startswith("$"):
        raise JsonPathError(f"{text!r}: a JSONPath query starts at the root, $")
    segments: list[_Segment] = []
    i = 1
    while i < len(text):
        descendant = text.startswith("..", i)
        if descendant:
            i += 2
            if i < len(text) and text[i] == "[":
                selectors, i = _bracket(text, i + 1)
                segments.append(_Segment(selectors, True))
                continue
        elif text[i] == ".":
            i += 1
        elif text[i] == "[":
            selectors, i = _bracket(text, i + 1)
            segments.append(_Segment(selectors, False))
            continue
        else:
            raise JsonPathError(f"{text!r}: expected . or [ at {i}")
        if i < len(text) and text[i] == "*":
            segments.append(_Segment((_Selector(wildcard=True),), descendant))
            i += 1
            continue
        found = _NAME.match(text, i)
        if found is None:
            raise JsonPathError(f"{text!r}: expected a member name at {i}")
        segments.append(_Segment((_Selector(name=found.group()),), descendant))
        i = found.end()
    return tuple(segments)


def _children(value: object) -> list[object]:
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return list(value)
    return []


def _select(value: object, selector: _Selector) -> list[object]:
    if selector.wildcard:
        return _children(value)
    if selector.name is not None:
        return [value[selector.name]] if isinstance(value, dict) and selector.name in value else []
    assert selector.index is not None
    if isinstance(value, list) and -len(value) <= selector.index < len(value):
        return [value[selector.index]]
    return []


def _descendants(value: object) -> list[object]:
    found = [value]
    for child in _children(value):
        found += _descendants(child)
    return found


def query(document: object, path: str) -> list[object]:
    """The node list `path` selects in `document`, in order."""
    nodes: list[object] = [document]
    for segment in parse(path):
        following: list[object] = []
        for node in nodes:
            for visited in _descendants(node) if segment.descendant else [node]:
                for selector in segment.selectors:
                    following += _select(visited, selector)
        nodes = following
    return nodes


def first(document: object, path: str) -> object | None:
    found = query(document, path)
    return found[0] if found else None


def _checked(text: str) -> str:
    parse(text)
    return text


JsonPath = Annotated[str, Field(min_length=1, description="A JSONPath query (RFC 9535)"), AfterValidator(_checked)]
"""A JSONPath query (RFC 9535, in the subset `parse` reads), checked when a declaration is loaded."""
