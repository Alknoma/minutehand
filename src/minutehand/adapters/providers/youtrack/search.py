"""What a parsed query matches, decided against the instance as the caller sees it.

A value nothing in scope has is refused with YouTrack's `invalid_query`, never answered with nothing: a filter the
tracker cannot read and a filter nothing matches are different answers, and a caller that reads the first as the
second reports work as missing that is there.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from minutehand.adapters.providers.youtrack import fields, state, wire
from minutehand.adapters.providers.youtrack.query import (
    ME,
    UNASSIGNED,
    Clause,
    Conjunction,
    Item,
    Keyword,
    Search,
    Shortcut,
    SortKey,
)
from minutehand.adapters.providers.youtrack.state import YouTrackWorld

_RESOLVED = "resolved"
_UNRESOLVED = "unresolved"

Test = Callable[[wire.StoredIssue], bool]


class Matcher:
    def __init__(self, world: YouTrackWorld, caller: wire.StoredUser, now: datetime, text: str) -> None:
        self._world = world
        self._caller = caller
        self._now = now
        self._text = text
        self._definitions = {d.name: d for d in world.definitions()}
        self._projects = {p.id: p for p in world.projects()}
        self._comments: dict[str, str] = {}

    def field_names(self) -> list[str]:
        return list(self._definitions)

    def matching(self, search: Search, scope: list[wire.StoredProject]) -> list[wire.StoredIssue]:
        """The issues in `scope` the query matches, in the order it sorts them by (oldest first by default)."""
        issues = [i for p in scope for i in self._world.issues(p.id)]
        tests = [self._conjunction(c, scope) for c in search.alternatives]
        matched = [i for i in issues if any(all(t(i) for t in test) for test in tests)]
        matched.sort(key=lambda i: (i.created, state.ordinal(i.id)))
        for key in reversed(search.sort):
            matched = self._sorted(matched, key)
        return matched

    # ------------------------------------------------------------------ terms

    def _conjunction(self, conjunction: Conjunction, everywhere: list[wire.StoredProject]) -> list[Test]:
        scope = everywhere
        tests: list[Test] = []
        for clause in conjunction.clauses:
            if clause.keyword is Keyword.PROJECT:
                wanted = self._projects_named(clause)
                scope = [p for p in scope if p.id in wanted]
                tests.append(lambda i, wanted=wanted: i.project in wanted)
        for clause in conjunction.clauses:
            if clause.keyword is not Keyword.PROJECT:
                tests.append(self._clause(clause, scope))
        tests += [self._shortcut(s, scope) for s in conjunction.shortcuts]
        for word in conjunction.words:
            needle = word.text.lower()
            negated = word.negated
            tests.append(lambda i, n=needle, neg=negated: (n in self._haystack(i)) != neg)
        return tests

    def _haystack(self, issue: wire.StoredIssue) -> str:
        if issue.id not in self._comments:
            said = " ".join(c.text for c in self._world.comments(issue.id))
            self._comments[issue.id] = " ".join(
                [issue.summary, issue.description or "", issue.idReadable, said]
            ).lower()
        return self._comments[issue.id]

    def _projects_named(self, clause: Clause) -> set[str]:
        ids: set[str] = set()
        for item in clause.items:
            named = self._world.project_named(item.text)
            if named is None or item.empty or item.upper is not None:
                raise wire.invalid_query(item.text, "project")
            ids.add(named.id)
        return ids

    def _clause(self, clause: Clause, scope: list[wire.StoredProject]) -> Test:
        keyword = clause.keyword
        if keyword is Keyword.ISSUE_ID:
            wanted = {item.text.lower() for item in clause.items}
            return lambda i: i.idReadable.lower() in wanted or i.id in wanted
        if keyword is Keyword.TAG:
            return self._tags(clause)
        if keyword is Keyword.FOR:
            field = self._world.definition_named(state.ASSIGNEE_FIELD)
            if field is None:
                raise wire.invalid_query(clause.items[0].text, "for")
            return self._users(clause, lambda i: self._field_value(i, field.name), field.name)
        if keyword is Keyword.REPORTER:
            return self._users(clause, lambda i: i.reporter, "reporter")
        if keyword is Keyword.CREATED:
            return self._dates(clause, lambda i: i.created, clause.attribute)
        if keyword is Keyword.UPDATED:
            return self._dates(clause, lambda i: i.updated, clause.attribute)
        if keyword is Keyword.RESOLVED_DATE:
            return self._dates(clause, lambda i: i.resolved, clause.attribute)
        if keyword in (Keyword.SUMMARY, Keyword.DESCRIPTION):
            read: Callable[[wire.StoredIssue], str] = (
                (lambda i: i.summary) if keyword is Keyword.SUMMARY else (lambda i: i.description or "")
            )
            wanted_text = [item.text.lower() for item in clause.items]
            return lambda i: any(w in read(i).lower() for w in wanted_text)
        if keyword is Keyword.HAS:
            return self._has(clause, scope)
        return self._custom(clause, scope)

    def _tags(self, clause: Clause) -> Test:
        tests: list[Test] = []
        for item in clause.items:
            if item.empty:
                tests.append(lambda i: not i.tags)
                continue
            tag = self._world.tag_named(item.text)
            if tag is None:
                raise wire.invalid_query(item.text, "tag")
            tests.append(lambda i, t=tag.id, neg=item.negated: (t in i.tags) != neg)
        return _any(tests, clause.items)

    def _user_named(self, text: str, attribute: str) -> str | None:
        """A user's id as a query names them; None for `Unassigned`."""
        if text.lower() == ME:
            return self._caller.id
        if text.lower() == UNASSIGNED:
            return None
        found = self._world.user_named(text)
        if found is None:
            raise wire.invalid_query(text, attribute)
        return found.id

    def _users(
        self, clause: Clause, read: Callable[[wire.StoredIssue], wire.FieldValue | None], attribute: str
    ) -> Test:
        tests: list[Test] = []
        for item in clause.items:
            wanted = None if item.empty else self._user_named(item.text, attribute)
            tests.append(lambda i, w=wanted, neg=item.negated: (read(i) == w) != neg)
        return _any(tests, clause.items)

    def _field_value(self, issue: wire.StoredIssue, name: str) -> wire.FieldValue | None:
        project = self._projects.get(issue.project)
        field = None if project is None else self._world.project_field(project, name)
        return None if field is None else issue.values.get(field.id)

    def _custom(self, clause: Clause, scope: list[wire.StoredProject]) -> Test:
        definition = self._definitions[clause.attribute]
        carried = [(p, f) for p in scope for f in p.fields if f.field == definition.id]
        kind = definition.fieldType
        if kind is wire.FieldType.USER:
            return self._users(clause, lambda i: self._field_value(i, definition.name), definition.name)
        if kind in wire.DATED:

            def read_date(i: wire.StoredIssue) -> int | None:
                value = self._field_value(i, definition.name)
                return value if isinstance(value, int) else None

            return self._dates(clause, read_date, definition.name)
        tests: list[Test] = []
        for item in clause.items:
            if item.empty:
                tests.append(lambda i, neg=item.negated: (self._field_value(i, definition.name) is None) != neg)
                continue
            if kind in wire.BUNDLED:
                ids = {v.id for _, f in carried for v in f.values if v.name.lower() == item.text.lower()}
                if not ids:
                    raise wire.invalid_query(item.text, definition.name)
                tests.append(lambda i, ids=ids, neg=item.negated: (self._field_value(i, definition.name) in ids) != neg)
                continue
            if not carried:
                raise wire.invalid_query(item.text, definition.name)
            tests.append(self._scalar(definition, item))
        return _any(tests, clause.items)

    def _scalar(self, definition: wire.StoredFieldDefinition, item: Item) -> Test:
        kind = definition.fieldType

        def number(text: str) -> float | None:
            if text == "*":
                return None
            if kind is wire.FieldType.PERIOD:
                minutes = fields.period_minutes(text)
                if minutes is None:
                    raise wire.invalid_query(text, definition.name)
                return float(minutes)
            try:
                return float(text)
            except ValueError as error:
                raise wire.invalid_query(text, definition.name) from error

        if kind is wire.FieldType.STRING:
            wanted = item.text.lower()
            return lambda i: (str(self._field_value(i, definition.name) or "").lower() == wanted) != item.negated
        low = number(item.text)
        high = number(item.upper) if item.upper is not None else low

        def holds(i: wire.StoredIssue) -> bool:
            value = self._field_value(i, definition.name)
            if value is None or isinstance(value, str):
                return False
            inside = (low is None or value >= low) and (high is None or value <= high)
            return inside != item.negated

        return holds

    def _has(self, clause: Clause, scope: list[wire.StoredProject]) -> Test:
        tests: list[Test] = []
        for item in clause.items:
            definition = next((d for d in self._definitions.values() if d.name.lower() == item.text.lower()), None)
            if definition is None:
                raise wire.invalid_query(item.text, "has")
            tests.append(
                lambda i, name=definition.name, neg=item.negated: (self._field_value(i, name) is not None) != neg
            )
        return lambda i: all(t(i) for t in tests)

    def _shortcut(self, shortcut: Shortcut, scope: list[wire.StoredProject]) -> Test:
        name = shortcut.name.lower()
        negated = shortcut.negated
        if name in (_RESOLVED, _UNRESOLVED):
            wanted = name == _RESOLVED

            def resolved(i: wire.StoredIssue) -> bool:
                project = self._projects.get(i.project)
                return project is not None and self._world.is_resolved(project, i)

            return lambda i: (resolved(i) == wanted) != negated
        if name == ME:
            me = self._caller.id
            users = [d.name for d in self._definitions.values() if d.fieldType is wire.FieldType.USER]
            return lambda i: any(self._field_value(i, u) == me for u in users) != negated
        ids = {v.id for p in scope for f in p.fields for v in f.values if v.name.lower() == name}
        if not ids:
            raise wire.invalid_query(shortcut.name, "#")
        return lambda i: any(v in ids for v in i.values.values()) != negated

    # ------------------------------------------------------------------ dates

    def _dates(self, clause: Clause, read: Callable[[wire.StoredIssue], int | None], attribute: str) -> Test:
        tests: list[Test] = []
        for item in clause.items:
            if item.empty:
                tests.append(lambda i, neg=item.negated: (read(i) is None) != neg)
                continue
            low = self._moment(item.text, attribute, end=False)
            high = self._moment(item.upper if item.upper is not None else item.text, attribute, end=True)

            def inside(
                i: wire.StoredIssue, low: int | None = low, high: int | None = high, neg: bool = item.negated
            ) -> bool:
                value = read(i)
                if value is None:
                    return False
                return ((low is None or value >= low) and (high is None or value <= high)) != neg

            tests.append(inside)
        return _any(tests, clause.items)

    def _moment(self, text: str, attribute: str, *, end: bool) -> int | None:
        """The first or last millisecond a date value names; None for `*`."""
        if text == "*":
            return None
        today = self._now.astimezone(UTC).date()
        relative = {"today": today, "yesterday": today - timedelta(days=1), "tomorrow": today + timedelta(days=1)}
        day = relative.get(text.lower())
        if day is None:
            try:
                if "T" in text:
                    moment = datetime.fromisoformat(text)
                    moment = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
                    return state.millis(moment)
                day = date.fromisoformat(text)
            except ValueError as error:
                raise wire.invalid_query(text, attribute) from error
        edge = datetime.combine(day, time.max if end else time.min, tzinfo=UTC)
        return state.millis(edge)

    # ------------------------------------------------------------------ sorting

    def _sorted(self, issues: list[wire.StoredIssue], key: SortKey) -> list[wire.StoredIssue]:
        """Ordered by one key; issues with no value for it come last whichever way it runs."""
        read = self._sort_value(key)
        keyed = [(read(i), i) for i in issues]
        present = [(v, i) for v, i in keyed if v is not None]
        empty = [i for v, i in keyed if v is None]
        present.sort(key=lambda pair: pair[0], reverse=key.descending)
        return [i for _, i in present] + empty

    def _sort_value(self, key: SortKey) -> Callable[[wire.StoredIssue], float | str | None]:
        if key.keyword is Keyword.CREATED:
            return lambda i: i.created
        if key.keyword is Keyword.UPDATED:
            return lambda i: i.updated
        if key.keyword is Keyword.RESOLVED_DATE:
            return lambda i: i.resolved
        if key.keyword is Keyword.SUMMARY:
            return lambda i: i.summary.lower()
        if key.keyword is Keyword.ISSUE_ID:
            return lambda i: float(state.ordinal(i.id)[1])
        if key.keyword is not None:
            raise wire.unparsed_query(self._text, f"cannot sort by {key.attribute}")
        definition = self._definitions[key.attribute]

        def value(i: wire.StoredIssue) -> float | str | None:
            raw = self._field_value(i, definition.name)
            if raw is None:
                return None
            if definition.fieldType in wire.BUNDLED:
                project = self._projects[i.project]
                field = self._world.project_field(project, definition.name)
                found = None if field is None else next((v for v in field.values if v.id == raw), None)
                return None if found is None else float(found.ordinal)
            if definition.fieldType is wire.FieldType.USER:
                user = self._world.user(str(raw))
                return None if user is None else user.fullName.lower()
            return raw if isinstance(raw, str) else float(raw)

        return value


def _any(tests: list[Test], items: list[Item]) -> Test:
    """Values of one attribute: any value named matches, and every value excluded must not."""
    named = [t for t, item in zip(tests, items, strict=True) if not item.negated]
    excluded = [t for t, item in zip(tests, items, strict=True) if item.negated]
    return lambda i: (not named or any(t(i) for t in named)) and all(t(i) for t in excluded)
