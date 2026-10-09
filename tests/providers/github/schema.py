"""GitHub's own machine-readable description read for what an answer, or a webhook's body, must carry: every field the
description requires, at any depth."""

from __future__ import annotations

import re

INDEX = re.compile(r"\[[0-9]+\]")


def resolve(components: dict[str, dict[str, dict[str, object]]], schema: dict[str, object]) -> dict[str, object]:
    while "$ref" in schema:
        reference = schema["$ref"]
        assert isinstance(reference, str)
        _, _, section, name = reference.split("/", 3)
        schema = components[section][name]
    return schema


def missing(
    components: dict[str, dict[str, dict[str, object]]],
    schema: dict[str, object],
    value: object,
    where: str,
    pending: dict[str, str] | None = None,
) -> list[str]:
    """Every field the description requires that `value` lacks, at any depth, by its path; those `pending` names (by
    path, an index shown as `[]`) are left out, each deliberately not answered."""
    left_out = pending or {}
    schema = resolve(components, schema)
    if value is None:
        return []
    for combined in ("oneOf", "anyOf"):
        if combined in schema:
            choices = schema[combined]
            assert isinstance(choices, list)
            each = [missing(components, choice, value, where, left_out) for choice in choices]
            return [] if any(not m for m in each) else min(each, key=len)
    if "allOf" in schema:
        parts = schema["allOf"]
        assert isinstance(parts, list)
        return [m for part in parts for m in missing(components, part, value, where, left_out)]
    if isinstance(value, list):
        items = schema["items"] if "items" in schema else {}
        assert isinstance(items, dict)
        return [m for n, item in enumerate(value) for m in missing(components, items, item, f"{where}[{n}]", left_out)]
    if not isinstance(value, dict):
        return []
    required = schema["required"] if "required" in schema else []
    properties = schema["properties"] if "properties" in schema else {}
    assert isinstance(required, list) and isinstance(properties, dict)
    gaps = [
        f"{where}.{name}"
        for name in required
        if name not in value and INDEX.sub("[]", f"{where}.{name}") not in left_out
    ]
    for name, held in value.items():
        if name in properties:
            gaps += missing(components, properties[name], held, f"{where}.{name}", left_out)
    return gaps
