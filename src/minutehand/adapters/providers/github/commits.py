"""Committing a file through the REST API: `PUT` and `DELETE /repos/{owner}/{repo}/contents/{path}`.

Each is one commit on a branch: the default branch, a branch that follows its head (which the commit leaves behind the
others), or a branch of its own. The commit is a `StoredCommit` with the path it changed and the blob it holds
(`history`), made with the author, committer and moment the request gives and, where it gives none, the user who made
the request and the run's clock (https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents).
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import JsonValue
from starlette.requests import Request

from minutehand.adapters.providers.github import content, history, wire
from minutehand.adapters.providers.github.answers import Answered, Caller, as_json
from minutehand.domain.errors import NotServed
from minutehand.domain.world import Actor

if TYPE_CHECKING:
    from minutehand.adapters.providers.github.app import GitHubApi

CONTENTS = "/repos/contents"
PUSH = wire.PERMISSION_ORDER.index(wire.Permission.PUSH)


class Commits:
    def __init__(self, api: GitHubApi) -> None:
        self._api = api

    # ------------------------------------------------------------------ who made the commit

    def identity(
        self, sent: wire.Sent, name: str, account: wire.StoredAccount, now: str
    ) -> tuple[str | None, str, str, str] | None:
        """An `author` or `committer` object as the request gives it: a `name` and an `email`, both required ("You'll
        receive a `422` status code if `email` is omitted"), and a `date` it may set. None when it gives none."""
        given = sent.object(name)
        if given is None:
            return None
        who, email = given["name"] if "name" in given else None, given["email"] if "email" in given else None
        if not isinstance(who, str) or not isinstance(email, str):
            raise sent.invalid()
        at = now
        stamp: JsonValue = given["date"] if "date" in given else None
        if stamp is not None:
            if not isinstance(stamp, str):
                raise sent.invalid()
            try:
                at = wire.timestamp(datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC))
            except ValueError:
                raise sent.invalid() from None
        login = self.login_of(email, account)
        return login, who, email, at

    def login_of(self, email: str, account: wire.StoredAccount) -> str | None:
        """The user a commit's email names, when the world has one: the commit's `author` and `committer` link to them."""
        if history.commit_email(account).lower() == email.lower():
            return account.login
        found = next(
            (
                a
                for a in self._api.world.accounts()
                if a.type is wire.AccountType.USER and (a.email or "").lower() == email.lower()
            ),
            None,
        )
        return None if found is None else found.login

    def authorship(self, sent: wire.Sent, account: wire.StoredAccount, now: str) -> history.Authorship:
        """ "`committer`: The person that committed the file. Default: the authenticated user." and "`author`: The author of
        the file. Default: The `committer` or the authenticated user if you omit `committer`."."""
        mine = history.authorship(account, now)
        committer = self.identity(sent, "committer", account, now)
        author = self.identity(sent, "author", account, now)
        by_committer = committer or (mine.committer_login, mine.committer_name, mine.committer_email, now)
        by_author = author or by_committer
        return history.Authorship(
            author_login=by_author[0],
            author_name=by_author[1],
            author_email=by_author[2],
            author_date=by_author[3],
            committer_login=by_committer[0],
            committer_name=by_committer[1],
            committer_email=by_committer[2],
            committer_date=by_committer[3],
        )

    # ------------------------------------------------------------------ where it goes

    def target(self, repository: wire.StoredRepository, branch: str | None) -> tuple[str, history.Tree, str | None]:
        """The branch a commit is made on, the tree it holds and the commit it points at. A
        branch that does not exist is a 404, the status the reference documents for the route."""
        name = repository.default_branch if branch is None else branch
        if not repository.commits:
            raise NotServed("committing to an empty repository: the reference does not say that it makes the branch")
        line = history.line_named(repository, name)
        if line is not None:
            return name, history.line_tree(line), history.tip(repository, name)
        if name == repository.default_branch or name in repository.branches:
            tree = history.trunk_tree(self._api.world, repository)
            return name, tree, repository.commits[0].sha
        raise wire.not_found(CONTENTS + "#create-or-update-file-contents")

    def made(
        self,
        repository: wire.StoredRepository,
        branch: str,
        message: str,
        change: wire.FileChange,
        blobs: list[wire.StoredBlob],
        by: history.Authorship,
        parent: str | None,
    ) -> wire.StoredCommit:
        commit = history.new_commit(
            repository,
            message=message,
            by=by,
            parent=parent,
            merged=None,
            changes=[change],
            salt="" if branch == repository.default_branch else f"@{branch}",
        )
        world = self._api.world
        if branch == repository.default_branch:
            history.commit_to_trunk(world, repository, commit, blobs, actor=Actor.AGENT)
        else:
            history.commit_to_line(world, repository, branch, commit, blobs, actor=Actor.AGENT)
        return commit

    # ------------------------------------------------------------------ presenting

    def file_commit(
        self,
        repository: wire.StoredRepository,
        branch: str,
        commit: wire.StoredCommit,
        file: wire.StoredFile | None,
    ) -> wire.FileCommitOut:
        full = repository.full_name
        tree = content.tree_sha(repository, "")
        person = wire.CommitAuthorOut
        parents = [p for p in (commit.parent, commit.merged) if p is not None]
        detail = wire.FileCommitDetailOut(
            sha=commit.sha,
            node_id=wire.node_id("C", int(commit.sha[:8], 16)),
            url=f"{wire.API}/repos/{full}/git/commits/{commit.sha}",
            html_url=f"{wire.WEB}/{full}/commit/{commit.sha}",
            author=person(name=commit.author_name, email=commit.author_email, date=commit.date),
            committer=person(name=commit.committer_name, email=commit.committer_email, date=commit.committer_date),
            tree=wire.ShaRefOut(sha=tree, url=f"{wire.API}/repos/{full}/git/trees/{tree}"),
            message=commit.message,
            parents=[
                wire.ParentOut(
                    sha=p,
                    url=f"{wire.API}/repos/{full}/commits/{p}",
                    html_url=f"{wire.WEB}/{full}/commit/{p}",
                )
                for p in parents
            ],
        )
        if file is None:
            return wire.FileCommitOut(content=None, commit=detail)
        git_url = f"{wire.API}/repos/{full}/git/blobs/{file.sha}"
        html_url = f"{wire.WEB}/{full}/blob/{branch}/{file.path}"
        url = f"{wire.API}/repos/{full}/contents/{file.path}?ref={branch}"
        shown = wire.ContentRefOut.model_validate(
            {
                "name": file.path.rsplit("/", 1)[-1],
                "path": file.path,
                "sha": file.sha,
                "size": file.size,
                "url": url,
                "html_url": html_url,
                "git_url": git_url,
                "download_url": f"{wire.RAW}/{full}/{branch}/{file.path}",
                "_links": wire.LinksOut.model_validate({"self": url, "git": git_url, "html": html_url}),
            }
        )
        return wire.FileCommitOut(content=shown, commit=detail)

    # ------------------------------------------------------------------ the routes

    def writer(
        self, request: Request, caller: Caller, section: str
    ) -> tuple[wire.StoredRepository, wire.StoredAccount]:
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._api.visible(caller, owner, name, section)
        if caller.account is None:
            raise wire.requires_authentication()
        if self._api.tracker.rank(caller, repository) < PUSH:
            raise NotServed("committing without push access: the reference lists no status for it")
        return repository, caller.account

    async def put(self, request: Request, caller: Caller) -> Answered:
        section = f"{CONTENTS}#create-or-update-file-contents"
        repository, account = self.writer(request, caller, section)
        path = request.path_params["path"].strip("/")
        sent = wire.Sent.read(await request.body(), section=section)
        message = sent.text("message", required=True)
        encoded = sent.text("content", required=True)
        branch = sent.text("branch")
        wanted = sent.text("sha")
        assert message is not None and encoded is not None
        if not path:
            raise wire.not_found(section)
        by = self.authorship(sent, account, wire.timestamp(self._api.clock.now()))
        try:
            raw = base64.b64decode("".join(encoded.split()), validate=True)
        except binascii.Error:
            raise NotServed("content that is not base64: the reference gives no message for it") from None
        name, tree, parent = self.target(repository, branch)
        existing = tree[path] if path in tree else None
        if existing is None:
            if wanted is not None:
                raise NotServed(f"a sha for {path}, which holds no file to replace")
            history.refuse_clash(tree, path)
        else:
            if wanted is None:
                raise sent.invalid()  # "Required if you are updating a file"
            if wanted != existing:
                raise wire.Refusal(409, "Conflict", section=section)
        blob = history.stored_blob(raw)
        if blob.sha == existing:
            raise NotServed(f"giving {path} the bytes it already has")
        status = wire.ChangeStatus.ADDED if existing is None else wire.ChangeStatus.MODIFIED
        change = wire.FileChange(path=path, status=status, sha=blob.sha, previous_sha=existing)
        commit = self.made(repository, name, message, change, [blob], by, parent)
        file = wire.StoredFile(path=path, content=blob.content, size=blob.size, sha=blob.sha)
        done = self.file_commit(repository, name, commit, file)
        return as_json(done, 201 if existing is None else 200)

    async def delete(self, request: Request, caller: Caller) -> Answered:
        section = f"{CONTENTS}#delete-a-file"
        repository, account = self.writer(request, caller, section)
        path = request.path_params["path"].strip("/")
        sent = wire.Sent.read(await request.body(), section=section)
        message = sent.text("message", required=True)
        wanted = sent.text("sha", required=True)
        branch = sent.text("branch")
        assert message is not None and wanted is not None
        by = self.authorship(sent, account, wire.timestamp(self._api.clock.now()))
        name, tree, parent = self.target(repository, branch)
        if path not in tree:
            raise wire.not_found(section)
        if wanted != tree[path]:
            raise wire.Refusal(409, "Conflict", section=section)
        change = wire.FileChange(path=path, status=wire.ChangeStatus.REMOVED, sha=None, previous_sha=tree[path])
        commit = self.made(repository, name, message, change, [], by, parent)
        return as_json(self.file_commit(repository, name, commit, None))
