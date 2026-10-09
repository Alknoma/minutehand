"""Pull requests, their files and commits, their reviews and the comments on their diffs.

A pull request is an issue with a head branch and a base (`StoredIssue.pull`), numbered with the repository's issues
(https://docs.github.com/en/rest/issues/issues#list-repository-issues). Its head is a branch with commits of its own
(`history.StoredLine`) and its base the repository's default branch. What it changes is the difference between the
tree its head was made at and the tree its head has; whether it merges cleanly is worked out from the three trees
where that can be known, and is `null` ("GitHub has started a background job to compute the mergeability") where it
cannot.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from hashlib import sha1
from typing import TYPE_CHECKING

from starlette.requests import Request

from minutehand.adapters.providers.github import content, diffs, history, state, wire
from minutehand.adapters.providers.github.answers import Answered, Caller, as_json, chosen, paged, param
from minutehand.adapters.providers.github.manifest import MANIFEST
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.adapters.providers.github.tracker import FIRST_ID, FORBIDDEN, PUSH, Tracker, moment
from minutehand.domain.errors import NotServed
from minutehand.domain.transitions import Transition, transition_change
from minutehand.domain.world import Actor, Operation

if TYPE_CHECKING:
    from minutehand.adapters.providers.github.app import GitHubApi

PULLS = "/pulls/pulls"
REVIEWS = "/pulls/reviews"
REVIEW_COMMENTS = "/pulls/comments"
MERGED = "Pull Request successfully merged"
"""The message of a successful merge, as the reference shows it: https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request"""
NOT_MERGEABLE = "Method Not Allowed"
"""The reference documents 405 "if merge cannot be performed" and gives no message; the status's name stands."""
CONFLICT = "Conflict"
"""The reference documents 409 "if sha was provided and pull request head did not match"; the status's name stands."""
REVIEW_STATES = {
    wire.ReviewEvent.APPROVE: wire.ReviewState.APPROVED,
    wire.ReviewEvent.REQUEST_CHANGES: wire.ReviewState.CHANGES_REQUESTED,
    wire.ReviewEvent.COMMENT: wire.ReviewState.COMMENTED,
}
"""The state each event leaves a review in, in GraphQL's `PullRequestReviewState` words."""
REVIEW_WORDS = {
    wire.ReviewState.APPROVED: "approved",
    wire.ReviewState.CHANGES_REQUESTED: "changes requested",
    wire.ReviewState.COMMENTED: "commented",
}
"""What a pull request is said to be after a review of each state, in plain words."""


@dataclass(frozen=True)
class Tips:
    """The commits a pull request stands between: its base's, and its head's."""

    base: str
    head: str


@dataclass(frozen=True)
class Entry:
    """One file a pull request changes."""

    path: str
    status: wire.ChangeStatus
    old: bytes | None
    new: bytes | None
    old_sha: str | None
    new_sha: str | None
    counts: diffs.Counts

    def patch(self) -> str:
        return diffs.patch(self.lines(self.old), self.lines(self.new))

    @staticmethod
    def lines(raw: bytes | None) -> list[str]:
        return [] if raw is None else diffs.split(raw.decode("utf-8"))


def tips_of(world: GitHubWorld, repository: wire.StoredRepository, issue: wire.StoredIssue) -> Tips:
    """The base's head and the head's: live while the pull request is open, as they were once it is closed."""
    pull = issue.pull
    assert pull is not None
    live = history.tip(repository, pull.head)
    head = pull.head_sha or live
    base = pull.base_sha or (repository.commits[0].sha if repository.commits else None)
    if head is None or base is None:
        raise LookupError(f"{repository.full_name}#{issue.number} stands between commits the repository has not")
    return Tips(base=base, head=head)


def line_of(repository: wire.StoredRepository, issue: wire.StoredIssue) -> wire.StoredLine:
    pull = issue.pull
    assert pull is not None
    line = history.line_named(repository, pull.head)
    if line is None:
        raise LookupError(f"{repository.full_name}#{issue.number} is from {pull.head}, which the repository has not")
    return line


def entries_of(world: GitHubWorld, repository: wire.StoredRepository, issue: wire.StoredIssue) -> list[Entry]:
    """The files the pull request changes: what its head has that the tree it was made at has not, by path."""
    line = line_of(repository, issue)
    tips = tips_of(world, repository, issue)
    before = {e.path: e.sha for e in line.base}
    after = history.line_tree(line, upto=tips.head)
    found: list[Entry] = []
    for change in history.changes_between(before, after):
        old = bytes_of(world, repository, change.previous_sha)
        new = bytes_of(world, repository, change.sha)
        if content.is_binary(old or b"") or content.is_binary(new or b""):
            raise NotServed("a binary file in a pull request's diff: the reference does not say how it is counted")
        counts = diffs.counts(Entry.lines(old), Entry.lines(new))
        found.append(
            Entry(
                path=change.path,
                status=change.status,
                old=old,
                new=new,
                old_sha=change.previous_sha,
                new_sha=change.sha,
                counts=counts,
            )
        )
    return found


def bytes_of(world: GitHubWorld, repository: wire.StoredRepository, sha: str | None) -> bytes | None:
    if sha is None:
        return None
    blob = world.blob(repository, sha)
    if blob is None:
        raise LookupError(f"{repository.full_name} holds no blob {sha}")
    return base64.b64decode(blob.content)


def merges_cleanly(world: GitHubWorld, repository: wire.StoredRepository, issue: wire.StoredIssue) -> bool | None:
    """Whether the head merges into the base without a conflict, where the three trees say: true when no path it
    changes has been changed otherwise on the default branch since it was made. A path changed on both sides
    differently is something only a merge of their lines could settle, which this does not do: `null`, the
    reference's "not yet computed"."""
    pull = issue.pull
    assert pull is not None
    if pull.merged:
        return None
    line = line_of(repository, issue)
    before = {e.path: e.sha for e in line.base}
    after = history.line_tree(line, upto=tips_of(world, repository, issue).head)
    trunk = history.trunk_tree(world, repository)
    for change in history.changes_between(before, after):
        there = trunk[change.path] if change.path in trunk else None
        if there != change.previous_sha and there != change.sha:
            return None
    return True


class Pulls:
    def __init__(self, api: GitHubApi) -> None:
        self._api = api

    @property
    def tracker(self) -> Tracker:
        return self._api.tracker

    # ------------------------------------------------------------------ what a pull request is made of

    def pull_of(self, request: Request, repository: wire.StoredRepository, section: str) -> wire.StoredIssue:
        found = self._api.world.issue(repository, int(request.path_params["pull_number"]))
        if found is None or found.pull is None:
            raise wire.not_found(section)
        return found

    def merge_commit_sha(self, repository: wire.StoredRepository, issue: wire.StoredIssue) -> str | None:
        """Before it is merged, the commit GitHub made to test the merge, which exists when the merge is clean; once
        merged, the merge commit."""
        pull = issue.pull
        assert pull is not None
        if pull.merged:
            return pull.merge_commit_sha
        if merges_cleanly(self._api.world, repository, issue) is not True:
            return None
        tips = tips_of(self._api.world, repository, issue)
        return sha1(f"test merge\0{repository.full_name}\0{tips.base}\0{tips.head}".encode()).hexdigest()

    # ------------------------------------------------------------------ presenting

    def side(
        self, repository: wire.StoredRepository, label_owner: str, ref: str, sha: str, shown: wire.RepositoryOut
    ) -> wire.PullSideOut:
        return wire.PullSideOut(
            label=f"{label_owner}:{ref}",
            ref=ref,
            sha=sha,
            user=self._api.account_out(repository.owner),
            repo=shown,
        )

    def pull_out(
        self,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        *,
        shown: wire.RepositoryOut,
        labels: dict[str, wire.StoredLabel],
        full: bool,
    ) -> wire.PullSimpleOut:
        pull = issue.pull
        assert pull is not None
        full_name = repository.full_name
        at = f"{wire.API}/repos/{full_name}/pulls/{issue.number}"
        web = f"{wire.WEB}/{full_name}/pull/{issue.number}"
        tips = tips_of(self._api.world, repository, issue)
        assignees = [self._api.account_out(login) for login in issue.assignees]
        common = {
            "url": at,
            "id": pull.id,
            "node_id": wire.node_id("PR", pull.id),
            "html_url": web,
            "diff_url": f"{web}.diff",
            "patch_url": f"{web}.patch",
            "issue_url": f"{wire.API}/repos/{full_name}/issues/{issue.number}",
            "commits_url": f"{at}/commits",
            "review_comments_url": f"{at}/comments",
            "review_comment_url": f"{wire.API}/repos/{full_name}/pulls/comments{{/number}}",
            "comments_url": f"{wire.API}/repos/{full_name}/issues/{issue.number}/comments",
            "statuses_url": f"{wire.API}/repos/{full_name}/statuses/{tips.head}",
            "number": issue.number,
            "state": issue.state,
            "locked": issue.locked,
            "title": issue.title,
            "user": self._api.account_out(issue.author),
            "body": issue.body,
            "labels": [self.tracker.label_out(repository, labels[n]) for n in issue.labels if n in labels],
            "active_lock_reason": issue.lock_reason,
            "created_at": issue.created_at,
            "updated_at": issue.updated_at,
            "closed_at": issue.closed_at,
            "merged_at": pull.merged_at,
            "merge_commit_sha": self.merge_commit_sha(repository, issue),
            "assignee": assignees[0] if assignees else None,
            "assignees": assignees,
            "requested_reviewers": [self._api.account_out(login) for login in pull.requested_reviewers],
            "head": self.side(repository, repository.owner, pull.head, tips.head, shown),
            "base": self.side(repository, repository.owner, pull.base, tips.base, shown),
            "_links": wire.PullLinksOut.model_validate(
                {
                    "self": wire.HrefOut(href=at),
                    "html": wire.HrefOut(href=web),
                    "issue": wire.HrefOut(href=f"{wire.API}/repos/{full_name}/issues/{issue.number}"),
                    "comments": wire.HrefOut(href=f"{wire.API}/repos/{full_name}/issues/{issue.number}/comments"),
                    "review_comments": wire.HrefOut(href=f"{at}/comments"),
                    "review_comment": wire.HrefOut(href=f"{wire.API}/repos/{full_name}/pulls/comments{{/number}}"),
                    "commits": wire.HrefOut(href=f"{at}/commits"),
                    "statuses": wire.HrefOut(href=f"{wire.API}/repos/{full_name}/statuses/{tips.head}"),
                }
            ),
            "author_association": self.tracker.association(repository, issue.author),
            "draft": pull.draft,
        }
        if not full:
            return wire.PullSimpleOut.model_validate(common)
        entries = entries_of(self._api.world, repository, issue)
        merger = None if pull.merged_by is None else self._api.account_out(pull.merged_by)
        mergeable = merges_cleanly(self._api.world, repository, issue)
        return wire.PullOut.model_validate(
            {
                **common,
                "merged": pull.merged,
                "mergeable": mergeable,
                "mergeable_state": "clean" if mergeable else "unknown",
                "merged_by": merger,
                "comments": sum(1 for c in self._api.world.comments(repository) if c.issue == issue.number),
                "review_comments": sum(
                    1 for c in self._api.world.review_comments(repository) if c.pull == issue.number
                ),
                "maintainer_can_modify": pull.maintainer_can_modify,
                "commits": len(history.between(repository, tips.base, tips.head)),
                "additions": sum(e.counts.additions for e in entries),
                "deletions": sum(e.counts.deletions for e in entries),
                "changed_files": len(entries),
            }
        )

    def present(
        self, caller: Caller, repository: wire.StoredRepository, issues: list[wire.StoredIssue], *, full: bool
    ) -> list[wire.PullSimpleOut]:
        shown = self._api.repository_out(caller, repository)
        labels = {label.name: label for label in self._api.world.labels(repository)}
        return [self.pull_out(repository, i, shown=shown, labels=labels, full=full) for i in issues]

    # ------------------------------------------------------------------ pull requests

    async def list_pulls(self, request: Request, caller: Caller) -> Answered:
        section = f"{PULLS}#list-pull-requests"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        status = chosen(request, "state", wire.StateFilter, wire.StateFilter.OPEN)
        sort = chosen(request, "sort", wire.PullSort, wire.PullSort.CREATED)
        if sort is wire.PullSort.LONG_RUNNING:
            raise NotServed("sort=long-running: it limits results by how long a pull request has been open and active")
        # "Default: `desc` when sort is `created` or sort is not specified, otherwise `asc`."
        fallback = wire.Direction.DESC if sort is wire.PullSort.CREATED else wire.Direction.ASC
        direction = chosen(request, "direction", wire.Direction, fallback)
        pulls = [i for i in self._api.world.issues(repository) if i.pull is not None]
        if status is not wire.StateFilter.ALL:
            pulls = [i for i in pulls if i.state.value == status.value]
        base = param(request, "base")
        if base is not None:
            pulls = [i for i in pulls if i.pull is not None and i.pull.base == base]
        head = param(request, "head")
        if head is not None:
            owner_part, _, ref = head.rpartition(":")
            pulls = [
                i
                for i in pulls
                if i.pull is not None and i.pull.head == ref and owner_part.lower() == repository.owner.lower()
            ]
        counts = {
            i.number: sum(1 for c in self._api.world.comments(repository) if c.issue == i.number)
            + sum(1 for c in self._api.world.review_comments(repository) if c.pull == i.number)
            for i in pulls
        }
        key = {
            wire.PullSort.CREATED: lambda i: (i.created_at, i.number),
            wire.PullSort.UPDATED: lambda i: (i.updated_at, i.number),
            wire.PullSort.POPULARITY: lambda i: (counts[i.number], i.number),
        }[sort]
        pulls = sorted(pulls, key=key, reverse=direction is wire.Direction.DESC)
        start, end, links = paged(request, len(pulls))
        self._api.world.saw(state.repository_ref(owner, name), Operation.SEARCH)
        return as_json(list(self.present(caller, repository, pulls[start:end], full=False)), headers=links)

    def may_change(self, caller: Caller, repository: wire.StoredRepository) -> bool:
        """ "To open or update a pull request in a public repository, you must have write access to the head or the
        source branch. For organization-owned repositories, you must be a member of the organization that owns the
        repository." https://docs.github.com/en/rest/pulls/pulls#create-a-pull-request"""
        if self.tracker.rank(caller, repository) >= PUSH:
            return True
        owner = self._api.world.account(repository.owner)
        return (
            owner is not None
            and owner.type is wire.AccountType.ORGANIZATION
            and caller.account is not None
            and caller.account.login.lower() in {m.lower() for m in owner.members}
        )

    async def create_pull(self, request: Request, caller: Caller) -> Answered:
        section = f"{PULLS}#create-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        account = self.tracker.agent(caller)
        sent = wire.Sent.read(await request.body(), section=section)
        title = sent.text("title")
        base = sent.text("base", required=True)
        head = sent.text("head", required=True)
        body = sent.text("body")
        draft = sent.flag("draft")
        modify = sent.flag("maintainer_can_modify")
        sent.refuse("head_repo", why="a pull request from another repository")
        sent.refuse("issue", why="converting an issue to a pull request")
        if title is None or not title.strip():
            raise sent.invalid()
        assert base is not None and head is not None
        owner_part, colon, ref = head.rpartition(":")
        if colon and owner_part.lower() != repository.owner.lower():
            raise NotServed("a pull request from a branch of another repository")
        line = history.line_named(repository, ref)
        if base != repository.default_branch:
            raise NotServed(f"a pull request into {base}, which is not the default branch {repository.default_branch}")
        if line is None or not line.commits:
            raise NotServed(f"a pull request from {ref}, which has no commits of its own beyond {base}")
        if not self.may_change(caller, repository):
            raise wire.Refusal(403, FORBIDDEN, section=section)
        world = self._api.world
        if any(
            i.pull is not None and i.pull.head == ref and i.state is wire.IssueState.OPEN
            for i in world.issues(repository)
        ):
            raise NotServed(f"a second open pull request from {ref}")
        issue = await self.open_pull(
            repository,
            account,
            title=title,
            body=body,
            head=ref,
            draft=bool(draft),
            maintainer_can_modify=bool(modify),
            actor=Actor.AGENT,
            who=None,
        )
        return as_json(self.present(caller, repository, [issue], full=True)[0], 201)

    async def open_pull(
        self,
        repository: wire.StoredRepository,
        account: wire.StoredAccount,
        *,
        title: str,
        body: str | None,
        head: str,
        draft: bool,
        maintainer_can_modify: bool,
        actor: Actor,
        who: str | None,
    ) -> wire.StoredIssue:
        world = self._api.world
        number = max((i.number for i in world.issues(repository)), default=0) + 1
        now = wire.timestamp(self._api.clock.now())
        issue = wire.StoredIssue(
            number=number,
            id=world.next_id("issue", first=FIRST_ID, actor=actor),
            title=title,
            body=body,
            author=account.login,
            created_at=now,
            updated_at=now,
            pull=wire.StoredPull(
                id=world.next_id("pull", first=FIRST_ID, actor=actor),
                head=head,
                base=repository.default_branch,
                draft=draft,
                maintainer_can_modify=maintainer_can_modify,
            ),
        )
        world.put_issue(repository, issue, operation=Operation.CREATE, actor=actor)
        self.tracker.moved_to(repository, issue, "open", None, actor, who, "{}")
        return issue

    async def get_pull(self, request: Request, caller: Caller) -> Answered:
        section = f"{PULLS}#get-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json(self.present(caller, repository, [issue], full=True)[0])

    async def update_pull(self, request: Request, caller: Caller) -> Answered:
        section = f"{PULLS}#update-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        account = self.tracker.agent(caller)
        if not self.may_change(caller, repository):
            raise wire.Refusal(403, FORBIDDEN, section=section)
        sent = wire.Sent.read(await request.body(), section=section, optional=True)
        pull = issue.pull
        assert pull is not None
        changes: dict[str, object] = {}
        base = sent.text("base")
        if base is not None and base != pull.base:
            raise NotServed(f"moving a pull request's base to {base}")
        if sent.named("title"):
            title = sent.text("title", required=True)
            if title is None or not title.strip():
                raise sent.invalid()
            changes["title"] = title
        if sent.named("body"):
            changes["body"] = sent.text("body")
        modify = sent.flag("maintainer_can_modify")
        if modify is not None and modify != pull.maintainer_can_modify:
            changes["pull"] = pull.model_copy(update={"maintainer_can_modify": modify})
        wanted = sent.member("state", wire.IssueState)
        changed = issue.model_copy(update=changes)
        if wanted is not None and wanted is not issue.state:
            changed = self.tracker.moved(changed, wanted, None, account, wire.timestamp(self._api.clock.now()))
            changed = self.frozen(repository, changed)
        changed = await self.tracker.edit_issue(repository, issue, changed, actor=Actor.AGENT, who=None)
        return as_json(self.present(caller, repository, [changed], full=True)[0])

    def frozen(self, repository: wire.StoredRepository, issue: wire.StoredIssue) -> wire.StoredIssue:
        """A pull request just closed keeps the commits it stood between; one reopened follows its branches again."""
        pull = issue.pull
        assert pull is not None
        if issue.state is wire.IssueState.CLOSED:
            tips = tips_of(self._api.world, repository, issue)
            return issue.model_copy(
                update={"pull": pull.model_copy(update={"head_sha": tips.head, "base_sha": tips.base})}
            )
        return issue.model_copy(update={"pull": pull.model_copy(update={"head_sha": None, "base_sha": None})})

    async def list_files(self, request: Request, caller: Caller) -> Answered:
        section = f"{PULLS}#list-pull-requests-files"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        tips = tips_of(self._api.world, repository, issue)
        full = repository.full_name
        listed: list[wire.Wire] = []
        for entry in entries_of(self._api.world, repository, issue):
            at = tips.base if entry.status is wire.ChangeStatus.REMOVED else tips.head
            listed.append(
                wire.DiffEntryOut(
                    sha=entry.new_sha if entry.new_sha is not None else entry.old_sha,
                    filename=entry.path,
                    status=entry.status,
                    additions=entry.counts.additions,
                    deletions=entry.counts.deletions,
                    changes=entry.counts.additions + entry.counts.deletions,
                    blob_url=f"{wire.WEB}/{full}/blob/{at}/{entry.path}",
                    raw_url=f"{wire.WEB}/{full}/raw/{at}/{entry.path}",
                    contents_url=f"{wire.API}/repos/{full}/contents/{entry.path}?ref={at}",
                    patch=entry.patch(),
                )
            )
        start, end, links = paged(request, len(listed))
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json(listed[start:end], headers=links)

    async def list_commits(self, request: Request, caller: Caller) -> Answered:
        section = f"{PULLS}#list-commits-on-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        tips = tips_of(self._api.world, repository, issue)
        found = history.between(repository, tips.base, tips.head)
        start, end, links = paged(request, len(found))
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json([self._api.commit_out(repository, c) for c in found[start:end]], headers=links)

    async def check_merged(self, request: Request, caller: Caller) -> Answered:
        section = f"{PULLS}#check-if-a-pull-request-has-been-merged"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        assert issue.pull is not None
        if not issue.pull.merged:
            raise wire.not_found(section)
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return Answered(204, b"")

    async def merge_pull(self, request: Request, caller: Caller) -> Answered:
        section = f"{PULLS}#merge-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        account = self.tracker.agent(caller)
        if self.tracker.rank(caller, repository) < PUSH:
            raise wire.Refusal(403, FORBIDDEN, section=section)
        sent = wire.Sent.read(await request.body(), section=section, optional=True)
        method = sent.text("merge_method")
        if method is not None and method != "merge":
            raise NotServed(f"merge_method={method}: the reference does not give the message of a {method} commit")
        title = sent.text("commit_title")
        extra = sent.text("commit_message")
        wanted = sent.text("sha")
        pull = issue.pull
        assert pull is not None
        if issue.state is not wire.IssueState.OPEN or pull.draft:
            raise wire.Refusal(405, NOT_MERGEABLE, section=section)
        tips = tips_of(self._api.world, repository, issue)
        if wanted is not None and wanted != tips.head:
            raise wire.Refusal(409, CONFLICT, section=section)
        if merges_cleanly(self._api.world, repository, issue) is not True:
            raise NotServed("merging a pull request whose changes meet changes on the default branch in the same files")
        merged = await self.merge(repository, issue, account, title=title, extra=extra, actor=Actor.AGENT, who=None)
        assert merged.pull is not None and merged.pull.merge_commit_sha is not None
        return as_json(wire.MergeResultOut(sha=merged.pull.merge_commit_sha, merged=True, message=MERGED))

    async def merge(
        self,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        account: wire.StoredAccount,
        *,
        title: str | None,
        extra: str | None,
        actor: Actor,
        who: str | None,
    ) -> wire.StoredIssue:
        """The pull request merged into the default branch by a merge commit: the head's changes laid on the default
        branch's tree, a commit of two parents, the pull request closed as merged."""
        pull = issue.pull
        assert pull is not None
        world = self._api.world
        line = line_of(repository, issue)
        tips = tips_of(self._api.world, repository, issue)
        base_tree = {e.path: e.sha for e in line.base}
        head_tree = history.line_tree(line, upto=tips.head)
        trunk = history.trunk_tree(world, repository)
        merged_tree = dict(trunk)
        for change in history.changes_between(base_tree, head_tree):
            if change.sha is None:
                merged_tree.pop(change.path, None)
            else:
                merged_tree[change.path] = change.sha
        changes = history.changes_between(trunk, merged_tree)
        now = wire.timestamp(self._api.clock.now())
        # The default message is recorded of the real service (tests/data/github_rest/): "Merge pull request #N from
        # <owner>/<branch>" and the pull request's title below it; `commit_title` and `commit_message` replace the
        # first and are appended to it, as the reference words them.
        first = (
            title if title is not None else f"Merge pull request #{issue.number} from {repository.owner}/{pull.head}"
        )
        paragraphs = [first, issue.title, *([extra] if extra else [])]
        commit = history.new_commit(
            repository,
            message="\n\n".join(paragraphs),
            by=history.authorship(account, now),
            parent=tips.base,
            merged=tips.head,
            changes=changes,
        )
        history.commit_to_trunk(world, repository, commit, [], actor=actor)
        done = issue.model_copy(
            update={
                "state": wire.IssueState.CLOSED,
                "closed_at": now,
                "closed_by": account.login,
                "pull": pull.model_copy(
                    update={
                        "merged": True,
                        "merged_at": now,
                        "merged_by": account.login,
                        "merge_commit_sha": commit.sha,
                        "head_sha": tips.head,
                        "base_sha": tips.base,
                    }
                ),
                "updated_at": now,
            }
        )
        world.put_issue(repository, done, operation=Operation.UPDATE, actor=actor)
        moved = Transition(
            provider=MANIFEST.key,
            item=state.issue_ref(repository.owner, repository.name, issue.number),
            name="merge",
            from_state=issue.state.value,
            to_state="merged",
            by=actor,
            who=who,
            content="{}",
            at=self._api.clock.now(),
        )
        world.store.apply(transition_change(moved, at_seq=world.store.head() + 1))
        return done

    # ------------------------------------------------------------------ reviews

    def review_out(self, repository: wire.StoredRepository, review: wire.StoredReview) -> wire.ReviewOut:
        full = repository.full_name
        web = f"{wire.WEB}/{full}/pull/{review.pull}#pullrequestreview-{review.id}"
        return wire.ReviewOut.model_validate(
            {
                "id": review.id,
                "node_id": wire.node_id("PRR", review.id),
                "user": self._api.account_out(review.author),
                "body": review.body,
                "state": review.state,
                "html_url": web,
                "pull_request_url": f"{wire.API}/repos/{full}/pulls/{review.pull}",
                "author_association": self.tracker.association(repository, review.author),
                "_links": wire.ReviewLinksOut(
                    html=wire.HrefOut(href=web),
                    pull_request=wire.HrefOut(href=f"{wire.API}/repos/{full}/pulls/{review.pull}"),
                ),
                "submitted_at": review.submitted_at,
                "commit_id": review.commit_id,
            }
        )

    async def list_reviews(self, request: Request, caller: Caller) -> Answered:
        section = f"{REVIEWS}#list-reviews-for-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        found = [r for r in self._api.world.reviews(repository) if r.pull == issue.number]
        start, end, links = paged(request, len(found))
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json([self.review_out(repository, r) for r in found[start:end]], headers=links)

    async def get_review(self, request: Request, caller: Caller) -> Answered:
        section = f"{REVIEWS}#get-a-review-for-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        found = self.review_of(request, repository, issue, section)
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json(self.review_out(repository, found))

    def review_of(
        self, request: Request, repository: wire.StoredRepository, issue: wire.StoredIssue, section: str
    ) -> wire.StoredReview:
        found = next(
            (
                r
                for r in self._api.world.reviews(repository)
                if r.pull == issue.number and r.id == int(request.path_params["review_id"])
            ),
            None,
        )
        if found is None:
            raise wire.not_found(section)
        return found

    async def create_review(self, request: Request, caller: Caller) -> Answered:
        section = f"{REVIEWS}#create-a-review-for-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        account = self.tracker.agent(caller)
        sent = wire.Sent.read(await request.body(), section=section, optional=True)
        text = sent.text("body")
        event = sent.member("event", wire.ReviewEvent)
        commit_id = sent.text("commit_id")
        drafts = sent.fields["comments"] if "comments" in sent.fields else None
        if event is None:
            raise NotServed("a review with no event: it is left PENDING until it is submitted, which is not served")
        if drafts:
            raise NotServed("comments in a review: they are drafts on the diff, written with the review")
        review = await self.submit_review(
            repository, issue, account, event=event, body=text, commit_id=commit_id, actor=Actor.AGENT, who=None
        )
        return as_json(self.review_out(repository, review))

    async def submit_review(
        self,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        account: wire.StoredAccount,
        *,
        event: wire.ReviewEvent,
        body: str | None,
        commit_id: str | None,
        actor: Actor,
        who: str | None,
    ) -> wire.StoredReview:
        """A review submitted on a pull request, as `POST /repos/{owner}/{repo}/pulls/{n}/reviews` submits it."""
        section = f"{REVIEWS}#create-a-review-for-a-pull-request"
        if event is not wire.ReviewEvent.APPROVE and (body is None or not body.strip()):
            # "**Required** when using `REQUEST_CHANGES` or `COMMENT` for the `event` parameter."
            raise wire.Refusal(422, "Invalid request", section=section)
        if event is not wire.ReviewEvent.COMMENT and issue.author.lower() == account.login.lower():
            raise NotServed(
                "approving or requesting changes on one's own pull request: GitHub refuses it, with no message given"
            )
        tips = tips_of(self._api.world, repository, issue)
        if commit_id is not None and commit_id != tips.head:
            raise NotServed("a review of a commit other than the pull request's head")
        world = self._api.world
        now = wire.timestamp(self._api.clock.now())
        review = wire.StoredReview(
            id=world.next_id("review", first=FIRST_ID, actor=actor),
            pull=issue.number,
            author=account.login,
            state=REVIEW_STATES[event],
            body="" if body is None else body,
            commit_id=tips.head,
            submitted_at=now,
        )
        world.put_review(repository, review, operation=Operation.CREATE, actor=actor)
        moved = Transition(
            provider=MANIFEST.key,
            item=state.issue_ref(repository.owner, repository.name, issue.number),
            name=event.value,
            from_state=issue.state.value,
            to_state=REVIEW_WORDS[review.state],
            by=actor,
            who=who,
            content="{}" if body is None else json.dumps({"body": body}),
            at=self._api.clock.now(),
        )
        world.store.apply(transition_change(moved, at_seq=world.store.head() + 1))
        return review

    # ------------------------------------------------------------------ comments on the diff

    def review_comment_out(
        self, repository: wire.StoredRepository, comment: wire.StoredReviewComment
    ) -> wire.ReviewCommentOut:
        full = repository.full_name
        at = f"{wire.API}/repos/{full}/pulls/comments/{comment.id}"
        web = f"{wire.WEB}/{full}/pull/{comment.pull}#discussion_r{comment.id}"
        pull = f"{wire.API}/repos/{full}/pulls/{comment.pull}"
        return wire.ReviewCommentOut.model_validate(
            {
                "url": at,
                "pull_request_review_id": comment.review,
                "id": comment.id,
                "node_id": wire.node_id("PRRC", comment.id),
                "diff_hunk": comment.diff_hunk,
                "path": comment.path,
                "position": comment.position,
                "original_position": comment.position,
                "commit_id": comment.commit_id,
                "original_commit_id": comment.commit_id,
                "in_reply_to_id": comment.in_reply_to,
                "user": self._api.account_out(comment.author),
                "body": comment.body,
                "created_at": comment.created_at,
                "updated_at": comment.updated_at,
                "html_url": web,
                "pull_request_url": pull,
                "author_association": self.tracker.association(repository, comment.author),
                "_links": wire.ReviewCommentLinksOut.model_validate(
                    {
                        "self": wire.HrefOut(href=at),
                        "html": wire.HrefOut(href=web),
                        "pull_request": wire.HrefOut(href=pull),
                    }
                ),
                "start_line": comment.start_line,
                "original_start_line": comment.start_line,
                "start_side": comment.start_side,
                "line": comment.line,
                "original_line": comment.line,
                "side": comment.side,
                "subject_type": "line",
            }
        )

    async def list_review_comments(self, request: Request, caller: Caller) -> Answered:
        section = f"{REVIEW_COMMENTS}#list-review-comments-on-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        found = [c for c in self._api.world.review_comments(repository) if c.pull == issue.number]
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
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json([self.review_comment_out(repository, c) for c in found[start:end]], headers=links)

    async def list_review_comments_of_review(self, request: Request, caller: Caller) -> Answered:
        section = f"{REVIEWS}#list-comments-for-a-pull-request-review"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        review = self.review_of(request, repository, issue, section)
        found = [c for c in self._api.world.review_comments(repository) if c.review == review.id]
        start, end, links = paged(request, len(found))
        self._api.world.saw(state.issue_ref(owner, name, issue.number), Operation.READ)
        return as_json([self.review_comment_out(repository, c) for c in found[start:end]], headers=links)

    async def create_review_comment(self, request: Request, caller: Caller) -> Answered:
        section = f"{REVIEW_COMMENTS}#create-a-review-comment-for-a-pull-request"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        issue = self.pull_of(request, repository, section)
        account = self.tracker.agent(caller)
        sent = wire.Sent.read(await request.body(), section=section)
        text = sent.text("body", required=True)
        commit_id = sent.text("commit_id", required=True)
        path = sent.text("path", required=True)
        reply_to = sent.whole("in_reply_to")
        line = sent.whole("line")
        position = sent.whole("position")
        side = sent.member("side", wire.Side)
        sent.refuse("start_line", "start_side", why="a comment on several lines")
        kind = sent.text("subject_type")
        if kind is not None and kind != "line":
            raise NotServed("a comment on a whole file: the reference does not say what its diff hunk is")
        assert text is not None and commit_id is not None and path is not None
        world = self._api.world
        tips = tips_of(self._api.world, repository, issue)
        if reply_to is not None:
            parent = next(
                (c for c in world.review_comments(repository) if c.id == reply_to and c.pull == issue.number), None
            )
            if parent is None:
                raise wire.Refusal(404, "Not Found", section=section)
            made = self.replying(parent, account, text)
        else:
            if commit_id != tips.head:
                raise NotServed("a comment on a commit other than the pull request's head")
            made = self.placed(repository, issue, account, text, path, line, position, side)
        stored = made.model_copy(update={"id": world.next_id("review_comment", first=FIRST_ID, actor=Actor.AGENT)})
        world.put_review_comment(repository, stored, operation=Operation.CREATE, actor=Actor.AGENT)
        return as_json(self.review_comment_out(repository, stored), 201)

    def replying(
        self, parent: wire.StoredReviewComment, account: wire.StoredAccount, text: str
    ) -> wire.StoredReviewComment:
        """A reply: "When specified, all parameters other than `body` in the request body are ignored", so it sits
        where the comment it answers does."""
        now = wire.timestamp(self._api.clock.now())
        return parent.model_copy(
            update={
                "review": None,
                "author": account.login,
                "body": text,
                "in_reply_to": parent.id,
                "created_at": now,
                "updated_at": now,
            }
        )

    def placed(
        self,
        repository: wire.StoredRepository,
        issue: wire.StoredIssue,
        account: wire.StoredAccount,
        text: str,
        path: str,
        line: int | None,
        position: int | None,
        side: wire.Side | None,
    ) -> wire.StoredReviewComment:
        """A comment on one line of the pull request's diff: the line named by its number on a side or its position
        in the patch, and the hunk up to it, as the real service shows it (recorded)."""
        section = f"{REVIEW_COMMENTS}#create-a-review-comment-for-a-pull-request"
        entry = next((e for e in entries_of(self._api.world, repository, issue) if e.path == path), None)
        if entry is None:
            raise NotServed(f"a comment on {path}, which the pull request does not change")
        patch = entry.patch()
        rows = diffs.line_numbers(patch)
        if position is not None:
            found = next((r for r in rows if r[0] == position), None)
        elif line is not None:
            wanted = wire.Side.RIGHT if side is None else side
            found = next(
                (
                    r
                    for r in rows
                    if (r[2] if wanted is wire.Side.RIGHT else r[1]) == line and not r[3].startswith("\\")
                ),
                None,
            )
        else:
            raise wire.Refusal(422, "Invalid request", section=section)
        if found is None:
            raise NotServed("a line the pull request's diff does not show: the reference gives no message for it")
        now = wire.timestamp(self._api.clock.now())
        row = found[3]
        return wire.StoredReviewComment(
            id=0,
            pull=issue.number,
            review=None,
            author=account.login,
            body=text,
            path=path,
            commit_id=tips_of(self._api.world, repository, issue).head,
            diff_hunk=diffs.hunk_through(patch, found[0]),
            position=found[0],
            line=found[2] if not row.startswith("-") else found[1],
            side=wire.Side.LEFT if row.startswith("-") else (wire.Side.RIGHT if side is None else side),
            created_at=now,
            updated_at=now,
        )
