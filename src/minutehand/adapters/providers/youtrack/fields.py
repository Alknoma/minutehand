"""Custom fields: the instance's standard register, the shapes a project is born with, and what a value written to
a field may be.

Every seeded project carries the full standard set (State, Priority, Type, Assignee, Due Date, Estimation, Spent
time, Story Points). A project made through `POST /admin/projects` carries YouTrack's default template: Priority,
Type, State and Assignee, and no Due Date, which a client attaches afterwards.
"""

from __future__ import annotations

import re

from minutehand.adapters.providers.youtrack import wire
from minutehand.domain.scenario import Model, TicketState

MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 8
DAYS_PER_WEEK = 5
"""YouTrack's default time-tracking settings: a working day of eight hours and a week of five days."""


class Definition(Model):
    id: str
    name: str
    type: wire.FieldType
    empty_text: str
    can_be_empty: bool = True


STATE = Definition(id="58-1", name="State", type=wire.FieldType.STATE, empty_text="No state", can_be_empty=False)
ASSIGNEE = Definition(id="58-2", name="Assignee", type=wire.FieldType.USER, empty_text="Unassigned")
PRIORITY = Definition(
    id="58-3", name="Priority", type=wire.FieldType.ENUM, empty_text="No priority", can_be_empty=False
)
TYPE = Definition(id="58-4", name="Type", type=wire.FieldType.ENUM, empty_text="No type", can_be_empty=False)
DUE_DATE = Definition(id="58-5", name="Due Date", type=wire.FieldType.DATE, empty_text="No due date")
ESTIMATION = Definition(id="58-6", name="Estimation", type=wire.FieldType.PERIOD, empty_text="No estimation")
SPENT_TIME = Definition(id="58-7", name="Spent time", type=wire.FieldType.PERIOD, empty_text="No spent time")
STORY_POINTS = Definition(id="58-8", name="Story Points", type=wire.FieldType.FLOAT, empty_text="?")

STANDARD = [STATE, ASSIGNEE, PRIORITY, TYPE, DUE_DATE, ESTIMATION, SPENT_TIME, STORY_POINTS]
"""The fields every instance defines, in YouTrack's own order."""
SEEDED_SET = [PRIORITY, TYPE, STATE, ASSIGNEE, DUE_DATE, ESTIMATION, SPENT_TIME, STORY_POINTS]
"""What a seeded project carries unless its seed says otherwise."""
TEMPLATE_SET = [PRIORITY, TYPE, STATE, ASSIGNEE]
"""What YouTrack's default template gives a project made through the API: no Due Date."""


class Value(Model):
    """One value a bundle starts with."""

    name: str
    resolved: bool = False
    outcome: TicketState = TicketState.OPEN


SEEDED_STATES = [
    Value(name="Open"),
    Value(name="In Progress"),
    Value(name="Fixed", resolved=True, outcome=TicketState.DONE),
    Value(name="Won't fix", resolved=True, outcome=TicketState.CANCELLED),
]
TEMPLATE_STATES = [
    Value(name="Submitted"),
    Value(name="Open"),
    Value(name="In Progress"),
    Value(name="To be discussed"),
    Value(name="Reopened"),
    Value(name="Can't Reproduce", resolved=True, outcome=TicketState.CANCELLED),
    Value(name="Duplicate", resolved=True, outcome=TicketState.CANCELLED),
    Value(name="Fixed", resolved=True, outcome=TicketState.DONE),
    Value(name="Won't fix", resolved=True, outcome=TicketState.CANCELLED),
    Value(name="Incomplete", resolved=True, outcome=TicketState.CANCELLED),
    Value(name="Obsolete", resolved=True, outcome=TicketState.CANCELLED),
    Value(name="Verified", resolved=True, outcome=TicketState.DONE),
]
PRIORITIES = [Value(name=n) for n in ("Show-stopper", "Critical", "Major", "Normal", "Minor")]
TYPES = [
    Value(name=n)
    for n in ("Bug", "Cosmetics", "Exception", "Feature", "Task", "Usability Problem", "Performance Problem", "Epic")
]
DEFAULTS: dict[str, str] = {"State": "Open", "Priority": "Normal", "Type": "Bug"}
TEMPLATE_DEFAULTS: dict[str, str] = {"State": "Submitted", "Priority": "Normal", "Type": "Bug"}
STANDARD_VALUES: dict[str, list[Value]] = {"State": SEEDED_STATES, "Priority": PRIORITIES, "Type": TYPES}


class Ids:
    """Fresh database ids for what lives inside a project's body: its fields, their bundles and bundle values.
    Each prefix counts on from the highest number already used anywhere in the instance."""

    def __init__(self, projects: list[wire.StoredProject]) -> None:
        self._next: dict[str, int] = {"92": 0, "60": 0, "62": 0}
        for project in projects:
            for field in project.fields:
                self._see(field.id)
                if field.bundle is not None:
                    self._see(field.bundle)
                for value in field.values:
                    self._see(value.id)

    def _see(self, entity_id: str) -> None:
        prefix, _, number = entity_id.partition("-")
        if prefix in self._next and number.isdigit():
            self._next[prefix] = max(self._next[prefix], int(number))

    def take(self, prefix: str) -> str:
        self._next[prefix] += 1
        return f"{prefix}-{self._next[prefix]}"


def project_field(
    ids: Ids,
    definition: wire.StoredFieldDefinition,
    *,
    values: list[Value] | None,
    can_be_empty: bool,
    empty_text: str,
    default: str | None,
) -> wire.StoredProjectField:
    """One field as a project carries it, with its own bundle when its type draws values from one."""
    bundle: str | None = None
    stored: list[wire.StoredBundleValue] = []
    if definition.fieldType in wire.BUNDLED or definition.fieldType is wire.FieldType.USER:
        bundle = ids.take("60")
    if definition.fieldType in wire.BUNDLED:
        stored = [
            wire.StoredBundleValue(
                id=ids.take("62"),
                name=v.name,
                ordinal=n,
                isResolved=v.resolved if definition.fieldType is wire.FieldType.STATE else False,
                outcome=v.outcome,
            )
            for n, v in enumerate(values or [])
        ]
    chosen = next((v.id for v in stored if default is not None and v.name.lower() == default.lower()), None)
    if default is not None and chosen is None:
        raise ValueError(f"{definition.name}'s default {default!r} is not one of its values")
    return wire.StoredProjectField(
        id=ids.take("92"),
        field=definition.id,
        bundle=bundle,
        values=stored,
        canBeEmpty=can_be_empty,
        emptyFieldText=empty_text,
        defaultValue=chosen,
    )


def standard_field(ids: Ids, definition: Definition, *, template: bool) -> wire.StoredProjectField:
    """A standard field with its standard bundle and default, as a seeded project or the default template has it."""
    values = TEMPLATE_STATES if template and definition is STATE else STANDARD_VALUES.get(definition.name)
    defaults = TEMPLATE_DEFAULTS if template else DEFAULTS
    return project_field(
        ids,
        stored_definition(definition),
        values=values,
        can_be_empty=definition.can_be_empty,
        empty_text=definition.empty_text,
        default=defaults.get(definition.name),
    )


def stored_definition(definition: Definition) -> wire.StoredFieldDefinition:
    return wire.StoredFieldDefinition(id=definition.id, name=definition.name, fieldType=definition.type)


# --------------------------------------------------------------------------- periods

_PERIOD = re.compile(r"^\s*(?:(\d+)w)?\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*$")


def period_minutes(presentation: str) -> int | None:
    """`1w 2d 3h 30m` in minutes, by YouTrack's default working week; None when it is not that shape."""
    match = _PERIOD.match(presentation)
    if match is None or not any(match.groups()):
        return None
    weeks, days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return ((weeks * DAYS_PER_WEEK + days) * HOURS_PER_DAY + hours) * MINUTES_PER_HOUR + minutes


def period_presentation(minutes: int) -> str:
    """Minutes as YouTrack presents a period: `1w 2d 3h 30m`, dropping what is zero, `0m` for nothing."""
    per_day = HOURS_PER_DAY * MINUTES_PER_HOUR
    per_week = DAYS_PER_WEEK * per_day
    parts: list[str] = []
    for size, unit in ((per_week, "w"), (per_day, "d"), (MINUTES_PER_HOUR, "h"), (1, "m")):
        count, minutes = divmod(minutes, size)
        if count:
            parts.append(f"{count}{unit}")
    return " ".join(parts) or "0m"
