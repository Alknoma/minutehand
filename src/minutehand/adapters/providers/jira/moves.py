"""What happens to an issue, by whoever does it: field edits checked against the screen, transitions through the
workflow, assignment, comments and deletion, each written with its changelog entry.

The API (the agent's calls) and the provider's ports (a person's acts) both change issues through `Desk`, so
an issue a person moved and one the agent moved read back the same way, with the mover as the author.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import JsonValue

from minutehand.adapters.providers.jira import wire
from minutehand.adapters.providers.jira.state import JiraWorld
from minutehand.domain.scenario import TicketState
from minutehand.domain.world import Actor
from minutehand.ports.store import Store

_NOT_ADF = "The value must be an Atlassian document (a 'doc' node at version 1), not plain text."


class Desk:
    def __init__(self, store: Store) -> None:
        self.world = JiraWorld(store)

    def site(self) -> wire.StoredSite:
        return self.world.site()

    # ------------------------------------------------------------------ permissions

    def roles_of(self, project: wire.StoredProject, account: str) -> list[wire.StoredRole]:
        return [r for r in self.site().roles if account in project.accounts(r.id)]

    def can_browse(self, project: wire.StoredProject, account: str) -> bool:
        """Seeing a project takes a role in it; administering the site does not let anyone read its issues."""
        return bool(self.roles_of(project, account))

    def can_edit(self, project: wire.StoredProject, account: str) -> bool:
        return any(r.edits for r in self.roles_of(project, account))

    def can_administer(self, project: wire.StoredProject, account: str) -> bool:
        user = self.world.user(account)
        return (user is not None and user.siteAdmin) or any(r.administers for r in self.roles_of(project, account))

    def assignable(self, project: wire.StoredProject) -> list[wire.StoredUser]:
        """Who the project's issues can be given to: active Atlassian accounts in a role that edits."""
        site = self.site()
        editing = {a for r in site.roles if r.edits for a in project.accounts(r.id)}
        return [
            u
            for u in self.world.users()
            if u.accountId in editing and u.active and u.accountType is wire.AccountType.ATLASSIAN
        ]

    # ------------------------------------------------------------------ values

    def resolution_for(self, status: wire.StoredStatus) -> str:
        """The resolution a done status sets when the transition names none: Won't Do for a cancelled one."""
        site = self.site()
        name = "Won't Do" if status.outcome is TicketState.CANCELLED else "Done"
        return next((r.id for r in site.resolutions if r.name == name), site.resolutions[0].id)

    def stored_value(self, field: wire.StoredField, raw: JsonValue) -> JsonValue:
        """A custom field's value as written into a body, as it is kept; a value the field will not take is
        refused with Jira's 400 naming the field."""
        if raw is None:
            return None
        match field.kind:
            case wire.CustomFieldType.NUMBER:
                if isinstance(raw, bool) or not isinstance(raw, int | float):
                    raise wire.bad_field(field.id, "The value must be a number.")
                return raw
            case wire.CustomFieldType.STRING | wire.CustomFieldType.EPIC_LINK:
                if not isinstance(raw, str):
                    raise wire.bad_field(field.id, "The value must be a string.")
                return raw
            case wire.CustomFieldType.DATE:
                if not isinstance(raw, str) or not _is_date(raw):
                    raise wire.bad_field(field.id, "The value must be a date written as yyyy-MM-dd.")
                return raw
            case wire.CustomFieldType.OPTION:
                return self._option(field, raw)
            case wire.CustomFieldType.OPTIONS:
                if not isinstance(raw, list):
                    raise wire.bad_field(field.id, "The value must be a list of options.")
                return [self._option(field, item) for item in raw]
            case wire.CustomFieldType.USER:
                ref = wire.read_ref(raw)
                if ref is None or ref.accountId is None or self.world.user(ref.accountId) is None:
                    raise wire.bad_field(field.id, "The value must name a user by accountId.")
                return ref.accountId
            case wire.CustomFieldType.SPRINT:
                ids = raw if isinstance(raw, list) else [raw]
                found: list[JsonValue] = []
                for item in ids:
                    sprint = self.world.sprint(item) if isinstance(item, int) and not isinstance(item, bool) else None
                    if sprint is None:
                        raise wire.bad_field(field.id, f"There is no sprint with id '{item}'.")
                    found.append(sprint.id)
                return found

    def _option(self, field: wire.StoredField, raw: JsonValue) -> str:
        ref = wire.read_ref(raw) if isinstance(raw, dict) else wire.Ref(value=raw) if isinstance(raw, str) else None
        option = None
        if ref is not None:
            option = next(
                (
                    o
                    for o in field.options
                    if (ref.id is not None and str(ref.id) == o.id) or (ref.value is not None and ref.value == o.value)
                ),
                None,
            )
        if option is None:
            raise wire.bad_field(field.id, f"That option is not one of {field.name}'s options.")
        return option.id

    def value_text(self, field: wire.StoredField, value: JsonValue) -> str | None:
        """How the changelog spells a custom field's value."""
        if value is None:
            return None
        match field.kind:
            case wire.CustomFieldType.OPTION:
                return next((o.value for o in field.options if o.id == value), None)
            case wire.CustomFieldType.OPTIONS:
                ids = value if isinstance(value, list) else []
                return ",".join(o.value for o in field.options if o.id in ids)
            case wire.CustomFieldType.SPRINT:
                ids = value if isinstance(value, list) else []
                return ",".join(s.name for s in self.world.sprints() if s.id in ids)
            case wire.CustomFieldType.USER:
                user = self.world.user(str(value))
                return user.displayName if user is not None else None
            case _:
                return str(value)

    # ------------------------------------------------------------------ fields

    def apply_fields(
        self,
        issue: wire.StoredIssue,
        project: wire.StoredProject,
        fields: wire.Json,
        *,
        creating: bool,
    ) -> wire.StoredIssue:
        """The issue with `fields` written, every one checked against the issue type's screen before any is kept;
        every bad field is named in one 400, as Jira reports a whole input at once."""
        site = self.site()
        screen = project.screen(issue.issuetype)
        on_screen = set(screen.fields) if screen is not None else set()
        errors: dict[str, str] = {}
        changed = issue
        for name, raw in fields.items():
            if name in ("project", "issuetype"):  # enum-lint: exempt Jira's own field ids in an issue body
                if not creating:
                    errors[name] = wire.not_on_screen(name)
                continue
            if name not in on_screen:
                errors[name] = wire.not_on_screen(name)
                continue
            try:
                changed = self._field(changed, project, site, name, raw)
            except wire.Refusal as refusal:
                errors |= refusal.fields or {name: "; ".join(refusal.messages)}
        if creating and screen is not None:
            for name in screen.required:
                fixed = name in ("project", "issuetype")  # enum-lint: exempt Jira's own field ids in an issue body
                if fixed or name in errors:
                    continue
                if not _present(changed, name, fields):
                    errors[name] = _required(name, site)
        if errors:
            raise wire.Refusal(400, [], errors)
        return changed

    def _field(
        self, issue: wire.StoredIssue, project: wire.StoredProject, site: wire.StoredSite, name: str, raw: JsonValue
    ) -> wire.StoredIssue:
        match name:
            case "summary":
                if not isinstance(raw, str) or not raw.strip():
                    raise wire.bad_field(name, "You must give the issue a summary.")
                if len(raw) > 255:
                    raise wire.bad_field(name, "The summary must be shorter than 255 characters.")
                return issue.model_copy(update={"summary": raw})
            case "description":
                if raw is not None and not wire.is_document(raw):
                    raise wire.bad_field(name, _NOT_ADF)
                return issue.model_copy(update={"description": raw})
            case "priority":
                ref = wire.read_ref(raw)
                found = None
                if ref is not None:
                    found = next(
                        (
                            p
                            for p in site.priorities
                            if str(ref.id) == p.id or (ref.name or "").lower() == p.name.lower()
                        ),
                        None,
                    )
                if found is None:
                    raise wire.bad_field(name, "The priority must name one of the site's priorities by id or name.")
                return issue.model_copy(update={"priority": found.id})
            case "assignee":
                if raw is None:
                    return issue.model_copy(update={"assignee": None})
                ref = wire.read_ref(raw)
                account = ref.accountId if ref is not None else None
                if account is None or account not in {u.accountId for u in self.assignable(project)}:
                    raise wire.bad_field(name, f"User '{account}' cannot be assigned issues in {project.key}.")
                return issue.model_copy(update={"assignee": account})
            case "reporter":
                ref = wire.read_ref(raw)
                if ref is None or ref.accountId is None or self.world.user(ref.accountId) is None:
                    raise wire.bad_field(name, "The reporter must name a user by accountId.")
                return issue.model_copy(update={"reporter": ref.accountId})
            case "labels":
                if not isinstance(raw, list) or not all(isinstance(label, str) for label in raw):
                    raise wire.bad_field(name, "Labels must be a list of strings.")
                labels = [str(label) for label in raw]
                if any(not label or any(c.isspace() for c in label) for label in labels):
                    raise wire.bad_field(name, "A label cannot be empty or hold a space.")
                return issue.model_copy(update={"labels": labels})
            case "duedate":
                if raw is None:
                    return issue.model_copy(update={"duedate": None})
                if not isinstance(raw, str) or not _is_date(raw):
                    raise wire.bad_field(name, "The due date must be written as yyyy-MM-dd.")
                return issue.model_copy(update={"duedate": date.fromisoformat(raw)})
            case "parent":
                return issue.model_copy(update={"parent": self._parent(issue, project, site, raw)})
            case _:
                field = site.field(name)
                if field is None:
                    raise wire.bad_field(name, wire.not_on_screen(name))
                value = self.stored_value(field, raw)
                kept = [v for v in issue.custom if v.field != name]
                if value is not None:
                    kept.append(wire.StoredValue(field=name, value=value))
                return issue.model_copy(update={"custom": kept})

    def _parent(
        self, issue: wire.StoredIssue, project: wire.StoredProject, site: wire.StoredSite, raw: JsonValue
    ) -> str | None:
        if raw is None:
            if site.issue_type(issue.issuetype).subtask:
                raise wire.bad_field("parent", "A subtask must have a parent.")
            return None
        ref = wire.read_ref(raw)
        reference = (ref.key or (str(ref.id) if ref.id is not None else None)) if ref is not None else None
        parent = self.world.find_issue(reference) if reference else None
        if parent is None or not self.can_browse_issue(parent, project):
            raise wire.bad_field("parent", "The parent issue does not exist, or you cannot see it.")
        if parent.id == issue.id:
            raise wire.bad_field("parent", "An issue cannot be its own parent.")
        child_level = site.issue_type(issue.issuetype).hierarchyLevel
        if site.issue_type(parent.issuetype).hierarchyLevel != child_level + 1:
            raise wire.bad_field("parent", "The parent must be exactly one level above this issue's type.")
        return parent.id

    def can_browse_issue(self, issue: wire.StoredIssue, project: wire.StoredProject) -> bool:
        return issue.project == project.id or self.world.project(issue.project) is not None

    def items(self, before: wire.StoredIssue, after: wire.StoredIssue) -> list[wire.StoredItem]:
        """The changelog lines between two versions of an issue."""
        site = self.site()
        found: list[wire.StoredItem] = []

        def user(account: str | None) -> str | None:
            found_user = self.world.user(account) if account is not None else None
            return found_user.displayName if found_user is not None else None

        def issue_key(issue_id: str | None) -> str | None:
            parent = self.world.issue(issue_id) if issue_id is not None else None
            return parent.key if parent is not None else None

        pairs: list[tuple[str, str, str | None, str | None, str | None, str | None]] = [
            ("summary", "summary", None, before.summary, None, after.summary),
            ("description", "description", None, wire.adf_text(before.description) or None, None,
             wire.adf_text(after.description) or None),
            ("status", "status", before.status, site.status(before.status).name, after.status,
             site.status(after.status).name),
            ("resolution", "resolution", before.resolution,
             site.resolution(before.resolution).name if before.resolution else None, after.resolution,
             site.resolution(after.resolution).name if after.resolution else None),
            ("priority", "priority", before.priority, site.priority(before.priority).name, after.priority,
             site.priority(after.priority).name),
            ("assignee", "assignee", before.assignee, user(before.assignee), after.assignee, user(after.assignee)),
            ("reporter", "reporter", before.reporter, user(before.reporter), after.reporter, user(after.reporter)),
            ("labels", "labels", None, " ".join(before.labels) or None, None, " ".join(after.labels) or None),
            ("duedate", "duedate", before.duedate.isoformat() if before.duedate else None,
             before.duedate.isoformat() if before.duedate else None,
             after.duedate.isoformat() if after.duedate else None,
             after.duedate.isoformat() if after.duedate else None),
            ("IssueParentAssociation", "parent", before.parent, issue_key(before.parent), after.parent,
             issue_key(after.parent)),
            ("issuetype", "issuetype", before.issuetype, site.issue_type(before.issuetype).name, after.issuetype,
             site.issue_type(after.issuetype).name),
        ]  # fmt: skip
        for label, field_id, old, old_text, new, new_text in pairs:
            if (old, old_text) != (new, new_text):
                found.append(
                    wire.StoredItem.model_validate(
                        {"field": label, "fieldId": field_id, "from": old, "fromString": old_text, "to": new,
                         "toString": new_text}
                    )
                )  # fmt: skip
        for field in site.fields:
            old_value, new_value = before.value(field.id), after.value(field.id)
            if old_value != new_value:
                found.append(
                    wire.StoredItem(
                        field=field.name,
                        fieldtype="custom",
                        fieldId=field.id,
                        fromString=self.value_text(field, old_value),
                        toString=self.value_text(field, new_value),
                    )
                )
        return found

    def changed(
        self, before: wire.StoredIssue, after: wire.StoredIssue, *, by: str | None, at: datetime
    ) -> wire.StoredIssue:
        """`after`, stamped as changed by `by` at `at` with its changelog entry; `before` when nothing differs."""
        items = self.items(before, after)
        if not items:
            return before
        entry = wire.StoredHistory(id=self.world.next_id(), author=by, created=at, items=items)
        return after.model_copy(update={"updated": at, "history": [*after.history, entry]})

    # ------------------------------------------------------------------ moves

    def moved(
        self,
        issue: wire.StoredIssue,
        to: wire.StoredStatus,
        *,
        resolution: str | None = None,
    ) -> wire.StoredIssue:
        """The issue in status `to`: a done status sets the resolution and its date, leaving one clears both."""
        was = self.site().status(issue.status)
        if to.category is wire.Category.DONE:
            return issue.model_copy(
                update={
                    "status": to.id,
                    "resolution": resolution or issue.resolution or self.resolution_for(to),
                    "resolutiondate": issue.resolutiondate if was.category is wire.Category.DONE else None,
                }
            )
        return issue.model_copy(update={"status": to.id, "resolution": None, "resolutiondate": None})

    def transitions(self, issue: wire.StoredIssue, project: wire.StoredProject) -> list[wire.StoredTransition]:
        """The transitions open from the issue's status, in workflow order, leaving out a move to where it is."""
        return [
            t
            for t in project.transitions
            if (not t.sources or issue.status in t.sources) and not (not t.sources and t.to == issue.status)
        ]

    def write(
        self, before: wire.StoredIssue, after: wire.StoredIssue, *, by: str | None, at: datetime, actor: Actor
    ) -> wire.StoredIssue:
        """Keep `after` with its changelog entry, stamping a resolution date on entering a done status."""
        site = self.site()
        if after.resolution is not None and after.resolutiondate is None:
            after = after.model_copy(update={"resolutiondate": at})
        stamped = self.changed(before, after, by=by, at=at)
        if stamped is not before:
            self.world.update_issue(stamped, actor=actor)
        del site
        return stamped

    def comment(
        self, issue: wire.StoredIssue, body: JsonValue, *, by: str, at: datetime, actor: Actor
    ) -> wire.StoredComment:
        comment = wire.StoredComment(
            id=self.world.next_id(),
            issue=issue.id,
            author=by,
            body=body,
            created=at,
            updated=at,
            updateAuthor=by,
        )
        self.world.write_comment(comment, actor=actor)
        current = self.world.issue(issue.id)
        if current is not None and current.updated < at:
            self.world.update_issue(current.model_copy(update={"updated": at}), actor=actor)
        return comment

    def delete(self, issue: wire.StoredIssue, *, actor: Actor) -> None:
        """The issue and its subtasks; the links that named any of them go with them."""
        doomed = [*self.world.subtasks(issue), issue]
        ids = {i.id for i in doomed}
        for link in self.world.links():
            if link.source in ids or link.destination in ids:
                self.world.delete_link(link, actor=actor)
        for gone in doomed:
            self.world.delete_issue(gone, actor=actor)


def _is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return len(text) == 10


def _present(issue: wire.StoredIssue, name: str, fields: wire.Json) -> bool:
    if name not in fields:
        return False
    match name:
        case "summary":
            return bool(issue.summary.strip())
        case "parent":
            return issue.parent is not None
        case _:
            return fields[name] is not None


def _required(name: str, site: wire.StoredSite) -> str:
    match name:
        case "summary":
            return "You must give the issue a summary."
        case "parent":
            return "A subtask must have a parent."
        case _:
            field = site.field(name)
            return f"{field.name if field is not None else name} is required."
