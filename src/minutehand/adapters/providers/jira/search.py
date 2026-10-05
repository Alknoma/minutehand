"""What a JQL query means on this site: which issues it matches and in what order.

Fields read: `project`, `status`, `statusCategory`, `assignee`, `reporter`, `creator`, `priority`,
`issuetype`/`type`, `resolution`, `labels`, `key`/`issuekey`/`id`, `parent`, `sprint`, `duedate`/`due`,
`created`/`createdDate`, `updated`/`updatedDate`, `resolved`/`resolutiondate`, `text`, `summary`,
`description`, `comment`, and a custom field as `cf[10016]` or by its name. Functions: `currentUser()`,
`now()`, `startOfDay()`, `endOfDay()`, `startOfWeek()`, `startOfMonth()`, `openSprints()`, `closedSprints()`.
A date is `yyyy-MM-dd` or `yyyy/MM/dd`, with an optional ` HH:mm`, or a relative offset (`-7d`, `2w`, `-4h`,
`-30m`, `1y`, `-2M`) from now on the run's clock.

As in Jira, `!=` and `NOT IN` never match an issue whose field is empty, a value that names nothing on the
site (a status, a user, a project) is a 400 naming it, and a query with no restriction is refused.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from minutehand.adapters.providers.jira import wire
from minutehand.adapters.providers.jira.jql import And, Clause, Node, Not, Op, Or, Query, Sort, Value, ValueKind
from minutehand.adapters.providers.jira.moves import Desk

_RELATIVE = re.compile(r"^([+-]?)(\d+)([yMwdhm])$")
_DATE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$")
_WORD = re.compile(r"[0-9a-z]+")
_EQUALITY = (Op.EQ, Op.NE, Op.IN, Op.NOT_IN, Op.IS, Op.IS_NOT)
_ORDERED = (Op.EQ, Op.NE, Op.IN, Op.NOT_IN, Op.IS, Op.IS_NOT, Op.GT, Op.GE, Op.LT, Op.LE)
_TEXT_OPS = (Op.LIKE, Op.NOT_LIKE, Op.IS, Op.IS_NOT)

Scalar = str | float | datetime | None


class Context:
    """One search: the site, who asks, and the run's now."""

    def __init__(self, desk: Desk, me: str, now: datetime) -> None:
        self.desk = desk
        self.world = desk.world
        self.site = desk.site()
        self.me = me
        self.now = now.astimezone(UTC)
        self._comments: dict[str, str] = {}

    def comments(self, issue: wire.StoredIssue) -> str:
        if issue.id not in self._comments:
            self._comments[issue.id] = " ".join(wire.adf_text(c.body) for c in self.world.comments(issue.id))
        return self._comments[issue.id]


def unbounded(query: Query) -> bool:
    return query.where is None


def matching(query: Query, issues: list[wire.StoredIssue], context: Context) -> list[wire.StoredIssue]:
    test = _compile(query.where, context) if query.where is not None else (lambda _: True)
    found = [i for i in issues if test(i)]
    return _ordered(found, query.order, context)


Test = Callable[[wire.StoredIssue], bool]


def _compile(node: Node, context: Context) -> Test:
    if isinstance(node, And):
        parts = [_compile(p, context) for p in node.parts]
        return lambda issue: all(p(issue) for p in parts)
    if isinstance(node, Or):
        parts = [_compile(p, context) for p in node.parts]
        return lambda issue: any(p(issue) for p in parts)
    if isinstance(node, Not):
        inner = _compile(node.operand, context)
        return lambda issue: not inner(issue)
    return _clause(node, context)


def _unknown_field(name: str) -> wire.Refusal:
    return wire.jql_error(f"Field '{name}' does not exist, or you are not allowed to see it.")


def _no_value(value: str, field: str) -> wire.Refusal:
    return wire.jql_error(f"The value '{value}' does not exist for the field '{field}'.")


def _bad_op(op: Op, field: str) -> wire.Refusal:
    return wire.jql_error(f"The operator '{op.value}' is not supported by the '{field}' field.")


def _clause(clause: Clause, context: Context) -> Test:
    name = clause.field.lower()
    site = context.site
    match name:
        case "project":  # enum-lint: exempt a JQL field name
            return _set_clause(clause, context, _projects(clause, context), lambda i: [i.project])
        case "status":  # enum-lint: exempt a JQL field name
            ids = _named(clause, "status", [(s.id, s.name) for s in site.statuses])
            return _set_clause(clause, context, ids, lambda i: [i.status])
        case "statuscategory":
            categories = [(c.value, wire.CATEGORY_NAME[c]) for c in wire.Category]
            categories += [(str(wire.CATEGORY_ID[c]), c.value) for c in wire.Category]
            wanted = _named(clause, "statusCategory", categories)
            keys = {
                wire.Category(v).value if v in {c.value for c in wire.Category} else _category_of(v) for v in wanted
            }
            return _set_clause(clause, context, keys, lambda i: [site.status(i.status).category.value])
        case "assignee" | "reporter" | "creator":  # enum-lint: exempt JQL field names
            ids = _users(clause, context, name)
            pick: dict[str, Callable[[wire.StoredIssue], list[str]]] = {
                "assignee": lambda i: [i.assignee] if i.assignee else [],
                "reporter": lambda i: [i.reporter],
                "creator": lambda i: [i.creator],
            }
            return _set_clause(clause, context, ids, pick[name])
        case "priority":
            ids = _named(clause, "priority", [(p.id, p.name) for p in site.priorities], ordered=True)
            return _set_clause(clause, context, ids, lambda i: [i.priority], ordered=_priority_rank(site))
        case "issuetype" | "type":
            ids = _named(clause, "issuetype", [(t.id, t.name) for t in site.issueTypes])
            return _set_clause(clause, context, ids, lambda i: [i.issuetype])
        case "resolution":
            values = [(r.id, r.name) for r in site.resolutions]
            if any(v.kind is ValueKind.TEXT and v.text.lower() == "unresolved" for v in clause.values):
                rest = [v for v in clause.values if not (v.kind is ValueKind.TEXT and v.text.lower() == "unresolved")]
                clause = clause.model_copy(update={"values": [*rest, Value(kind=ValueKind.EMPTY)]})
            ids = _named(clause, "resolution", values)
            return _set_clause(clause, context, ids, lambda i: [i.resolution] if i.resolution else [])
        case "labels":  # enum-lint: exempt JQL field names
            labels = {v.text for v in clause.values if v.kind is ValueKind.TEXT}
            if any(v.kind is ValueKind.FUNCTION for v in clause.values):
                raise _bad_op(clause.op, "labels")
            return _set_clause(clause, context, labels, lambda i: i.labels)
        case "key" | "issuekey" | "id":  # enum-lint: exempt JQL field names
            ids = _issues(clause, context)
            return _set_clause(clause, context, ids, lambda i: [i.id], ordered=lambda v: float(v))
        case "parent":
            ids = _issues(clause, context)
            return _set_clause(clause, context, ids, lambda i: [i.parent] if i.parent else [])
        case "sprint":
            ids = _sprints(clause, context)
            field = site.sprintField

            def sprints(issue: wire.StoredIssue) -> list[str]:
                value = issue.value(field)
                return [str(s) for s in value] if isinstance(value, list) else []

            return _set_clause(clause, context, ids, sprints)
        case "duedate" | "due":  # enum-lint: exempt JQL field names
            return _date_clause(clause, context, lambda i: _midnight(i.duedate) if i.duedate else None, "duedate")
        case "created" | "createddate":  # enum-lint: exempt JQL field names
            return _date_clause(clause, context, lambda i: i.created, "created")
        case "updated" | "updateddate":  # enum-lint: exempt JQL field names
            return _date_clause(clause, context, lambda i: i.updated, "updated")
        case "resolved" | "resolutiondate":
            return _date_clause(clause, context, lambda i: i.resolutiondate, "resolved")
        case "text":
            return _text_clause(
                clause,
                lambda i: " ".join([i.summary, wire.adf_text(i.description), context.comments(i), *_texts(i, site)]),
            )
        case "summary":  # enum-lint: exempt JQL field names
            return _text_clause(clause, lambda i: i.summary)
        case "description":  # enum-lint: exempt JQL field names
            return _text_clause(clause, lambda i: wire.adf_text(i.description))
        case "comment":  # enum-lint: exempt a JQL field name
            return _text_clause(clause, context.comments)
        case _:
            field = _custom(clause.field, site)
            if field is None:
                raise _unknown_field(clause.field)
            return _custom_clause(clause, context, field)


def _category_of(value: str) -> str:
    by_id = {str(wire.CATEGORY_ID[c]): c.value for c in wire.Category}
    return by_id.get(value, value)


def _texts(issue: wire.StoredIssue, site: wire.StoredSite) -> list[str]:
    kinds = (wire.CustomFieldType.STRING, wire.CustomFieldType.EPIC_LINK)
    return [str(v.value) for v in issue.custom if (f := site.field(v.field)) is not None and f.kind in kinds]


def _custom(name: str, site: wire.StoredSite) -> wire.StoredField | None:
    lowered = name.lower()
    if lowered.startswith("cf[") and lowered.endswith("]"):
        return site.field(f"customfield_{lowered[3:-1]}")
    return next((f for f in site.fields if f.name.lower() == lowered or f.id == lowered), None)


def _texts_of(clause: Clause, field: str) -> list[str]:
    found: list[str] = []
    for value in clause.values:
        if value.kind is ValueKind.FUNCTION:
            raise wire.jql_error(f"The function '{value.text}()' cannot be used with the field '{field}'.")
        if value.kind is ValueKind.TEXT:
            found.append(value.text)
    return found


def _named(clause: Clause, field: str, known: list[tuple[str, str]], *, ordered: bool = False) -> set[str]:
    """The ids a clause's values name, by id or by name in any case; a value naming nothing is a 400."""
    if clause.op not in (_ORDERED if ordered else _EQUALITY):
        raise _bad_op(clause.op, field)
    ids: set[str] = set()
    for text in _texts_of(clause, field):
        found = [i for i, name in known if text == i or text.lower() == name.lower()]
        if not found:
            raise _no_value(text, field)
        ids.update(found)
    return ids


def _projects(clause: Clause, context: Context) -> set[str]:
    if clause.op not in _EQUALITY:
        raise _bad_op(clause.op, "project")
    ids: set[str] = set()
    for text in _texts_of(clause, "project"):
        project = context.world.find_project(text) or next(
            (p for p in context.world.projects() if p.name.lower() == text.lower()), None
        )
        if project is None or not context.desk.can_browse(project, context.me):
            raise _no_value(text, "project")
        ids.add(project.id)
    return ids


def _users(clause: Clause, context: Context, field: str) -> set[str]:
    if clause.op not in _EQUALITY:
        raise _bad_op(clause.op, field)
    ids: set[str] = set()
    for value in clause.values:
        if value.kind is ValueKind.FUNCTION:
            if value.text.lower() != "currentuser":
                raise wire.jql_error(f"The function '{value.text}()' cannot be used with the field '{field}'.")
            ids.add(context.me)
        elif value.kind is ValueKind.TEXT:
            user = context.world.user(value.text) or context.world.user_by_email(value.text)
            if user is None:
                raise _no_value(value.text, field)
            ids.add(user.accountId)
    return ids


def _issues(clause: Clause, context: Context) -> set[str]:
    ids: set[str] = set()
    for text in _texts_of(clause, clause.field):
        issue = context.world.find_issue(text)
        if issue is None:
            if clause.op in (Op.EQ, Op.IN):
                raise wire.jql_error(f"An issue with key '{text}' does not exist for field '{clause.field}'.")
            continue
        ids.add(issue.id)
    return ids


def _sprints(clause: Clause, context: Context) -> set[str]:
    if clause.op not in _EQUALITY:
        raise _bad_op(clause.op, "sprint")
    ids: set[str] = set()
    sprints = context.world.sprints()
    for value in clause.values:
        if value.kind is ValueKind.FUNCTION:
            states = {"opensprints": wire.SprintState.ACTIVE, "closedsprints": wire.SprintState.CLOSED,
                      "futuresprints": wire.SprintState.FUTURE}  # fmt: skip
            state = states.get(value.text.lower())
            if state is None:
                raise wire.jql_error(f"The function '{value.text}()' cannot be used with the field 'sprint'.")
            ids |= {str(s.id) for s in sprints if s.state is state}
        elif value.kind is ValueKind.TEXT:
            found = {str(s.id) for s in sprints if value.text in (str(s.id), s.name)}
            if not found:
                raise _no_value(value.text, "sprint")
            ids |= found
    return ids


def _priority_rank(site: wire.StoredSite) -> Callable[[str], float]:
    order = [p.id for p in site.priorities]
    return lambda priority: float(len(order) - order.index(priority))


def _set_clause(
    clause: Clause,
    context: Context,
    wanted: set[str],
    values: Callable[[wire.StoredIssue], list[str]],
    *,
    ordered: Callable[[str], float] | None = None,
) -> Test:
    del context
    op = clause.op
    empty = any(v.kind is ValueKind.EMPTY for v in clause.values)
    if op in (Op.GT, Op.GE, Op.LT, Op.LE):
        if ordered is None or len(wanted) != 1:
            raise _bad_op(op, clause.field)
        bound = ordered(next(iter(wanted)))
        compare = _comparison(op)
        return lambda issue: any(compare(ordered(v), bound) for v in values(issue))
    if op in (Op.LIKE, Op.NOT_LIKE):
        raise _bad_op(op, clause.field)

    def test(issue: wire.StoredIssue) -> bool:
        held = values(issue)
        match op:
            case Op.EQ | Op.IN:
                return (empty and not held) or any(v in wanted for v in held)
            case Op.NE | Op.NOT_IN:
                if not held:
                    return False
                return not any(v in wanted for v in held)
            case Op.IS:
                return not held
            case _:
                return bool(held)

    if op in (Op.NE, Op.NOT_IN) and empty:

        def not_empty_either(issue: wire.StoredIssue) -> bool:
            held = values(issue)
            return bool(held) and not any(v in wanted for v in held)

        return not_empty_either
    return test


def _comparison(op: Op) -> Callable[[float, float], bool]:
    match op:
        case Op.GT:
            return lambda a, b: a > b
        case Op.GE:
            return lambda a, b: a >= b
        case Op.LT:
            return lambda a, b: a < b
        case _:
            return lambda a, b: a <= b


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


def moment(value: Value, context: Context, field: str) -> datetime:
    """A JQL date value as a moment on the run's clock."""
    now = context.now
    if value.kind is ValueKind.FUNCTION:
        today = _midnight(now.date())
        offset = _relative(value.args[0]) if value.args else timedelta(0)
        match value.text.lower():
            case "now":
                return now
            case "startofday":
                return today + offset
            case "endofday":
                return today + timedelta(days=1) - timedelta(milliseconds=1) + offset
            case "startofweek":
                return today - timedelta(days=(now.weekday() + 1) % 7) + offset
            case "startofmonth":
                return today.replace(day=1) + offset
            case _:
                raise wire.jql_error(f"The function '{value.text}()' cannot be used with the field '{field}'.")
    text = value.text.strip()
    relative = _RELATIVE.match(text)
    if relative is not None:
        return now + _relative(text)
    shape = _DATE.match(text)
    if shape is None:
        raise wire.jql_error(
            f"Date value '{text}' for field '{field}' is invalid: write 'yyyy/MM/dd HH:mm', 'yyyy-MM-dd HH:mm', "
            "'yyyy/MM/dd', 'yyyy-MM-dd', or a period such as '-5d' or '4w 2d'."
        )
    year, month, day, hour, minute = shape.groups()
    try:
        return datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), tzinfo=UTC)
    except ValueError as error:
        raise wire.jql_error(f"Date value '{text}' for field '{field}' is not a date.") from error


def _relative(text: str) -> timedelta:
    shape = _RELATIVE.match(text.strip())
    if shape is None:
        raise wire.jql_error(f"'{text}' is not a period such as '-5d'.")
    sign = -1 if shape.group(1) == "-" else 1
    amount = int(shape.group(2)) * sign
    unit = shape.group(3)
    days = {"y": 365, "M": 30, "w": 7, "d": 1}
    if unit in days:
        return timedelta(days=amount * days[unit])
    return timedelta(hours=amount) if unit == "h" else timedelta(minutes=amount)


def _date_clause(
    clause: Clause, context: Context, value: Callable[[wire.StoredIssue], datetime | None], field: str
) -> Test:
    if clause.op not in _ORDERED:
        raise _bad_op(clause.op, field)
    if clause.op in (Op.IS, Op.IS_NOT):
        wanted_empty = clause.op is Op.IS
        return lambda issue: (value(issue) is None) is wanted_empty
    bounds = [moment(v, context, field) for v in clause.values if v.kind is not ValueKind.EMPTY]
    empty = any(v.kind is ValueKind.EMPTY for v in clause.values)
    op = clause.op

    def test(issue: wire.StoredIssue) -> bool:
        held = value(issue)
        if held is None:
            return empty and op in (Op.EQ, Op.IN)
        match op:
            case Op.EQ | Op.IN:
                return held in bounds
            case Op.NE | Op.NOT_IN:
                return held not in bounds
            case _:
                return _comparison(op)(held.timestamp(), bounds[0].timestamp())

    return test


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _text_clause(clause: Clause, text: Callable[[wire.StoredIssue], str]) -> Test:
    if clause.op not in _TEXT_OPS:
        raise _bad_op(clause.op, clause.field)
    if clause.op in (Op.IS, Op.IS_NOT):
        wanted_empty = clause.op is Op.IS
        return lambda issue: (not text(issue).strip()) is wanted_empty
    value = clause.values[0]
    if value.kind is not ValueKind.TEXT:
        raise _bad_op(clause.op, clause.field)
    phrase = value.text.strip()
    exact = len(phrase) > 1 and phrase.startswith('"') and phrase.endswith('"')
    terms = _words(phrase)
    wildcard = phrase.endswith("*")
    if not terms:
        raise wire.jql_error(f"The text query '{value.text}' holds no word to search for.")

    def held(issue: wire.StoredIssue) -> bool:
        words = _words(text(issue))
        if exact:
            return " ".join(terms) in " ".join(words)
        found = set(words)
        return all(
            (t in found) or ((wildcard and t == terms[-1]) and any(w.startswith(t) for w in words)) for t in terms
        )

    if clause.op is Op.NOT_LIKE:
        return lambda issue: not held(issue)
    return held


def _custom_clause(clause: Clause, context: Context, field: wire.StoredField) -> Test:
    if field.kind is wire.CustomFieldType.NUMBER:

        def number(issue: wire.StoredIssue) -> list[str]:
            v = issue.value(field.id)
            return [str(float(v))] if isinstance(v, int | float) and not isinstance(v, bool) else []

        wanted: set[str] = set()
        for text in _texts_of(clause, field.name):
            try:
                wanted.add(str(float(text)))
            except ValueError as error:
                raise _no_value(text, field.name) from error
        return _set_clause(clause, context, wanted, number, ordered=float)
    if field.kind in (wire.CustomFieldType.STRING, wire.CustomFieldType.EPIC_LINK) and clause.op in (
        Op.LIKE,
        Op.NOT_LIKE,
    ):
        return _text_clause(clause, lambda i: str(i.value(field.id) or ""))
    if field.kind in (wire.CustomFieldType.OPTION, wire.CustomFieldType.OPTIONS):
        ids = _named(clause, field.name, [(o.id, o.value) for o in field.options])

        def options(issue: wire.StoredIssue) -> list[str]:
            v = issue.value(field.id)
            return [str(x) for x in v] if isinstance(v, list) else [str(v)] if v is not None else []

        return _set_clause(clause, context, ids, options)
    if field.kind is wire.CustomFieldType.SPRINT:
        return _clause(clause.model_copy(update={"field": "sprint"}), context)
    texts = set(_texts_of(clause, field.name))
    return _set_clause(clause, context, texts, lambda i: [str(i.value(field.id))] if i.value(field.id) else [])


def _ordered(issues: list[wire.StoredIssue], order: list[Sort], context: Context) -> list[wire.StoredIssue]:
    found = sorted(issues, key=lambda i: int(i.id))
    for sort in reversed(order):
        key = _sort_key(sort.field, context)
        present = [i for i in found if key(i) is not None]
        absent = [i for i in found if key(i) is None]
        present.sort(key=lambda i: _comparable(key(i)), reverse=not sort.ascending)
        found = present + absent if sort.ascending else absent + present
    return found


def _comparable(value: Scalar) -> tuple[float, str]:
    if isinstance(value, datetime):
        return value.timestamp(), ""
    if isinstance(value, float):
        return value, ""
    return 0.0, str(value or "")


def _sort_key(name: str, context: Context) -> Callable[[wire.StoredIssue], Scalar]:
    site = context.site
    rank = _priority_rank(site)
    lowered = name.lower()
    keys: dict[str, Callable[[wire.StoredIssue], Scalar]] = {
        "created": lambda i: i.created,
        "updated": lambda i: i.updated,
        "duedate": lambda i: _midnight(i.duedate) if i.duedate else None,
        "resolutiondate": lambda i: i.resolutiondate,
        "resolved": lambda i: i.resolutiondate,
        "priority": lambda i: rank(i.priority),
        "summary": lambda i: i.summary.lower(),
        "status": lambda i: site.status(i.status).name.lower(),
        "issuetype": lambda i: site.issue_type(i.issuetype).name.lower(),
        "key": lambda i: float(int(i.id)),
        "id": lambda i: float(int(i.id)),
        "assignee": lambda i: _display(context, i.assignee),
    }
    if lowered in keys:
        return keys[lowered]
    field = _custom(name, site)
    if field is None:
        raise wire.jql_error(f"Field '{name}' does not exist, or you cannot order by it.")

    def custom(issue: wire.StoredIssue) -> Scalar:
        v = issue.value(field.id)
        if isinstance(v, int | float) and not isinstance(v, bool):
            return float(v)
        return None if v is None else str(v)

    return custom


def _display(context: Context, account: str | None) -> str | None:
    user = context.world.user(account) if account is not None else None
    return user.displayName.lower() if user is not None else None
