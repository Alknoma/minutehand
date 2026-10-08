"""The JSON shape of what a service answers: a JSON Schema, read as far as a service's description uses it.

A shape is fixed for a route of a service the first time the route is answered (`shape_of` of that answer), or
before, from the service's OpenAPI document or a pin in the scenario; every later answer must conform (`problems`).
What is read: `type` (one or a list; OpenAPI's `nullable`), `enum`, `const`, `properties`, `required`, `items`,
`additionalProperties` (false, or a shape), `oneOf`, `anyOf`, `allOf`, and local `$ref`s (`#/...`) into the document
the shape came from. Anything else in a shape (`format`, `pattern`, `minimum`, descriptions) is left unread: it says
more than a shape fixed from one answer can know.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from pydantic import JsonValue

Shape = Mapping[str, JsonValue]

_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")


def problems(value: JsonValue, shape: JsonValue, document: JsonValue = None, *, at: str = "$") -> list[str]:
    """Every way `value` departs from `shape`, each naming where; empty when it conforms. `document` holds what a
    `$ref` points into (the shape itself when None)."""
    root = shape if document is None else document
    return _check(value, shape, root, at, 0)


def _check(value: JsonValue, shape: JsonValue, root: JsonValue, at: str, depth: int) -> list[str]:
    if depth > 64:
        return [f"{at}: the shape refers to itself too deeply to read"]
    if shape is True or shape is None:
        return []
    if shape is False:
        return [f"{at}: nothing is allowed here"]
    if not isinstance(shape, Mapping):
        return [f"{at}: the shape here is not a JSON Schema object"]
    if "$ref" in shape:
        return _check(value, _resolve(shape["$ref"], root), root, at, depth + 1)
    found: list[str] = []
    if "allOf" in shape and isinstance(shape["allOf"], list):
        for part in shape["allOf"]:
            found += _check(value, part, root, at, depth + 1)
    for key in ("oneOf", "anyOf"):
        parts = shape[key] if key in shape else None
        if isinstance(parts, list) and not any(not _check(value, part, root, at, depth + 1) for part in parts):
            found.append(f"{at}: matches none of the {len(parts)} shapes it may take")
    if "const" in shape and value != shape["const"]:
        found.append(f"{at}: is {_said(value)}, not {_said(shape['const'])}")
    if "enum" in shape and isinstance(shape["enum"], list) and value not in shape["enum"]:
        found.append(f"{at}: is {_said(value)}, not one of {_said(shape['enum'])}")
    wanted = _types(shape)
    if wanted and _type_of(value) not in wanted and not (_type_of(value) == "integer" and "number" in wanted):
        found.append(f"{at}: is {_type_of(value)}, not {' or '.join(wanted)}")
        return found
    if isinstance(value, dict):
        found += _object(value, shape, root, at, depth)
    if isinstance(value, list) and "items" in shape:
        for i, item in enumerate(value):
            found += _check(item, shape["items"], root, f"{at}[{i}]", depth + 1)
    return found


def _object(value: Mapping[str, JsonValue], shape: Shape, root: JsonValue, at: str, depth: int) -> list[str]:
    found: list[str] = []
    properties = shape["properties"] if "properties" in shape and isinstance(shape["properties"], Mapping) else {}
    required = shape["required"] if "required" in shape and isinstance(shape["required"], list) else []
    for name in required:
        if isinstance(name, str) and name not in value:
            found.append(f"{at}.{name}: is missing")
    extra = shape["additionalProperties"] if "additionalProperties" in shape else True
    for name, inner in value.items():
        if name in properties:
            found += _check(inner, properties[name], root, f"{at}.{name}", depth + 1)
        elif extra is False:
            found.append(f"{at}.{name}: is not a field this shape has")
        elif isinstance(extra, Mapping):
            found += _check(inner, extra, root, f"{at}.{name}", depth + 1)
    return found


def _types(shape: Shape) -> list[str]:
    said = shape["type"] if "type" in shape else None
    named = (
        [said] if isinstance(said, str) else [t for t in said if isinstance(t, str)] if isinstance(said, list) else []
    )
    if named and "nullable" in shape and shape["nullable"] is True:
        named.append("null")
    return [t for t in named if t in _TYPES]


def _type_of(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _resolve(ref: JsonValue, root: JsonValue) -> JsonValue:
    """What a local `$ref` (`#/components/schemas/Request`) points to; a shape that allows nothing when it points
    nowhere, so an answer is never let through a reference the document does not hold."""
    if not isinstance(ref, str) or not ref.startswith("#"):
        return False
    here = root
    for part in [p for p in ref[1:].split("/") if p]:
        name = part.replace("~1", "/").replace("~0", "~")
        if isinstance(here, Mapping) and name in here:
            here = here[name]
        else:
            return False
    return here


def _said(value: JsonValue) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "..."


def shape_of(value: JsonValue) -> JsonValue:
    """The shape one answer fixes for every later answer to its route: each object has exactly the fields it has
    here, each of the type it has here; a list's items take the shape of its first item, and an empty list's are
    left open; a field that is null here may later hold anything."""
    kind = _type_of(value)
    if kind == "null":
        return {}
    if kind == "object":
        assert isinstance(value, dict)
        return {
            "type": "object",
            "properties": {k: shape_of(v) for k, v in value.items()},
            "required": list(value),
            "additionalProperties": False,
        }
    if kind == "array":
        assert isinstance(value, list)
        return {"type": "array", "items": shape_of(value[0])} if value else {"type": "array"}
    if kind == "integer":  # enum-lint: exempt JSON Schema's own type names
        return {"type": "number"}
    return {"type": kind}


def values_at(value: JsonValue, path: str) -> list[JsonValue]:
    """Every value at a dotted path, `[*]` taking every item of a list: `items[*].status`. `$` alone is the value."""
    found: list[JsonValue] = [value]
    if path in ("", "$"):
        return found
    for part in path.removeprefix("$.").split("."):
        every = part.endswith("[*]")
        name = part.removesuffix("[*]")
        nxt: list[JsonValue] = []
        for here in found:
            if name and not (isinstance(here, dict) and name in here):
                continue
            inner = here[name] if name and isinstance(here, dict) else here
            if every:
                if isinstance(inner, list):
                    nxt += inner
            else:
                nxt.append(inner)
        found = nxt
    return found


def strings_in(value: JsonValue) -> Sequence[str]:
    """Every string anywhere in a JSON value, depth first."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in strings_in(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings_in(v)]
    return []
