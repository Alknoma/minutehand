"""An issue's activity feed, read from the run's log rather than kept beside it.

Every write to an issue is a new version of it in the store, stamped with who wrote it (`updater`) and when
(`updated`), so the feed is the difference between each version and the one before: one item per field, tag set,
summary or description that changed. Comments and links are entities of their own and add their own items. A
person who changed a state in the scenario is that item's author, at the moment the run's clock stood at.
"""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise

from minutehand.adapters.providers.youtrack import wire
from minutehand.adapters.providers.youtrack.present import Presenter
from minutehand.adapters.providers.youtrack.state import YouTrackWorld


class Category(StrEnum):
    ISSUE_CREATED = "IssueCreatedCategory"
    CUSTOM_FIELD = "CustomFieldCategory"
    COMMENTS = "CommentsCategory"
    SUMMARY = "SummaryCategory"
    DESCRIPTION = "DescriptionCategory"
    TAGS = "TagsCategory"
    LINKS = "LinksCategory"


def categories(text: str | None) -> set[Category]:
    """The `categories=` parameter, which YouTrack requires."""
    if text is None or not text.strip():
        raise wire.bad_request("Parameter 'categories' should be specified")
    chosen: set[Category] = set()
    for name in (n.strip() for n in text.split(",")):
        if not name:
            continue
        try:
            chosen.add(Category(name))
        except ValueError as error:
            raise wire.bad_request(f"Unknown activity category: {name}") from error
    return chosen


Change = list[wire.ActivityValueOut] | wire.ActivityValueOut | None


class _Item:
    def __init__(self, seq: int, out: wire.ActivityOut) -> None:
        self.seq = seq
        self.out = out


class Feed:
    def __init__(self, world: YouTrackWorld, present: Presenter) -> None:
        self._world = world
        self._present = present

    def of(self, issue: wire.StoredIssue, wanted: set[Category]) -> list[wire.ActivityOut]:
        """The issue's activities in the chosen categories, oldest first."""
        project = self._present.home(issue)
        target = self._present.issue_ref(issue)
        items: list[_Item] = []
        history = self._world.issue_history(issue.id)
        if history and Category.ISSUE_CREATED in wanted:
            seq, first = history[0]
            items.append(
                _Item(
                    seq,
                    self._item(
                        "IssueCreatedActivityItem",
                        seq,
                        0,
                        first.reporter,
                        first.created,
                        Category.ISSUE_CREATED,
                        target,
                        None,
                        None,
                        None,
                    ),
                )
            )
        for (_, before), (seq, after) in pairwise(history):
            items += self._changes(project, target, seq, before, after, wanted)
        if Category.COMMENTS in wanted:
            seqs = self._world.comment_seqs(issue.id)
            for comment in self._world.comments(issue.id):
                seq = seqs[comment.id]
                shown = self._present.comment(comment, issue)
                items.append(
                    _Item(
                        seq,
                        self._item(
                            "CommentActivityItem",
                            seq,
                            0,
                            comment.author,
                            comment.created,
                            Category.COMMENTS,
                            shown,
                            None,
                            [shown],
                            [],
                        ),
                    )
                )
        if Category.LINKS in wanted:
            items += self._links(issue, target)
        items.sort(key=lambda i: (i.out.timestamp, i.seq, i.out.id))
        return [i.out for i in items]

    def _item(
        self,
        kind: str,
        seq: int,
        n: int,
        author: str,
        at: int,
        category: Category,
        target: wire.LinkedIssueOut | wire.CommentOut,
        field: wire.FilterFieldOut | None,
        added: Change,
        removed: Change,
    ) -> wire.ActivityOut:
        return wire.ActivityOut(
            type_=kind,
            id=f"{target.id}.{seq}-{n}",
            timestamp=at,
            author=self._present.user_by_id(author),
            category=wire.ActivityCategoryOut(id=category.value),
            target=target,
            targetMember=None if field is None else field.name,
            field=field,
            added=added,
            removed=removed,
        )

    def _changes(
        self,
        project: wire.StoredProject,
        target: wire.LinkedIssueOut,
        seq: int,
        before: wire.StoredIssue,
        after: wire.StoredIssue,
        wanted: set[Category],
    ) -> list[_Item]:
        found: list[_Item] = []
        author, at = after.updater, after.updated

        def add(kind: str, category: Category, field: wire.FilterFieldOut, added: Change, removed: Change) -> None:
            found.append(
                _Item(seq, self._item(kind, seq, len(found), author, at, category, target, field, added, removed))
            )

        if Category.SUMMARY in wanted and before.summary != after.summary:
            add("SimpleValueActivityItem", Category.SUMMARY, _predefined("summary"), after.summary, before.summary)
        if Category.DESCRIPTION in wanted and before.description != after.description:
            add(
                "TextMarkupActivityItem",
                Category.DESCRIPTION,
                _predefined("description"),
                after.description,
                before.description,
            )
        if Category.CUSTOM_FIELD in wanted:
            for field in project.fields:
                was, now = before.values.get(field.id), after.values.get(field.id)
                if was == now:
                    continue
                definition = self._world.definition(field.field)
                if definition is None:
                    continue
                shown_now = self._present.issue_value(project, field, now)
                shown_was = self._present.issue_value(project, field, was)
                filter_field = wire.FilterFieldOut(
                    type_="CustomFilterField", id=definition.name, name=definition.name, presentation=definition.name
                )
                if definition.fieldType in wire.BUNDLED or definition.fieldType is wire.FieldType.USER:
                    add(
                        "CustomFieldActivityItem",
                        Category.CUSTOM_FIELD,
                        filter_field,
                        _listed(shown_now),
                        _listed(shown_was),
                    )
                else:
                    add(
                        "CustomFieldActivityItem",
                        Category.CUSTOM_FIELD,
                        filter_field,
                        _plain(shown_now),
                        _plain(shown_was),
                    )
        if Category.TAGS in wanted and before.tags != after.tags:
            gained = [self._tag(t) for t in after.tags if t not in before.tags]
            lost = [self._tag(t) for t in before.tags if t not in after.tags]
            add(
                "TagsActivityItem",
                Category.TAGS,
                _predefined("tag"),
                [t for t in gained if t is not None],
                [t for t in lost if t is not None],
            )
        return found

    def _tag(self, tag: str) -> wire.TagOut | None:
        stored = self._world.tag(tag)
        return None if stored is None else self._present.tag(stored)

    def _links(self, issue: wire.StoredIssue, target: wire.LinkedIssueOut) -> list[_Item]:
        found: list[_Item] = []
        for link in self._world.every_link():
            if issue.id not in (link.source, link.target):
                continue
            other_id = link.target if link.source == issue.id else link.source
            other = self._world.issue(other_id)
            if other is None:
                continue
            shown = self._present.issue_ref(other)
            held = False
            for seq, version in self._world.link_history(link):
                holds = version.removed is None
                if holds == held:
                    continue
                held = holds
                author = version.author if holds else (version.remover or version.author)
                at = version.created if holds else (version.removed or version.created)
                field = _predefined("links")
                added: list[wire.ActivityValueOut] = [shown] if holds else []
                removed: list[wire.ActivityValueOut] = [] if holds else [shown]
                found.append(
                    _Item(
                        seq,
                        self._item(
                            "LinkActivityItem", seq, 0, author, at, Category.LINKS, target, field, added, removed
                        ),
                    )
                )
        return found


def _predefined(name: str) -> wire.FilterFieldOut:
    return wire.FilterFieldOut(type_="PredefinedFilterField", id=name, name=name, presentation=name)


def _plain(value: wire.IssueFieldValueOut | None) -> wire.ActivityValueOut | None:
    """A simple field's value as an activity carries it: a period by its minutes, anything else as itself."""
    if isinstance(value, wire.PeriodOut):
        return value.minutes
    return value


def _listed(value: wire.IssueFieldValueOut | None) -> list[wire.ActivityValueOut]:
    """A bundled or user value as an activity carries it: a collection of what was added or removed."""
    if value is None or isinstance(value, wire.PeriodOut):
        return []
    return [value]
