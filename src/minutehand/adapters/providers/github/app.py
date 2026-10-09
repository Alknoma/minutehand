"""The GitHub REST and GraphQL reads a code-reading client makes, as an ASGI app over the run's store and clock.

Every call is checked in GitHub's order: an `X-GitHub-Api-Version` it does not serve (400), then the credential,
then the faults the scenario armed, then the route. Minutehand deliberately does not enforce credentials: every
`Authorization`, or none, is accepted, a token the world holds acting as its user and any other (an unseeded or
empty token, a JWT, an installation token, no header at all) as the world's stand-in user. What a user may see is world data: a repository the user
neither owns, collaborates on nor reaches through an organization, and that is private, is a 404, exactly as one
that does not exist. Lists are paged by `per_page` and `page` and say where the next page is in a `Link` header.
A GET answered 200 carries an `ETag`; the same GET sent with it in `If-None-Match` is a 304, which an
authorized call does not spend from its budget. A call to anything else is refused by name (501).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from datetime import timedelta

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters import answering
from minutehand.adapters.providers.github import content, graphql, search, state, wire
from minutehand.adapters.providers.github.answers import (
    PAGE_DEFAULT,
    PAGE_MAX,
    Answered,
    Caller,
    Handler,
    as_json,
    header,
    number,
    paged,
    param,
)
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.adapters.providers.github.tracker import Tracker
from minutehand.domain.errors import NotServed
from minutehand.domain.world import Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

SEARCH_CEILING = 1000
"""Search serves the first thousand results and no more."""
TEXT_MATCH = "application/vnd.github.text-match+json"
"""The media type that asks search for `text_matches`."""
INSTALLATION_TOKEN_LIFETIME = timedelta(hours=1)
"""An installation access token expires an hour after it is made.
https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app"""
INSTALLATION_TOKEN_PREFIX = "ghs_"
"""The prefix GitHub writes on an installation access token (https://github.blog/2021-04-05-behind-githubs-new-authentication-token-formats/)."""


class _Exhausted(Exception):
    """A call refused because its budget is spent; it carries its whole answer, headers and all."""

    def __init__(self, answered: Answered) -> None:
        super().__init__("rate limit exceeded")
        self.answered = answered


def _login(caller: Caller | None) -> str | None:
    return caller.account.login if caller is not None and caller.account is not None else None


def _budget_headers(budget: wire.StoredBudget, resource: wire.Resource) -> dict[str, str]:
    """The `X-RateLimit-*` headers GitHub puts on every answer, for the budget the call spent."""
    return {
        "X-RateLimit-Limit": str(budget.limit),
        "X-RateLimit-Remaining": str(budget.remaining),
        "X-RateLimit-Used": str(budget.used),
        "X-RateLimit-Reset": str(budget.reset),
        "X-RateLimit-Resource": resource.value,
    }


def _budget_out(budget: wire.StoredBudget, resource: wire.Resource) -> wire.BudgetOut:
    return wire.BudgetOut(
        limit=budget.limit, remaining=budget.remaining, used=budget.used, reset=budget.reset, resource=resource
    )


def resource_of(path: str) -> wire.Resource:
    if path == "/graphql":
        return wire.Resource.GRAPHQL
    if path == "/search/code":
        return wire.Resource.CODE_SEARCH
    if path.startswith("/search/"):
        return wire.Resource.SEARCH
    return wire.Resource.CORE


def _raw(file: wire.StoredFile) -> bytes:
    return base64.b64decode(file.content)


class GitHubApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self.world = GitHubWorld(store)
        self.clock = clock
        self.tracker = Tracker(self)

    # ------------------------------------------------------------------ the gate

    def endpoint(self, handler: Handler, *, spends: bool = True) -> Callable[[Request], Awaitable[Response]]:
        async def answer(request: Request) -> Response:
            version = header(request, "x-github-api-version")
            resource = resource_of(request.url.path)
            caller: Caller | None = None
            budget: wire.StoredBudget | None = None
            # A conditional call acting as a user spends only if it is not answered 304, so it is counted
            # once its answer is known; every other call is counted before it is answered.
            conditional = header(request, "if-none-match") is not None
            owed = False
            try:
                if version in wire.UNSERVED_API_VERSIONS:
                    raise NotServed(f"API version {version}: only {', '.join(wire.API_VERSIONS)} is served")
                if version is not None and version not in wire.API_VERSIONS:
                    raise wire.unsupported_version(version)
                caller = self._authenticate(request)
                deferred = conditional and caller.account is not None
                budget = self._window(caller, resource)
                if spends and budget.limit > 0:
                    if budget.remaining == 0:
                        raise self._exhausted(budget, resource, caller)
                    if deferred:
                        owed = True
                    else:
                        budget = self._spend(caller, resource, budget)
                answered = self._fault(request, caller) or await handler(request, caller)
            except wire.Refusal as refusal:
                answered = Answered(refusal.status, wire.error_body(refusal), refusal.headers)
            except _Exhausted as exhausted:
                answered = exhausted.answered
            if request.method == "GET" and answered.status == 200:
                answered = _conditional(request, answered)
            if owed and budget is not None and answered.status != 304:
                budget = self._spend(caller, resource, budget)
            if budget is None:
                budget = self._window(caller, resource)
            headers = {
                "X-GitHub-Media-Type": "github.v3; format=json",
                **_budget_headers(budget, resource),
                **answered.headers,
            }
            if caller is not None and caller.token is not None and caller.token.kind is wire.TokenKind.CLASSIC:
                headers["X-OAuth-Scopes"] = ", ".join(caller.token.scopes)
            if version is not None and version in wire.API_VERSIONS:
                headers["X-GitHub-Api-Version-Selected"] = version
            if answered.status == 304:
                return Response(status_code=304, headers=headers)
            return Response(answered.body, status_code=answered.status, media_type=wire.JSON, headers=headers)

        return answer

    def _spend(self, caller: Caller | None, resource: wire.Resource, budget: wire.StoredBudget) -> wire.StoredBudget:
        spent = budget.model_copy(update={"used": budget.used + 1})
        self.world.write_budget(_login(caller), resource, spent)
        return spent

    def _window(self, caller: Caller | None, resource: wire.Resource) -> wire.StoredBudget:
        """The caller's primary budget for `resource` as it stands now: the user's, shared by every token that acts
        as them, or the address's when nobody is authenticated. A window that has ended on the run's clock is a
        whole budget again, ending a full window from now; the run's clock does not move inside a wake, so a
        budget spent there stays spent until the clock passes its reset."""
        login = _login(caller)
        limit = wire.limit_for(resource, authenticated=login is not None)
        now = self.clock.now().timestamp()
        stored = self.world.budget(login, resource)
        if stored is not None and now < stored.reset:
            return stored
        return wire.StoredBudget(limit=limit, used=0, reset=math.ceil(now) + wire.WINDOW_SECONDS[resource])

    def _exhausted(self, budget: wire.StoredBudget, resource: wire.Resource, caller: Caller) -> _Exhausted:
        """The call after the last one the budget allows: GitHub's primary-limit refusal, which spends nothing."""
        return _Exhausted(self._rate_limited(403, resource, caller, _budget_headers(budget, resource)))

    async def rate_limit(self, request: Request, caller: Caller) -> Answered:
        """`GET /rate_limit`: every budget of the caller as it stands, `rate` being the core one. Reading it
        spends nothing."""
        budgets = {resource: _budget_out(self._window(caller, resource), resource) for resource in wire.Resource}
        return as_json(wire.RateLimitOut(resources=budgets, rate=budgets[wire.Resource.CORE]))

    def _authenticate(self, request: Request) -> Caller:
        """Who the call acts as. Minutehand does not authenticate: a token the world holds acts as its user, and
        anything else, an empty token or no `Authorization` at all included, as the world's stand-in
        (`unknown_credentials_act_as`). Only a world with no user has nobody to stand in, and there the call reads
        as nobody's. Nothing here refuses."""
        authorization = (header(request, "authorization") or "").strip()
        token = self.world.token(_presented(authorization)) if authorization else None
        stand_in = self.world.stand_in() if token is None else None
        login = token.login if token is not None else stand_in.login if stand_in is not None else None
        if login is None:
            return Caller(account=None, token=None)
        account = self.world.account(login)
        if account is None:
            raise LookupError(f"a credential acts as {login}, who is not in this GitHub")
        return Caller(account=account, token=token)

    def _fault(self, request: Request, caller: Caller) -> Answered | None:
        resource = resource_of(request.url.path)
        for ref, armed in self.world.faults():
            fault = armed.fault
            if armed.answered >= fault.times or (fault.resource is not None and fault.resource is not resource):
                continue
            self.world.spend(ref, armed)
            answering.injected()
            return self._faulted(fault, resource, caller)
        return None

    def _faulted(self, fault: wire.Fault, resource: wire.Resource, caller: Caller) -> Answered:
        if isinstance(fault, wire.ServerError):
            return Answered(fault.status, json.dumps({"message": "Server Error"}).encode())
        if isinstance(fault, wire.SecondaryRateLimited):
            refusal = wire.Refusal(
                fault.status,
                "You have exceeded a secondary rate limit. Wait before you try again.",
                section="/using-the-rest-api/rate-limits-for-the-rest-api",
            )
            return Answered(fault.status, wire.error_body(refusal), {"Retry-After": str(fault.retry_after)})
        reset = math.ceil(self.clock.now().timestamp()) + wire.WINDOW_SECONDS[resource]
        limit = wire.LIMITS[resource]
        spent = wire.StoredBudget(limit=limit, used=limit, reset=reset)
        return self._rate_limited(fault.status, resource, caller, _budget_headers(spent, resource))

    def _rate_limited(self, status: int, resource: wire.Resource, caller: Caller, headers: dict[str, str]) -> Answered:
        """The primary limit's refusal: a 403 (or 429) on REST, a 200 whose `errors` say RATE_LIMITED on GraphQL."""
        who = f"user ID {caller.account.id}" if caller.account is not None else "this address"
        message = f"API rate limit exceeded for {who}."
        if resource is wire.Resource.GRAPHQL:
            errors = wire.GraphErrorsOut(errors=[wire.GraphError(type="RATE_LIMITED", message=message)])
            return Answered(200, errors.model_dump_json(exclude_none=True).encode(), headers)
        refusal = wire.Refusal(status, message, section="/using-the-rest-api/rate-limits-for-the-rest-api")
        return Answered(status, wire.error_body(refusal), headers)

    # ------------------------------------------------------------------ who may see what

    def permission(self, caller: Caller, repository: wire.StoredRepository) -> wire.Permission | None:
        """The caller's role on the repository, or None when it may not see it at all: world data only (owner,
        collaborators, organization members, public or private), never what the credential was issued for."""
        public = None if repository.private else wire.Permission.PULL
        if caller.account is None:
            return public
        return self.world.role(caller.account, repository) or public

    def visible(self, caller: Caller, owner: str, name: str, section: str) -> wire.StoredRepository:
        repository = self.world.repository(owner, name)
        if repository is None or self.permission(caller, repository) is None:
            raise wire.not_found(section)
        return repository

    # ------------------------------------------------------------------ presenting

    def account_out(self, login: str) -> wire.AccountOut:
        account = self.world.account(login)
        if account is None:
            raise LookupError(f"{login} is named by a record and is not in this GitHub")
        return wire.AccountOut.of(account)

    def repository_out(self, caller: Caller, repository: wire.StoredRepository) -> wire.RepositoryOut:
        files = self.world.files(repository)
        languages = content.breakdown(files)
        full = repository.full_name
        pushed = repository.commits[0].date if repository.commits else repository.created_at
        role = self.permission(caller, repository) if caller.account is not None else None
        permissions = None
        if role is not None:
            rank = wire.PERMISSION_ORDER.index(role)
            permissions = wire.PermissionsOut(
                admin=rank >= 4, maintain=rank >= 3, push=rank >= 2, triage=rank >= 1, pull=True
            )
        license = repository.license
        return wire.RepositoryOut(
            **wire.RepositoryLinksOut.of(full).model_dump(),
            id=repository.id,
            node_id=wire.node_id("R", repository.id),
            name=repository.name,
            full_name=full,
            private=repository.private,
            owner=self.account_out(repository.owner),
            description=repository.description,
            git_url=f"git://github.com/{full}.git",
            ssh_url=f"git@github.com:{full}.git",
            clone_url=f"{wire.WEB}/{full}.git",
            svn_url=f"{wire.WEB}/{full}",
            homepage=repository.homepage,
            size=math.ceil(sum(f.size for f in files) / 1024),
            stargazers_count=repository.stargazers_count,
            watchers_count=repository.stargazers_count,
            language=languages[0][0] if languages else None,
            forks_count=repository.forks_count,
            license=None
            if license is None
            else wire.LicenseOut(
                key=license.key,
                name=license.name,
                spdx_id=license.spdx_id,
                url=f"{wire.API}/licenses/{license.key}",
                node_id=wire.node_id("L", int(hashlib.sha1(license.key.encode()).hexdigest()[:8], 16)),
            ),
            topics=repository.topics,
            visibility="private" if repository.private else "public",
            forks=repository.forks_count,
            watchers=repository.stargazers_count,
            default_branch=repository.default_branch,
            network_count=repository.network_count,
            subscribers_count=repository.subscribers_count,
            permissions=permissions,
            created_at=repository.created_at,
            updated_at=pushed,
            pushed_at=pushed,
        )

    def _content_out(
        self, repository: wire.StoredRepository, entry: content.Entry, ref: str, *, inline: bool
    ) -> wire.EntryOut:
        full = repository.full_name
        file = entry.file
        if file is None:
            sha = content.tree_sha(repository, entry.path)
            git_url = f"{wire.API}/repos/{full}/git/trees/{sha}"
            html_url = f"{wire.WEB}/{full}/tree/{ref}/{entry.path}"
            url = f"{wire.API}/repos/{full}/contents/{entry.path}?ref={ref}"
            return wire.EntryOut.model_validate(
                {
                    "type": "dir",
                    "size": 0,
                    "name": entry.name,
                    "path": entry.path,
                    "sha": sha,
                    "url": url,
                    "git_url": git_url,
                    "html_url": html_url,
                    "download_url": None,
                    "_links": {"self": url, "git": git_url, "html": html_url},
                }
            )
        git_url = f"{wire.API}/repos/{full}/git/blobs/{file.sha}"
        html_url = f"{wire.WEB}/{full}/blob/{ref}/{file.path}"
        url = f"{wire.API}/repos/{full}/contents/{file.path}?ref={ref}"
        shape: dict[str, object] = {
            "type": "file",
            "size": file.size,
            "name": entry.name,
            "path": file.path,
            "sha": file.sha,
            "url": url,
            "git_url": git_url,
            "html_url": html_url,
            "download_url": f"{wire.RAW}/{full}/{ref}/{file.path}",
            "_links": {"self": url, "git": git_url, "html": html_url},
        }
        if not inline:
            return wire.EntryOut.model_validate(shape)
        fits = file.size <= content.CONTENTS_INLINE_LIMIT
        encoded = wire.wrapped_base64(_raw(file)) if fits else ""
        return wire.FileOut.model_validate({**shape, "encoding": "base64" if fits else "none", "content": encoded})

    def _commit_out(self, repository: wire.StoredRepository, commit: wire.StoredCommit) -> wire.CommitOut:
        full = repository.full_name
        person = wire.PersonOut(name=commit.author_name, email=commit.author_email, date=commit.date)
        committed = wire.PersonOut(name=commit.committer_name, email=commit.committer_email, date=commit.committer_date)
        author = self.account_out(commit.author_login) if commit.author_login is not None else None
        committer = self.account_out(commit.committer_login) if commit.committer_login is not None else None
        tree = content.tree_sha(repository, "")
        return wire.CommitOut(
            sha=commit.sha,
            node_id=wire.node_id("C", int(commit.sha[:8], 16)),
            commit=wire.CommitDetailOut(
                author=person,
                committer=committed,
                message=commit.message,
                tree=wire.ShaRefOut(sha=tree, url=f"{wire.API}/repos/{full}/git/trees/{tree}"),
                url=f"{wire.API}/repos/{full}/git/commits/{commit.sha}",
            ),
            url=f"{wire.API}/repos/{full}/commits/{commit.sha}",
            html_url=f"{wire.WEB}/{full}/commit/{commit.sha}",
            comments_url=f"{wire.API}/repos/{full}/commits/{commit.sha}/comments",
            author=author,
            committer=committer,
            parents=[]
            if commit.parent is None
            else [
                wire.ParentOut(
                    sha=commit.parent,
                    url=f"{wire.API}/repos/{full}/commits/{commit.parent}",
                    html_url=f"{wire.WEB}/{full}/commit/{commit.parent}",
                )
            ],
        )

    # ------------------------------------------------------------------ routes

    async def user(self, request: Request, caller: Caller) -> Answered:
        account = caller.account
        if account is None:
            raise wire.requires_authentication()
        self.world.saw(state.account_ref(account.login), Operation.READ)
        owned = [r for r in self.world.repositories() if r.owner.lower() == account.login.lower() and not r.private]
        out = wire.UserOut(
            **self.account_out(account.login).model_dump(),
            name=account.name,
            email=account.email,
            public_repos=len(owned),
            created_at=account.created_at,
            updated_at=account.created_at,
        )
        return as_json(out)

    async def user_repos(self, request: Request, caller: Caller) -> Answered:
        account = caller.account
        if account is None:
            raise wire.requires_authentication()
        mine = [r for r in self.world.repositories() if self._listed(account, r)]
        sort = param(request, "sort") or "full_name"
        if sort == "full_name":
            mine.sort(key=lambda r: r.full_name.lower())
        else:
            pushed = {r.full_name: (r.commits[0].date if r.commits else r.created_at) for r in mine}
            created = {r.full_name: r.created_at for r in mine}
            by_creation = sort == "created"  # enum-lint: exempt GitHub's `sort` query value, its wire vocabulary
            moment = created if by_creation else pushed
            mine.sort(key=lambda r: (moment[r.full_name], r.full_name.lower()), reverse=True)
        start, end, links = paged(request, len(mine))
        self.world.saw(state.account_ref(account.login), Operation.SEARCH)
        return as_json([self.repository_out(caller, r) for r in mine[start:end]], headers=links)

    def _listed(self, account: wire.StoredAccount, repository: wire.StoredRepository) -> bool:
        """`/user/repos` lists what the user owns, collaborates on or reaches through an organization."""
        return self.world.role(account, repository) is not None

    async def repository(self, request: Request, caller: Caller) -> Answered:
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self.visible(caller, owner, name, "/repos/repos#get-a-repository")
        self.world.saw(state.repository_ref(owner, name), Operation.READ)
        return as_json(self.repository_out(caller, repository))

    async def languages(self, request: Request, caller: Caller) -> Answered:
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self.visible(caller, owner, name, "/repos/repos#list-repository-languages")
        self.world.saw(state.repository_ref(owner, name), Operation.READ)
        counted = dict(content.breakdown(self.world.files(repository)))
        return Answered(200, json.dumps(counted).encode())

    async def branches(self, request: Request, caller: Caller) -> Answered:
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self.visible(caller, owner, name, "/branches/branches#list-branches")
        self.world.saw(state.repository_ref(owner, name), Operation.READ)
        if not repository.commits:
            return as_json([])
        head = repository.commits[0].sha
        tips = {name: head for name in (repository.default_branch, *repository.branches)}
        tips |= {line.name: line.commits[0].sha if line.commits else line.fork for line in repository.lines}
        names = sorted(tips)
        start, end, links = paged(request, len(names), path=_by_id(request, repository))
        out: list[wire.Wire] = [
            wire.BranchOut(
                name=branch,
                commit=wire.ShaRefOut(
                    sha=tips[branch], url=f"{wire.API}/repos/{repository.full_name}/commits/{tips[branch]}"
                ),
                protected=False,
            )
            for branch in names[start:end]
        ]
        return as_json(out, headers=links)

    async def contents(self, request: Request, caller: Caller) -> Answered:
        section = "/repos/contents#get-repository-content"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        path = (request.path_params["path"] if "path" in request.path_params else "").strip("/")
        repository = self.visible(caller, owner, name, section)
        if not repository.commits:
            raise wire.Refusal(404, "This repository is empty.", section=section)
        ref = param(request, "ref")
        if content.resolve(repository, ref) is None:
            # Observed 2026-10-08: this refusal points at the reference's old address.
            raise wire.Refusal(
                404,
                f"No commit found for the ref {ref}",
                documentation_url="https://docs.github.com/v3/repos/contents/",
            )
        shown = ref or repository.default_branch
        files = self.world.files(repository)
        self.world.saw(state.repository_ref(owner, name), Operation.READ)
        found = next((f for f in files if f.path == path), None) if path else None
        if found is not None:
            entry = content.Entry(name=found.path.rsplit("/", 1)[-1], path=found.path, file=found)
            return as_json(self._content_out(repository, entry, shown, inline=True))
        if path and path not in content.directories(files):
            raise wire.not_found(section)
        entries = content.children(files, path)[: repository.directory_entry_limit]
        return as_json([self._content_out(repository, e, shown, inline=False) for e in entries])

    async def blob(self, request: Request, caller: Caller) -> Answered:
        section = "/git/blobs#get-a-blob"
        owner, name, sha = request.path_params["owner"], request.path_params["repo"], request.path_params["sha"]
        repository = self.visible(caller, owner, name, section)
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise wire.Refusal(
                422, "The sha parameter must be exactly 40 characters and contain only [0-9a-f].", section=section
            )
        found = next((f for f in self.world.files(repository) if f.sha == sha), None)
        if found is None:
            raise wire.not_found(section)
        self.world.saw(state.repository_ref(owner, name), Operation.READ)
        out = wire.BlobOut(
            sha=found.sha,
            node_id=wire.node_id("B", int(found.sha[:8], 16)),
            size=found.size,
            url=f"{wire.API}/repos/{repository.full_name}/git/blobs/{found.sha}",
            content=wire.wrapped_base64(_raw(found)),
        )
        return as_json(out)

    async def tree(self, request: Request, caller: Caller) -> Answered:
        section = "/git/trees#get-a-tree"
        owner, name, wanted = request.path_params["owner"], request.path_params["repo"], request.path_params["tree"]
        repository = self.visible(caller, owner, name, section)
        files = self.world.files(repository)
        directory: str | None = "" if content.resolve(repository, wanted) is not None else None
        if directory is None:
            directory = next(
                (d for d in ["", *content.directories(files)] if content.tree_sha(repository, d) == wanted.lower()),
                None,
            )
        if directory is None or not repository.commits:
            raise wire.not_found(section)
        recursive = param(request, "recursive") is not None
        prefix = f"{directory}/" if directory else ""
        entries = content.under(files, directory) if recursive else content.children(files, directory)
        out: list[wire.TreeEntryOut] = []
        for entry in entries:
            relative = entry.path[len(prefix) :]
            if entry.file is None:
                sha = content.tree_sha(repository, entry.path)
                out.append(
                    wire.TreeEntryOut(
                        path=relative,
                        mode="040000",
                        type="tree",
                        sha=sha,
                        url=f"{wire.API}/repos/{repository.full_name}/git/trees/{sha}",
                    )
                )
            else:
                out.append(
                    wire.TreeEntryOut(
                        path=relative,
                        mode="100644",
                        type="blob",
                        sha=entry.file.sha,
                        size=entry.file.size,
                        url=f"{wire.API}/repos/{repository.full_name}/git/blobs/{entry.file.sha}",
                    )
                )
        truncated = len(out) > repository.tree_entry_limit
        sha = content.tree_sha(repository, directory)
        self.world.saw(state.repository_ref(owner, name), Operation.READ)
        return as_json(
            wire.TreeOut(
                sha=sha,
                url=f"{wire.API}/repos/{repository.full_name}/git/trees/{sha}",
                tree=out[: repository.tree_entry_limit],
                truncated=truncated,
            )
        )

    async def commits(self, request: Request, caller: Caller) -> Answered:
        section = "/commits/commits#list-commits"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self.visible(caller, owner, name, section)
        if not repository.commits:
            raise wire.Refusal(409, "Git Repository is empty.", section=section)
        sha = param(request, "sha")
        start = content.resolve(repository, sha)
        if start is None:
            raise wire.not_found(section)
        history = repository.commits[repository.commits.index(start) :]
        path = (param(request, "path") or "").strip("/")
        if path:
            history = [c for c in history if any(p == path or p.startswith(path + "/") for p in c.paths)]
        first, end, links = paged(request, len(history), path=_by_id(request, repository))
        self.world.saw(state.repository_ref(owner, name), Operation.READ)
        return as_json([self._commit_out(repository, c) for c in history[first:end]], headers=links)

    async def search_code(self, request: Request, caller: Caller) -> Answered:
        if caller.account is None:
            raise wire.requires_authentication()
        query = search.parse(param(request, "q") or "")
        visible = [r for r in self.world.repositories() if self.permission(caller, r) is not None]
        names = {r.full_name.lower(): r for r in visible}
        logins = {a.login.lower() for a in self.world.accounts()}
        hidden = [n for n in query.repos if n not in names] + [o for o in query.owners if o not in logins]
        if hidden:
            raise wire.validation_failed(
                search.SECTION,
                wire.FieldError(
                    resource="Search",
                    field="q",
                    code="invalid",
                    message="The listed users and repositories cannot be searched either because the resources "
                    "do not exist or you do not have permission to view them.",
                ),
            )
        scope = [names[n] for n in dict.fromkeys(query.repos)] if query.repos else visible
        if query.owners:
            scope = [r for r in scope if r.owner.lower() in query.owners]
        per_page = min(max(number(param(request, "per_page"), PAGE_DEFAULT), 1), PAGE_MAX)
        page = max(number(param(request, "page"), 1), 1)
        if (page - 1) * per_page >= SEARCH_CEILING:
            raise wire.validation_failed(
                search.SECTION,
                wire.FieldError(
                    resource="Search",
                    field="q",
                    code="invalid",
                    message=f"Only the first {SEARCH_CEILING} search results are available",
                ),
            )
        hits: list[tuple[wire.StoredRepository, wire.StoredFile]] = []
        for repository in sorted(scope, key=lambda r: r.full_name.lower()):
            self.world.saw(state.repository_ref(repository.owner, repository.name), Operation.SEARCH)
            for file in self.world.files(repository):
                raw = _raw(file)
                if file.size > content.SEARCH_INDEX_LIMIT or content.is_binary(raw):
                    continue
                if query.matches(file, raw.decode("utf-8")):
                    hits.append((repository, file))
        start, end, links = paged(request, len(hits), ceiling=SEARCH_CEILING)
        window = hits[start:end]
        if TEXT_MATCH not in (header(request, "accept") or ""):
            items = [self._code_item(r, f) for r, f in window]
            return as_json(
                wire.CodeSearchOut(total_count=len(hits), incomplete_results=False, items=items), headers=links
            )
        matched = [
            wire.MatchedCodeItemOut(
                **self._code_item(r, f).model_dump(),
                text_matches=[self._text_match(r, f, query)],
            )
            for r, f in window
        ]
        out = wire.MatchedCodeSearchOut(total_count=len(hits), incomplete_results=False, items=matched)
        return as_json(out, headers=links)

    def _text_match(
        self, repository: wire.StoredRepository, file: wire.StoredFile, query: search.CodeQuery
    ) -> wire.TextMatchOut:
        fragment, terms = search.text_match(query, _raw(file).decode("utf-8"))
        return wire.TextMatchOut(
            object_url=f"{wire.API}/repositories/{repository.id}/contents/{file.path}?ref={repository.commits[0].sha}",
            object_type="FileContent",
            property="content",
            fragment=fragment,
            matches=[wire.TermMatchOut(text=t.text, indices=[t.start, t.end]) for t in terms],
        )

    def _code_item(self, repository: wire.StoredRepository, file: wire.StoredFile) -> wire.CodeItemOut:
        full = repository.full_name
        return wire.CodeItemOut(
            name=file.path.rsplit("/", 1)[-1],
            path=file.path,
            sha=file.sha,
            url=f"{wire.API}/repositories/{repository.id}/contents/{file.path}?ref={repository.commits[0].sha}",
            git_url=f"{wire.API}/repositories/{repository.id}/git/blobs/{file.sha}",
            html_url=f"{wire.WEB}/{full}/blob/{repository.commits[0].sha}/{file.path}",
            repository=wire.MinimalRepositoryOut(
                **wire.RepositoryLinksOut.of(full).model_dump(),
                id=repository.id,
                node_id=wire.node_id("R", repository.id),
                name=repository.name,
                full_name=full,
                owner=self.account_out(repository.owner),
                private=repository.private,
                description=repository.description,
            ),
        )

    def by_id(self, handler: Handler) -> Handler:
        """A repository route reached as `/repositories/{repository_id}/…`, the address GitHub's own `Link` headers
        give (recorded 2026-10-08, `tests/providers/github/observed/`): the same read, of the repository with that
        id."""

        async def answer(request: Request, caller: Caller) -> Answered:
            wanted = request.path_params["repository_id"]
            found = next((r for r in self.world.repositories() if r.id == wanted), None)
            if found is None:
                raise wire.not_found()
            request.scope["path_params"] = {**request.path_params, "owner": found.owner, "repo": found.name}
            return await handler(request, caller)

        return answer

    async def installation_token(self, request: Request, caller: Caller) -> Answered:
        """`POST /app/installations/{installation_id}/access_tokens`: always issued, whatever authenticates it and
        whichever installation it names (Minutehand does not enforce credentials). The token is GitHub's to make;
        `permissions` comes back as the request sent it. The token, used, acts as any credential the world does
        not hold. https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app"""
        section = "/apps/apps#create-an-installation-access-token-for-an-app"
        installation = int(request.path_params["installation_id"])
        sent = await request.body()
        try:
            asked = wire.InstallationTokenIn.model_validate_json(sent) if sent.strip() else wire.InstallationTokenIn()
        except ValidationError as error:
            raise wire.Refusal(400, "Problems parsing JSON", section=section) from error
        expires_at = wire.timestamp(self.clock.now() + INSTALLATION_TOKEN_LIFETIME)
        issued = wire.StoredInstallationToken(
            installation_id=installation, expires_at=expires_at, permissions=asked.permissions
        )
        number = self.world.issue_installation_token(issued)
        token = INSTALLATION_TOKEN_PREFIX + hashlib.sha256(f"{installation}\0{number}".encode()).hexdigest()[:36]
        answer = wire.InstallationTokenOut(token=token, expires_at=expires_at, permissions=asked.permissions)
        return Answered(201, answer.model_dump_json(exclude_none=True).encode())

    async def graph(self, request: Request, caller: Caller) -> Answered:
        viewer = caller.account
        if viewer is None:
            raise wire.Refusal(401, "This endpoint requires you to be authenticated.", section="/graphql")
        try:
            body = wire.GraphQLIn.model_validate_json(await request.body())
        except ValidationError as error:
            if any(problem["loc"] == ("query",) for problem in error.errors()):
                raise wire.Refusal(
                    400, "A query attribute must be specified and must be a string.", section="/graphql"
                ) from error
            raise wire.Refusal(400, "Problems parsing JSON", section="/graphql") from error

        def find(owner: str, name: str) -> graphql.Visible | None:
            repository = self.world.repository(owner, name)
            if repository is None or self.permission(caller, repository) is None:
                return None
            return graphql.Visible(repository=repository, files=lambda: self.world.files(repository))

        answer = graphql.execute(body, find, viewer)
        for repository in answer.seen:
            self.world.saw(state.repository_ref(repository.owner, repository.name), Operation.READ)
        return Answered(200, answer.body)


def _by_id(request: Request, repository: wire.StoredRepository) -> str:
    """The call's path as GitHub's `Link` headers write it: under `/repositories/{id}`, not `/repos/{owner}/{repo}`."""
    path = request.url.path
    if path.startswith("/repositories/"):
        return path
    parts = path.split("/", 4)
    return f"/repositories/{repository.id}" + ("/" + parts[4] if len(parts) > 4 else "")


def _presented(authorization: str) -> str:
    """The credential an `Authorization` header carries: what follows `Bearer` or `token`, the password of
    `Basic` (where GitHub takes a token), or the whole value under any other scheme."""
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "basic":
        return rest.strip() if rest else authorization
    try:
        _, _, password = base64.b64decode(rest.strip(), validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return rest.strip()
    return password


def _etag(body: bytes) -> str:
    """An opaque validator of the answer's bytes; GitHub's reference gives it no format of its own."""
    return 'W/"' + hashlib.sha256(body).hexdigest() + '"'


def _conditional(request: Request, answered: Answered) -> Answered:
    """A 200 GET with its `ETag`, or a 304 with nothing in it when `If-None-Match` names that tag (compared
    weakly, as If-None-Match is). https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests-if-appropriate"""
    tag = _etag(answered.body)
    headers = {**answered.headers, "ETag": tag}
    wanted = header(request, "if-none-match")
    if wanted is not None:
        named = {t.strip().removeprefix("W/") for t in wanted.split(",")}
        if "*" in named or tag.removeprefix("W/") in named:
            return Answered(304, b"", headers)
    return Answered(answered.status, answered.body, headers)


def build_app(store: Store, clock: Clock) -> Starlette:
    api = GitHubApi(store, clock)
    tracker = api.tracker
    table: list[tuple[str, str, Handler]] = [
        ("/user", "GET", api.user),
        ("/user/repos", "GET", api.user_repos),
        ("/repos/{owner}/{repo}", "GET", api.repository),
        ("/repos/{owner}/{repo}/languages", "GET", api.languages),
        ("/repos/{owner}/{repo}/branches", "GET", api.branches),
        ("/repos/{owner}/{repo}/contents", "GET", api.contents),
        ("/repos/{owner}/{repo}/contents/", "GET", api.contents),
        ("/repos/{owner}/{repo}/contents/{path:path}", "GET", api.contents),
        ("/repos/{owner}/{repo}/git/blobs/{sha}", "GET", api.blob),
        ("/repos/{owner}/{repo}/git/trees/{tree:path}", "GET", api.tree),
        ("/repos/{owner}/{repo}/commits", "GET", api.commits),
        ("/repos/{owner}/{repo}/issues", "GET", tracker.list_issues),
        ("/repos/{owner}/{repo}/issues", "POST", tracker.create_issue),
        ("/repos/{owner}/{repo}/issues/comments", "GET", tracker.list_repository_comments),
        ("/repos/{owner}/{repo}/issues/comments/{comment_id:int}", "GET", tracker.get_comment),
        ("/repos/{owner}/{repo}/issues/comments/{comment_id:int}", "PATCH", tracker.update_comment),
        ("/repos/{owner}/{repo}/issues/comments/{comment_id:int}", "DELETE", tracker.delete_comment),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}", "GET", tracker.get_issue),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}", "PATCH", tracker.update_issue),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}/comments", "GET", tracker.list_comments),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}/comments", "POST", tracker.create_comment),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}/lock", "PUT", tracker.lock_issue),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}/lock", "DELETE", tracker.unlock_issue),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}/labels", "GET", tracker.list_issue_labels),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}/labels", "POST", tracker.add_labels),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}/labels", "PUT", tracker.set_labels),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}/labels", "DELETE", tracker.clear_labels),
        ("/repos/{owner}/{repo}/issues/{issue_number:int}/labels/{name:path}", "DELETE", tracker.remove_label),
        ("/repos/{owner}/{repo}/labels", "GET", tracker.list_labels),
        ("/repos/{owner}/{repo}/labels", "POST", tracker.create_label),
        ("/repos/{owner}/{repo}/labels/{name:path}", "GET", tracker.get_label),
        ("/search/code", "GET", api.search_code),
        ("/app/installations/{installation_id:int}/access_tokens", "POST", api.installation_token),
        ("/graphql", "POST", api.graph),
    ]
    aliases = [
        ("/repositories/{repository_id:int}" + path.removeprefix(REPOSITORY), method, api.by_id(handler))
        for path, method, handler in table
        if path.startswith(REPOSITORY)
    ]
    routes = [Route(path, api.endpoint(handler), methods=[method]) for path, method, handler in [*table, *aliases]]
    routes.append(Route("/rate_limit", api.endpoint(api.rate_limit, spends=False), methods=["GET"]))

    async def unserved(request: Request) -> Response:
        """Every other method and path of api.github.com: refused by name (501, "minutehand's github fake does
        not implement <METHOD> <path>"), never a 404 a client would read as GitHub's answer."""
        raise NotServed("it is not among the calls this provider serves (its README's table)")

    routes.append(Route("/{anything:path}", unserved, methods=list(EVERY_METHOD)))
    return Starlette(routes=routes)


REPOSITORY = "/repos/{owner}/{repo}"
EVERY_METHOD = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
