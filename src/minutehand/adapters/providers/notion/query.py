"""A database query's `filter` and `sorts`: parsed against the database's schema, refused as Notion refuses
them, then applied to its rows.

Filters built: a property filter for title, rich_text, url, email, phone_number, number,
checkbox, select, status, multi_select, date, people, relation, created_time,
last_edited_time, created_by and last_edited_by properties; a timestamp filter on
`created_time` or `last_edited_time`; and `and` / `or` compounds nested at most two deep.
A condition, a property type or a filter type this does not know is a `validation_error`.

Text conditions compare case-insensitively, except `equals` and `does_not_equal`. A date
condition holding a date alone compares calendar days; a date-time compares instants.
Relative date conditions (`past_week`, `next_month`, `this_week`, ...) are measured from
the run's clock. Sorting puts empty values last in either direction, and orders a select
or status by the order of its options, as Notion does.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import Field, JsonValue

from minutehand.adapters.providers.notion import wire
from minutehand.domain.scenario import Model

TEXT = frozenset(
    {"equals", "does_not_equal", "contains", "does_not_contain", "starts_with", "ends_with", "is_empty", "is_not_empty"}
)
NUMBER = frozenset(
    {
        "equals",
        "does_not_equal",
        "greater_than",
        "less_than",
        "greater_than_or_equal_to",
        "less_than_or_equal_to",
        "is_empty",
        "is_not_empty",
    }
)
CHECKBOX = frozenset({"equals", "does_not_equal"})
SELECT = frozenset({"equals", "does_not_equal", "is_empty", "is_not_empty"})
CONTAINS = frozenset({"contains", "does_not_contain", "is_empty", "is_not_empty"})
RELATIVE = {
    "past_week": (-7, 0),
    "past_month": (-30, 0),
    "past_year": (-365, 0),
    "next_week": (0, 7),
    "next_month": (0, 30),
    "next_year": (0, 365),
}
DATE = frozenset(
    {"equals", "before", "after", "on_or_before", "on_or_after", "is_empty", "is_not_empty", "this_week", *RELATIVE}
)
TIMESTAMPS = (wire.PropertyType.CREATED_TIME, wire.PropertyType.LAST_EDITED_TIME)

CONDITIONS: dict[wire.PropertyType, frozenset[str]] = {
    wire.PropertyType.TITLE: TEXT,
    wire.PropertyType.RICH_TEXT: TEXT,
    wire.PropertyType.URL: TEXT,
    wire.PropertyType.EMAIL: TEXT,
    wire.PropertyType.PHONE_NUMBER: TEXT,
    wire.PropertyType.NUMBER: NUMBER,
    wire.PropertyType.CHECKBOX: CHECKBOX,
    wire.PropertyType.SELECT: SELECT,
    wire.PropertyType.STATUS: SELECT,
    wire.PropertyType.MULTI_SELECT: CONTAINS,
    wire.PropertyType.DATE: DATE,
    wire.PropertyType.PEOPLE: CONTAINS,
    wire.PropertyType.CREATED_BY: CONTAINS,
    wire.PropertyType.LAST_EDITED_BY: CONTAINS,
    wire.PropertyType.RELATION: CONTAINS,
    wire.PropertyType.CREATED_TIME: DATE,
    wire.PropertyType.LAST_EDITED_TIME: DATE,
}
MAX_DEPTH = 2


class Condition(Model):
    """One property or timestamp condition, checked."""

    name: str | None = Field(default=None, description="None for a timestamp filter")
    kind: wire.PropertyType
    condition: str
    value: JsonValue


class Compound(Model):
    every: bool = True
    parts: list[Condition | Compound]


Filter = Condition | Compound


def parse_filter(
    given: JsonValue, schema: dict[str, wire.Json], *, depth: int = 0, where: str = "body.filter"
) -> Filter:
    found = wire.as_object(given, where)
    compound = [k for k in ("and", "or") if k in found]
    if compound:
        if len(found) != 1:
            raise wire.invalid(f"{where} should hold `and` or `or` and nothing beside it.")
        if depth >= MAX_DEPTH:
            raise wire.invalid(f"{where}: compound filters may be nested at most {MAX_DEPTH} deep.")
        key = compound[0]
        parts = wire.as_list(found[key], f"{where}.{key}")
        return Compound(
            every=key == "and",
            parts=[parse_filter(p, schema, depth=depth + 1, where=f"{where}.{key}[{i}]") for i, p in enumerate(parts)],
        )
    if "timestamp" in found:
        which = wire.as_text(found["timestamp"], f"{where}.timestamp")
        kind = next((t for t in TIMESTAMPS if t.value == which), None)
        if kind is None:
            raise wire.invalid(f"{where}.timestamp should be created_time or last_edited_time; got `{which}`.")
        wire.only_keys(found, ["timestamp", which], where)
        if which not in found:
            raise wire.invalid(f"{where}.{which} should be defined.")
        return _condition(None, kind, found[which], f"{where}.{which}")
    if "property" not in found:
        raise wire.invalid(f"{where} should be a property filter, a timestamp filter, `and` or `or`.")
    key = wire.as_text(found["property"], f"{where}.property")
    by_id = {str(s["id"]): n for n, s in schema.items()}
    name = key if key in schema else by_id[key] if key in by_id else None
    if name is None:
        raise wire.invalid(f"{where}.property: `{key}` is not a property of this database.")
    kind = wire.schema_type(schema[name])
    typed = [k for k in found if k not in ("property", "type")]
    if typed != [kind.value]:
        shown = typed[0] if typed else "nothing"
        raise wire.invalid(f"{where}: `{name}` is a {kind.value} property, and the filter gives {shown}.")
    return _condition(name, kind, found[kind.value], f"{where}.{kind.value}")


def _condition(name: str | None, kind: wire.PropertyType, given: JsonValue, where: str) -> Condition:
    body = wire.as_object(given, where)
    if len(body) != 1:
        raise wire.invalid(f"{where} should hold exactly one condition.")
    condition, value = next(iter(body.items()))
    if condition not in CONDITIONS[kind]:
        raise wire.invalid(f"{where}.{condition} is not a condition of a {kind.value} filter.")
    at = f"{where}.{condition}"
    if condition in ("is_empty", "is_not_empty"):
        if value is not True:
            raise wire.invalid(f"{at} should be true.")
    elif condition in RELATIVE or condition == "this_week":
        wire.only_keys(wire.as_object(value, at), [], at)
    elif CONDITIONS[kind] is DATE:
        wire.read_date(wire.as_text(value, at), at)
    elif CONDITIONS[kind] is NUMBER:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise wire.invalid(f"{at} should be a number.")
    elif CONDITIONS[kind] is CHECKBOX:
        wire.as_bool(value, at)
    elif CONDITIONS[kind] is CONTAINS and kind is not wire.PropertyType.MULTI_SELECT:
        value = wire.canonical_id(wire.as_text(value, at), at)
    else:
        wire.as_text(value, at)
    return Condition(name=name, kind=kind, condition=condition, value=value)


class Row(Model):
    """What a filter or a sort reads of one row."""

    values: dict[str, wire.Json]
    created: datetime
    edited: datetime
    created_by: str
    edited_by: str


def matches(found: Filter, row: Row, now: datetime) -> bool:
    if isinstance(found, Compound):
        checks = (matches(p, row, now) for p in found.parts)
        return all(checks) if found.every else any(checks)
    if found.name is None:
        moment: JsonValue = (row.created if found.kind is wire.PropertyType.CREATED_TIME else row.edited).isoformat()
        return _date(found, moment, now)
    value = row.values[found.name]
    kind = found.kind
    raw = value[kind.value] if kind.value in value else None
    if kind is wire.PropertyType.CREATED_TIME:
        return _date(found, row.created.isoformat(), now)
    if kind is wire.PropertyType.LAST_EDITED_TIME:
        return _date(found, row.edited.isoformat(), now)
    if kind is wire.PropertyType.CREATED_BY:
        return _contains(found, [row.created_by])
    if kind is wire.PropertyType.LAST_EDITED_BY:
        return _contains(found, [row.edited_by])
    if CONDITIONS[kind] is TEXT:
        return _text(found, wire.value_text(value))
    if kind is wire.PropertyType.NUMBER:
        return _number(found, raw)
    if kind is wire.PropertyType.CHECKBOX:
        return (
            (raw is True) == (found.value is True)
            if found.condition == "equals"
            else (raw is True) != (found.value is True)
        )
    if kind in (wire.PropertyType.SELECT, wire.PropertyType.STATUS):
        chosen = str(raw["name"]) if isinstance(raw, dict) else None
        return _select(found, chosen)
    if kind is wire.PropertyType.MULTI_SELECT:
        names = [str(o["name"]) for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
        return _contains(found, names)
    if kind is wire.PropertyType.DATE:
        start = raw["start"] if isinstance(raw, dict) else None
        return _date(found, start, now)
    ids = [str(o["id"]) for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
    return _contains(found, ids)


def _text(found: Condition, text: str) -> bool:
    asked = str(found.value)
    folded, wanted = text.casefold(), asked.casefold()
    return {
        "equals": lambda: text == asked,
        "does_not_equal": lambda: text != asked,
        "contains": lambda: wanted in folded,
        "does_not_contain": lambda: wanted not in folded,
        "starts_with": lambda: folded.startswith(wanted),
        "ends_with": lambda: folded.endswith(wanted),
        "is_empty": lambda: text == "",
        "is_not_empty": lambda: text != "",
    }[found.condition]()


def _number(found: Condition, raw: JsonValue) -> bool:
    if found.condition == "is_empty":
        return raw is None
    if found.condition == "is_not_empty":
        return raw is not None
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        return found.condition == "does_not_equal"
    asked = found.value
    assert isinstance(asked, int | float)
    return {
        "equals": raw == asked,
        "does_not_equal": raw != asked,
        "greater_than": raw > asked,
        "less_than": raw < asked,
        "greater_than_or_equal_to": raw >= asked,
        "less_than_or_equal_to": raw <= asked,
    }[found.condition]


def _select(found: Condition, chosen: str | None) -> bool:
    return {
        "equals": chosen == found.value,
        "does_not_equal": chosen != found.value,
        "is_empty": chosen is None,
        "is_not_empty": chosen is not None,
    }[found.condition]


def _contains(found: Condition, held: list[str]) -> bool:
    return {
        "contains": found.value in held,
        "does_not_contain": found.value not in held,
        "is_empty": not held,
        "is_not_empty": bool(held),
    }[found.condition]


def _date(found: Condition, start: JsonValue, now: datetime) -> bool:
    if found.condition == "is_empty":
        return start is None
    if found.condition == "is_not_empty":
        return start is not None
    if not isinstance(start, str):
        return False
    held = wire.date_moment(start)
    if found.condition in RELATIVE:
        low, high = RELATIVE[found.condition]
        return now + timedelta(days=low) <= held <= now + timedelta(days=high)
    if found.condition == "this_week":
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return monday <= held < monday + timedelta(days=7)
    asked_text = str(found.value)
    asked = wire.date_moment(asked_text)
    if len(asked_text) == 10:
        day, wanted = held.date(), asked.date()
        compare: Callable[[], bool] = {
            "equals": lambda: day == wanted,
            "before": lambda: day < wanted,
            "after": lambda: day > wanted,
            "on_or_before": lambda: day <= wanted,
            "on_or_after": lambda: day >= wanted,
        }[found.condition]
        return compare()
    return {
        "equals": held == asked,
        "before": held < asked,
        "after": held > asked,
        "on_or_before": held <= asked,
        "on_or_after": held >= asked,
    }[found.condition]


# --------------------------------------------------------------------------- sorts


class Sort(Model):
    name: str | None = None
    timestamp: wire.PropertyType | None = None
    descending: bool = False


def parse_sorts(given: JsonValue, schema: dict[str, wire.Json]) -> list[Sort]:
    found: list[Sort] = []
    by_id = {str(s["id"]): n for n, s in schema.items()}
    for i, item in enumerate(wire.as_list(given, "body.sorts")):
        where = f"body.sorts[{i}]"
        sort = wire.as_object(item, where)
        wire.only_keys(sort, ["property", "timestamp", "direction"], where)
        direction = wire.as_text(sort["direction"], f"{where}.direction") if "direction" in sort else "ascending"
        if direction not in ("ascending", "descending"):
            raise wire.invalid(f"{where}.direction should be ascending or descending; got `{direction}`.")
        if ("property" in sort) == ("timestamp" in sort):
            raise wire.invalid(f"{where} should name a property or a timestamp, not both.")
        if "property" in sort:
            key = wire.as_text(sort["property"], f"{where}.property")
            name = key if key in schema else by_id[key] if key in by_id else None
            if name is None:
                raise wire.invalid(f"{where}.property: `{key}` is not a property of this database.")
            found.append(Sort(name=name, descending=direction == "descending"))
        else:
            which = wire.as_text(sort["timestamp"], f"{where}.timestamp")
            kind = next((t for t in TIMESTAMPS if t.value == which), None)
            if kind is None:
                raise wire.invalid(f"{where}.timestamp should be created_time or last_edited_time.")
            found.append(Sort(timestamp=kind, descending=direction == "descending"))
    return found


SortKey = tuple[float, str]


def sorted_rows(
    rows: list[Row], sorts: list[Sort], schema: dict[str, wire.Json], names: Callable[[str], str]
) -> list[Row]:
    """Rows in the order the sorts ask, the first sort deciding first; empty values last either way."""
    ordered = list(rows)
    for sort in reversed(sorts):
        filled = [r for r in ordered if _key(sort, r, schema, names) is not None]
        empty = [r for r in ordered if _key(sort, r, schema, names) is None]
        filled.sort(key=lambda r: _key(sort, r, schema, names) or (0.0, ""), reverse=sort.descending)
        ordered = filled + empty
    return ordered


def _key(sort: Sort, row: Row, schema: dict[str, wire.Json], names: Callable[[str], str]) -> SortKey | None:
    if sort.timestamp is not None or sort.name is None:
        moment = row.created if sort.timestamp is wire.PropertyType.CREATED_TIME else row.edited
        return (moment.timestamp(), "")
    value = row.values[sort.name]
    kind = wire.schema_type(schema[sort.name])
    raw = value[kind.value] if kind.value in value else None
    if kind is wire.PropertyType.NUMBER:
        return None if not isinstance(raw, int | float) or isinstance(raw, bool) else (float(raw), "")
    if kind is wire.PropertyType.CHECKBOX:
        return (1.0 if raw is True else 0.0, "")
    if kind in (wire.PropertyType.SELECT, wire.PropertyType.STATUS):
        if not isinstance(raw, dict):
            return None
        config = schema[sort.name][kind.value]
        listed = config["options"] if isinstance(config, dict) and "options" in config else []
        order = [o["name"] for o in listed if isinstance(o, dict)] if isinstance(listed, list) else []
        return (float(order.index(raw["name"])) if raw["name"] in order else float(len(order)), "")
    if kind is wire.PropertyType.DATE:
        return None if not isinstance(raw, dict) else (wire.date_moment(str(raw["start"])).timestamp(), "")
    if kind is wire.PropertyType.CREATED_TIME:
        return (row.created.timestamp(), "")
    if kind is wire.PropertyType.LAST_EDITED_TIME:
        return (row.edited.timestamp(), "")
    if kind is wire.PropertyType.PEOPLE:
        held = [names(str(o["id"])) for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
        return (0.0, held[0].casefold()) if held else None
    text = wire.value_text(value)
    return (0.0, text.casefold()) if text else None
