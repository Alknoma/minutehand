"""Issues, issue comments and labels: the part of GitHub's REST API a team tracks its work in.

GitHub numbers a repository's issues and pull requests from one sequence and answers a pull request on the "Issues"
routes too, marked by `pull_request` (https://docs.github.com/en/rest/issues/issues#list-repository-issues), so one
stored record serves both and this module presents both. What an agent writes (titles, bodies, label names, comments)
is kept as sent and answered as kept; what GitHub assigns (ids, node ids, numbers, URLs, timestamps, counts) is made
here, from the run's clock and the world's counters.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import JsonValue
from starlette.requests import Request

from minutehand.adapters.providers.github import state, wire
from minutehand.adapters.providers.github.answers import Answered, Caller, as_json, chosen, paged, param
from minutehand.adapters.providers.github.manifest import MANIFEST
from minutehand.domain.errors import NotServed
from minutehand.domain.transitions import Transition, transition_change
from minutehand.domain.world import Actor, Operation

if TYPE_CHECKING:
    from minutehand.adapters.providers.github.app import GitHubApi

FIRST_ID = 10_000_000_000
"""Where the ids GitHub hands out for what the agent makes begin: above every id a seed derives from a name."""
ISSUES = "/issues/issues"
COMMENTS = "/issues/comments"
LABELS = "/issues/labels"


def moment(text: str) -> str:
    """A `since` as GitHub writes a timestamp: ISO 8601, `YYYY-MM-DDTHH:MM:SSZ`; any other is refused by name."""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise NotServed("a since that is not ISO 8601, YYYY-MM-DDTHH:MM:SSZ") from error
    if parsed.tzinfo is None:
        raise NotServed("a since without a time zone: the reference writes it YYYY-MM-DDTHH:MM:SSZ")
    return wire.timestamp(parsed.astimezone(UTC))


def label_names(sent: wire.Sent, name: str) -> list[str] | None:
    """A `labels` parameter: names, or objects with a `name`, as the reference lets either stand."""
    if not sent.present(name):
        return None
    return label_list(sent.fields[name], sent)


def label_list(found: JsonValue, sent: wire.Sent) -> list[str]:
    if not isinstance(found, list):
        raise sent.invalid()
    names: list[str] = []
    for item in found:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item["name"] if "name" in item else None, str):
            names.append(str(item["name"]))
        else:
            raise sent.invalid()
    return names


class Tracker:
    def __init__(self, api: GitHubApi) -> None:
        self._api = api

    # ------------------------------------------------------------------ shared

    def agent(self, caller: Caller) -> wire.StoredAccount:
        """The user a write acts as: GitHub refuses a write that acts as nobody."""
        if caller.account is None:
            raise wire.requires_authentication()
        return caller.account

    def association(self, repository: wire.StoredRepository, login: str) -> wire.Association:
        """How an author is associated with the repository, in the words GraphQL documents for each:
        https://docs.github.com/en/graphql/reference/enums#commentauthorassociation"""
        lowered = login.lower()
        if repository.owner.lower() == lowered:
            return wire.Association.OWNER
        owner = self._api.world.account(repository.owner)
        if owner is not None and lowered in {m.lower() for m in owner.members}:
            return wire.Association.MEMBER
        if any(c.login.lower() == lowered for c in repository.collaborators):
            return wire.Association.COLLABORATOR
        if any((c.author_login or "").lower() == lowered for c in repository.commits):
            return wire.Association.CONTRIBUTOR
        return wire.Association.NONE

    def assignable(self, logins: list[str]) -> list[str]:
        """The users to assign, as the world names them: a user of this GitHub. A login that names no user is refused
        by name; what a user may be assigned is not a thing this provider judges (authorization is out of scope)."""
        found: list[str] = []
        for login in logins:
            account = self._api.world.account(login)
            if account is None or account.type is not wire.AccountType.USER:
                raise NotServed(f"assigning {login}, who is not a user of this GitHub")
            if account.login not in found:
                found.append(account.login)
        return found

    def defined(self, repository: wire.StoredRepository, names: list[str]) -> list[str]:
        """Label names the repository has defined, each once. A name it has not is refused by name: the reference does
        not say what GitHub does with one."""
        held = {label.name for label in self._api.world.labels(repository)}
        found: list[str] = []
        for name in names:
            if name not in held:
                raise NotServed(f"the label {name}, which {repository.full_name} has not defined")
            if name not in found:
                found.append(name)
        return found

    def issue_of(self, request: Request, repository: wire.StoredRepository, section: str) -> wire.StoredIssue:
        found = self._api.world.issue(repository, int(request.path_params["issue_number"]))
        if found is None:
            raise wire.not_found(section)
        return found

    # ------------------------------------------------------------------ presenting

    def label_out(self, repository: wire.StoredRepository, label: wire.StoredLabel) -> wire.LabelOut:
        url = f"{wire.API}/repos/{repository.full_name}/labels/{quote(label.name, safe=':')}"
        return wire.LabelOut(
            id=label.id,
            node_id=wire.node_id("LA", label.id),
            url=url,
            name=label.name,
            description=label.description,
            color=label.color,
            default=label.default,
        )

    def issue_out(
        self,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        *,
        labels: dict[str, wire.StoredLabel],
        comments: int,
    ) -> wire.IssueOut:
        full = repository.full_name
        at = f"{wire.API}/repos/{full}/issues/{issue.number}"
        assignees = [self._api.account_out(login) for login in issue.assignees]
        common = {
            "url": at,
            "repository_url": f"{wire.API}/repos/{full}",
            "labels_url": f"{at}/labels{{/name}}",
            "comments_url": f"{at}/comments",
            "events_url": f"{at}/events",
            "id": issue.id,
            "node_id": wire.node_id("I", issue.id),
            "number": issue.number,
            "state": issue.state,
            "state_reason": issue.state_reason,
            "title": issue.title,
            "body": issue.body,
            "user": self._api.account_out(issue.author),
            "labels": [self.label_out(repository, labels[n]) for n in issue.labels if n in labels],
            "assignee": assignees[0] if assignees else None,
            "assignees": assignees,
            "locked": issue.locked,
            "active_lock_reason": issue.lock_reason,
            "comments": comments,
            "closed_at": issue.closed_at,
            "closed_by": None if issue.closed_by is None else self._api.account_out(issue.closed_by),
            "created_at": issue.created_at,
            "updated_at": issue.updated_at,
            "author_association": self.association(repository, issue.author),
        }
        pull = issue.pull
        if pull is None:
            return wire.IssueOut(html_url=f"{wire.WEB}/{full}/issues/{issue.number}", **common)
        web = f"{wire.WEB}/{full}/pull/{issue.number}"
        return wire.IssueOfPullOut(
            html_url=web,
            pull_request=wire.IssuePullOut(
                url=f"{wire.API}/repos/{full}/pulls/{issue.number}",
                html_url=web,
                diff_url=f"{web}.diff",
                patch_url=f"{web}.patch",
                merged_at=pull.merged_at,
            ),
            draft=pull.draft,
            **common,
        )

    def comment_out(self, repository: wire.StoredRepository, comment: wire.StoredComment) -> wire.CommentOut:
        full = repository.full_name
        issue = self._api.world.issue(repository, comment.issue)
        kind = "pull" if issue is not None and issue.pull is not None else "issues"
        return wire.CommentOut(
            url=f"{wire.API}/repos/{full}/issues/comments/{comment.id}",
            html_url=f"{wire.WEB}/{full}/{kind}/{comment.issue}#issuecomment-{comment.id}",
            issue_url=f"{wire.API}/repos/{full}/issues/{comment.issue}",
            id=comment.id,
            node_id=wire.node_id("IC", comment.id),
            user=self._api.account_out(comment.author),
            created_at=comment.created_at,
            updated_at=comment.updated_at,
            author_association=self.association(repository, comment.author),
            body=comment.body,
        )

    def present(self, repository: wire.StoredRepository, issues: list[wire.StoredIssue]) -> list[wire.Wire]:
        """Each issue as the list routes answer it, labels and comment counts read once."""
        labels = {label.name: label for label in self._api.world.labels(repository)}
        counts = Counter(c.issue for c in self._api.world.comments(repository))
        return [self.issue_out(repository, i, labels=labels, comments=counts[i.number]) for i in issues]

    # ------------------------------------------------------------------ issues

    async def list_issues(self, request: Request, caller: Caller) -> Answered:
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, f"{ISSUES}#list-repository-issues")
        asked = {k: v for k, v in request.query_params.items()}
        for unserved in ("mentioned", "issue_field_values"):
            if unserved in asked:
                raise NotServed(f"the parameter {unserved}: the world holds no mentions or issue fields")
        status = chosen(request, "state", wire.StateFilter, wire.StateFilter.OPEN)
        sort = chosen(request, "sort", wire.IssueSort, wire.IssueSort.CREATED)
        direction = chosen(request, "direction", wire.Direction, wire.Direction.DESC)
        issues = self._api.world.issues(repository)
        counts = Counter(c.issue for c in self._api.world.comments(repository))
        if status is not wire.StateFilter.ALL:
            issues = [i for i in issues if i.state.value == status.value]
        wanted = param(request, "labels")
        if wanted is not None:
            names = [n for n in wanted.split(",") if n]
            issues = [i for i in issues if all(n in i.labels for n in names)]
        assignee = param(request, "assignee")
        if assignee == wire.NO_ONE:
            issues = [i for i in issues if not i.assignees]
        elif assignee == wire.ANYONE:
            issues = [i for i in issues if i.assignees]
        elif assignee is not None:
            issues = [i for i in issues if assignee.lower() in {a.lower() for a in i.assignees}]
        creator = param(request, "creator")
        if creator is not None:
            issues = [i for i in issues if i.author.lower() == creator.lower()]
        # No milestone or issue type exists in this world, so none of its issues has either; what the reference says
        # of each filter then follows: `none` keeps every issue, `*` and a name or number keep none.
        for filtered in ("milestone", "type"):
            if filtered in asked and asked[filtered] != wire.NO_ONE:
                issues = []
        since = param(request, "since")
        if since is not None:
            # Inclusive: recorded of the real service (tests/data/github_rest/).
            floor = moment(since)
            issues = [i for i in issues if i.updated_at >= floor]
        key = {
            wire.IssueSort.CREATED: lambda i: (i.created_at, i.number),
            wire.IssueSort.UPDATED: lambda i: (i.updated_at, i.number),
            wire.IssueSort.COMMENTS: lambda i: (counts[i.number], i.number),
        }[sort]
        issues = sorted(issues, key=key, reverse=direction is wire.Direction.DESC)
        start, end, links = paged(request, len(issues))
        self._api.world.saw(state.repository_ref(owner, name), Operation.SEARCH)
        return as_json(self.present(repository, issues[start:end]), headers=links)

    async def create_issue(self, request: Request, caller: Caller) -> Answered:
        section = f"{ISSUES}#create-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        account = self.agent(caller)
        sent = wire.Sent.read(await request.body(), section=section)
        title = sent.text("title", required=True, numbers=True)
        assert title is not None
        body = sent.text("body")
        labels = label_names(sent, "labels")
        assignees = sent.texts("assignees")
        single = sent.text("assignee")
        sent.refuse("milestone", why="the world holds no milestones")
        sent.refuse("type", why="the world holds no issue types")
        sent.refuse("parent_issue_id", why="the world holds no sub-issues")
        sent.refuse("issue_field_values", why="the world holds no issue fields")
        if not title.strip():
            raise sent.invalid()
        named = list(assignees or []) if assignees is not None else ([single] if single is not None else [])
        kept_labels = self.defined(repository, labels or [])
        kept_assignees = self.assignable(named)
        issue = await self.open_issue(
            repository,
            account,
            title=title,
            body=body,
            labels=kept_labels,
            assignees=kept_assignees,
            actor=Actor.AGENT,
            who=None,
        )
        return as_json(self.present(repository, [issue])[0], 201)

    async def open_issue(
        self,
        repository: wire.StoredRepository,
        account: wire.StoredAccount,
        *,
        title: str,
        body: str | None,
        labels: list[str],
        assignees: list[str],
        actor: Actor,
        who: str | None,
    ) -> wire.StoredIssue:
        """An issue opened by `account`: numbered after the repository's last issue or pull request, stamped from the
        clock, recorded as the first move of its state, and an `issues` webhook if the agent has a target."""
        world = self._api.world
        number = max((i.number for i in world.issues(repository)), default=0) + 1
        now = wire.timestamp(self._api.clock.now())
        issue = wire.StoredIssue(
            number=number,
            id=world.next_id("issue", first=FIRST_ID, actor=actor),
            title=title,
            body=body,
            author=account.login,
            labels=labels,
            assignees=assignees,
            created_at=now,
            updated_at=now,
        )
        world.put_issue(repository, issue, operation=Operation.CREATE, actor=actor)
        self.moved_to(repository, issue, "open", None, actor, who, "{}")
        await self._api.hooks.opened(repository, issue, account)
        return issue

    async def edit_issue(
        self,
        repository: wire.StoredRepository,
        before: wire.StoredIssue,
        after: wire.StoredIssue,
        *,
        by: wire.StoredAccount,
        actor: Actor,
        who: str | None,
        content: str = "{}",
    ) -> wire.StoredIssue:
        """`after` kept in place of `before` when it differs, its update time the clock's; a change of state is a move
        of the item's state, recorded, and a webhook if the agent has a target."""
        if after == before:
            return before
        stamped = after.model_copy(update={"updated_at": wire.timestamp(self._api.clock.now())})
        self._api.world.put_issue(repository, stamped, operation=Operation.UPDATE, actor=actor)
        if stamped.state is not before.state:
            name = "close" if stamped.state is wire.IssueState.CLOSED else "reopen"
            self.moved_to(repository, stamped, name, before.state.value, actor, who, content)
            await self._api.hooks.state_changed(repository, before, stamped, by)
        return stamped

    def moved_to(
        self,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        name: str,
        was: str | None,
        actor: Actor,
        who: str | None,
        content: str,
    ) -> None:
        """The move recorded once in the log beside the issue's own versions (`docs/design-transitions.md`)."""
        item = state.issue_ref(repository.owner, repository.name, issue.number)
        moved = Transition(
            provider=MANIFEST.key,
            item=item,
            name=name,
            from_state=was,
            to_state=issue.state.value,
            by=actor,
            who=who,
            content=content,
            at=self._api.clock.now(),
        )
        self._api.world.store.apply(transition_change(moved, at_seq=self._api.world.store.head() + 1))

    async def post_comment(
        self,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        account: wire.StoredAccount,
        body: str,
        *,
        actor: Actor,
    ) -> wire.StoredComment:
        """A comment written now. The issue's `updated_at` moves with it: recorded of the real service
        (tests/data/github_rest/)."""
        world = self._api.world
        now = wire.timestamp(self._api.clock.now())
        comment = wire.StoredComment(
            id=world.next_id("comment", first=FIRST_ID, actor=actor),
            issue=issue.number,
            author=account.login,
            body=body,
            created_at=now,
            updated_at=now,
        )
        world.put_comment(repository, comment, operation=Operation.CREATE, actor=actor)
        touched = issue.model_copy(update={"updated_at": now})
        world.put_issue(repository, touched, operation=Operation.UPDATE, actor=actor)
        await self._api.hooks.comment_created(repository, touched, comment, account)
        return comment

    async def get_issue(self, request: Request, caller: Caller) -> Answered:
        section = f"{ISSUES}#get-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json(self.present(repository, [issue])[0])

    async def update_issue(self, request: Request, caller: Caller) -> Answered:
        section = f"{ISSUES}#update-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        account = self.agent(caller)
        sent = wire.Sent.read(await request.body(), section=section, optional=True)
        sent.refuse("type", why="the world holds no issue types")
        sent.refuse("issue_field_values", why="the world holds no issue fields")
        sent.refuse("duplicate_issue_id", why="the world holds no canonical duplicates")
        sent.refuse("milestone", why="the world holds no milestones")
        changes: dict[str, object] = {}
        if sent.named("title"):
            title = sent.text("title", required=True, numbers=True)
            if title is None or not title.strip():
                raise sent.invalid()
            changes["title"] = title
        if sent.named("body"):
            changes["body"] = sent.text("body")
        labels = label_names(sent, "labels")
        if labels is not None:
            changes["labels"] = self.defined(repository, labels)
        assignees = sent.texts("assignees")
        single = sent.text("assignee")
        if assignees is not None or single is not None:
            named = assignees if assignees is not None else ([single] if single is not None else [])
            changes["assignees"] = self.assignable(named)
        wanted = sent.member("state", wire.IssueState)
        reason = sent.member("state_reason", wire.StateReason)
        now = wire.timestamp(self._api.clock.now())
        changed = issue.model_copy(update=changes)
        if wanted is not None and wanted is not issue.state:
            changed = self.moved(changed, wanted, reason, account, now)
        changed = await self.edit_issue(repository, issue, changed, by=account, actor=Actor.AGENT, who=None)
        return as_json(self.present(repository, [changed])[0])

    def moved(
        self,
        issue: wire.StoredIssue,
        to: wire.IssueState,
        reason: wire.StateReason | None,
        by: wire.StoredAccount,
        now: str,
    ) -> wire.StoredIssue:
        """The issue opened or closed: when, by whom and, as sent, why. A pull request that was merged stays merged
        and cannot be reopened."""
        if to is wire.IssueState.CLOSED:
            return issue.model_copy(
                update={
                    "state": wire.IssueState.CLOSED,
                    "state_reason": reason,
                    "closed_at": now,
                    "closed_by": by.login,
                }
            )
        if issue.pull is not None and issue.pull.merged:
            raise NotServed("reopening a pull request that was merged")
        return issue.model_copy(
            update={"state": wire.IssueState.OPEN, "state_reason": reason, "closed_at": None, "closed_by": None}
        )

    async def lock_issue(self, request: Request, caller: Caller) -> Answered:
        section = f"{ISSUES}#lock-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        self.agent(caller)
        sent = wire.Sent.read(await request.body(), section=section, optional=True)
        reason = sent.lock_reason("lock_reason")
        locked = issue.model_copy(update={"locked": True, "lock_reason": reason})
        if locked != issue:
            self._api.world.put_issue(repository, locked, operation=Operation.UPDATE, actor=Actor.AGENT)
        return Answered(204, b"")

    async def unlock_issue(self, request: Request, caller: Caller) -> Answered:
        section = f"{ISSUES}#unlock-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        self.agent(caller)
        unlocked = issue.model_copy(update={"locked": False, "lock_reason": None})
        if unlocked != issue:
            self._api.world.put_issue(repository, unlocked, operation=Operation.UPDATE, actor=Actor.AGENT)
        return Answered(204, b"")

    # ------------------------------------------------------------------ comments

    async def list_comments(self, request: Request, caller: Caller) -> Answered:
        section = f"{COMMENTS}#list-issue-comments"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        found = [c for c in self._api.world.comments(repository) if c.issue == issue.number]
        since = param(request, "since")
        if since is not None:
            floor = moment(since)
            found = [c for c in found if c.updated_at >= floor]
        start, end, links = paged(request, len(found))
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json([self.comment_out(repository, c) for c in found[start:end]], headers=links)

    async def create_comment(self, request: Request, caller: Caller) -> Answered:
        section = f"{COMMENTS}#create-an-issue-comment"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        account = self.agent(caller)
        sent = wire.Sent.read(await request.body(), section=section)
        body = sent.text("body", required=True)
        assert body is not None
        comment = await self.post_comment(repository, issue, account, body, actor=Actor.AGENT)
        return as_json(self.comment_out(repository, comment), 201)

    def comment_of(self, request: Request, repository: wire.StoredRepository, section: str) -> wire.StoredComment:
        found = self._api.world.comment(repository, int(request.path_params["comment_id"]))
        if found is None:
            raise wire.not_found(section)
        return found

    async def get_comment(self, request: Request, caller: Caller) -> Answered:
        section = f"{COMMENTS}#get-an-issue-comment"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        comment = self.comment_of(request, repository, section)
        self._api.world.saw(state.issue_ref(owner, name, comment.issue), Operation.READ)
        return as_json(self.comment_out(repository, comment))

    async def update_comment(self, request: Request, caller: Caller) -> Answered:
        section = f"{COMMENTS}#update-an-issue-comment"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        comment = self.comment_of(request, repository, section)
        self.agent(caller)
        sent = wire.Sent.read(await request.body(), section=section)
        body = sent.text("body", required=True)
        assert body is not None
        changed = comment.model_copy(update={"body": body, "updated_at": wire.timestamp(self._api.clock.now())})
        self._api.world.put_comment(repository, changed, operation=Operation.UPDATE, actor=Actor.AGENT)
        return as_json(self.comment_out(repository, changed))

    async def delete_comment(self, request: Request, caller: Caller) -> Answered:
        section = f"{COMMENTS}#delete-an-issue-comment"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        comment = self.comment_of(request, repository, section)
        self.agent(caller)
        self._api.world.delete_comment(repository, comment, actor=Actor.AGENT)
        return Answered(204, b"")

    async def list_repository_comments(self, request: Request, caller: Caller) -> Answered:
        section = f"{COMMENTS}#list-issue-comments-for-a-repository"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        found = self._api.world.comments(repository)
        if param(request, "sort") is not None:
            if param(request, "direction") is None:
                raise NotServed("a sort without a direction: the reference gives no default")
            sort = chosen(request, "sort", wire.CommentSort, wire.CommentSort.CREATED)
            direction = chosen(request, "direction", wire.Direction, wire.Direction.ASC)
            found = sorted(
                found,
                key=lambda c: (c.created_at if sort is wire.CommentSort.CREATED else c.updated_at, c.id),
                reverse=direction is wire.Direction.DESC,
            )
        since = param(request, "since")
        if since is not None:
            floor = moment(since)
            found = [c for c in found if c.updated_at >= floor]
        start, end, links = paged(request, len(found))
        self._api.world.saw(state.repository_ref(owner, name), Operation.READ)
        return as_json([self.comment_out(repository, c) for c in found[start:end]], headers=links)

    # ------------------------------------------------------------------ labels

    async def list_labels(self, request: Request, caller: Caller) -> Answered:
        section = f"{LABELS}#list-labels-for-a-repository"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        found = self._api.world.labels(repository)
        start, end, links = paged(request, len(found))
        self._api.world.saw(state.repository_ref(owner, name), Operation.READ)
        return as_json([self.label_out(repository, label) for label in found[start:end]], headers=links)

    async def create_label(self, request: Request, caller: Caller) -> Answered:
        section = f"{LABELS}#create-a-label"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        self.agent(caller)
        sent = wire.Sent.read(await request.body(), section=section)
        label = sent.text("name", required=True)
        assert label is not None
        color = sent.text("color")
        description = sent.text("description")
        if color is None:
            raise NotServed(
                "a label without a color: the reference calls it required in its text and optional in its schema"
            )
        color = color.removeprefix("#")
        if len(color) != 6 or any(c not in "0123456789abcdefABCDEF" for c in color):
            raise wire.validation_failed(section, wire.FieldError(resource="Label", field="color", code="invalid"))
        if description is not None and len(description) > 100:
            raise wire.validation_failed(
                section, wire.FieldError(resource="Label", field="description", code="invalid")
            )
        if self._api.world.label(repository, label) is not None:
            raise wire.validation_failed(
                section, wire.FieldError(resource="Label", field="name", code="already_exists")
            )
        stored = wire.StoredLabel(
            id=self._api.world.next_id("label", first=FIRST_ID, actor=Actor.AGENT),
            name=label,
            color=color,
            description=description,
        )
        self._api.world.put_label(repository, stored, operation=Operation.CREATE, actor=Actor.AGENT)
        return as_json(self.label_out(repository, stored), 201)

    async def get_label(self, request: Request, caller: Caller) -> Answered:
        section = f"{LABELS}#get-a-label"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        found = self._api.world.label(repository, request.path_params["name"])
        if found is None:
            raise wire.not_found(section)
        self._api.world.saw(state.repository_ref(owner, name), Operation.READ)
        return as_json(self.label_out(repository, found))

    def labels_on(self, repository: wire.StoredRepository, issue: wire.StoredIssue) -> list[wire.Wire]:
        held = {label.name: label for label in self._api.world.labels(repository)}
        return [self.label_out(repository, held[n]) for n in issue.labels if n in held]

    async def list_issue_labels(self, request: Request, caller: Caller) -> Answered:
        section = f"{LABELS}#list-labels-for-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        shown = self.labels_on(repository, issue)
        start, end, links = paged(request, len(shown))
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json(shown[start:end], headers=links)

    def labelled(
        self, repository: wire.StoredRepository, issue: wire.StoredIssue, names: list[str], account: wire.StoredAccount
    ) -> wire.StoredIssue:
        changed = issue.model_copy(update={"labels": names, "updated_at": wire.timestamp(self._api.clock.now())})
        self._api.world.put_issue(repository, changed, operation=Operation.UPDATE, actor=Actor.AGENT)
        return changed

    async def add_labels(self, request: Request, caller: Caller) -> Answered:
        section = f"{LABELS}#add-labels-to-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        account = self.agent(caller)
        sent = await self.label_body(request, section)
        names = self.defined(repository, sent)
        changed = self.labelled(
            repository, issue, [*issue.labels, *(n for n in names if n not in issue.labels)], account
        )
        return as_json(self.labels_on(repository, changed))

    async def set_labels(self, request: Request, caller: Caller) -> Answered:
        section = f"{LABELS}#set-labels-for-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        account = self.agent(caller)
        names = self.defined(repository, await self.label_body(request, section))
        changed = self.labelled(repository, issue, names, account)
        return as_json(self.labels_on(repository, changed))

    async def label_body(self, request: Request, section: str) -> list[str]:
        """The labels a request names, in any of the forms the reference lists: an object with `labels`, an array of
        names or of objects with a `name`, and (set only) a bare name."""
        raw = await request.body()
        if not raw.strip():
            return []
        found = wire.Sent.parsed(raw)
        sent = wire.Sent(fields={}, section=section)
        if isinstance(found, str):
            return [found]
        if isinstance(found, dict):
            if "labels" not in found:
                return []
            found = found["labels"]
        if isinstance(found, list) and any(isinstance(i, dict) and "suggest" in i for i in found):
            raise NotServed("a label added as a suggestion: the world holds no suggestions")
        return label_list(found, sent)

    async def clear_labels(self, request: Request, caller: Caller) -> Answered:
        section = f"{LABELS}#remove-all-labels-from-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        account = self.agent(caller)
        if issue.labels:
            self.labelled(repository, issue, [], account)
        return Answered(204, b"")

    async def remove_label(self, request: Request, caller: Caller) -> Answered:
        section = f"{LABELS}#remove-a-label-from-an-issue"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.issue_of(request, repository, section)
        account = self.agent(caller)
        label = request.path_params["name"]
        # "This endpoint returns a `404 Not Found` status if the label does not exist."
        if label not in issue.labels:
            raise wire.not_found(section)
        changed = self.labelled(repository, issue, [n for n in issue.labels if n != label], account)
        return as_json(self.labels_on(repository, changed))
