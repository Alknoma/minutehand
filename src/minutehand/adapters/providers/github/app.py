"""The GitHub REST and GraphQL reads a code-reading client makes, as an ASGI app over the run's store and clock.

Every call is checked in GitHub's order: an `X-GitHub-Api-Version` it does not serve (400), then the credential
(`Authorization: Bearer` or `token`; an unknown one is 401 `Bad credentials`), then the faults the scenario armed,
then the route. A repository the credential may not see is a 404, exactly as one that does not exist. Lists are
paged by `per_page` and `page` and say where the next page is in a `Link` header.
"""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.github import content, graphql, search, state, wire
from minutehand.adapters.providers.github.state import GitHubWorld
from minutehand.domain.world import Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

PAGE_DEFAULT = 30
PAGE_MAX = 100
SEARCH_CEILING = 1000
"""Search serves the first thousand results and no more."""


@dataclass(frozen=True)
class Caller:
    """Who a call acts as: nobody, or a user through one of their tokens."""

    account: wire.StoredAccount | None
    token: wire.StoredToken | None


@dataclass
class Answered:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


Handler = Callable[[Request, Caller], Awaitable[Answered]]


def _param(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def _header(request: Request, name: str) -> str | None:
    return request.headers[name] if name in request.headers else None


def _json(entity: wire.Wire | list[wire.Wire], status: int = 200, headers: dict[str, str] | None = None) -> Answered:
    if isinstance(entity, list):
        body = ("[" + ",".join(e.model_dump_json(by_alias=True) for e in entity) + "]").encode()
    else:
        body = entity.model_dump_json(by_alias=True).encode()
    return Answered(status, body, headers or {})


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
        self._world = GitHubWorld(store)
        self._clock = clock

    # ------------------------------------------------------------------ the gate

    def endpoint(self, handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def answer(request: Request) -> Response:
            version = _header(request, "x-github-api-version")
            caller: Caller | None = None
            try:
                if version is not None and version not in wire.API_VERSIONS:
                    raise wire.Refusal(400, f"API version {version} is not supported.", section="/about-the-rest-api")
                caller = self._authenticate(request)
                answered = self._fault(request, caller) or await handler(request, caller)
            except wire.Refusal as refusal:
                answered = Answered(refusal.status, wire.error_body(refusal), refusal.headers)
            headers = {"X-GitHub-Media-Type": "github.v3; format=json", **answered.headers}
            if caller is not None and caller.token is not None and caller.token.kind is wire.TokenKind.CLASSIC:
                headers["X-OAuth-Scopes"] = ", ".join(caller.token.scopes)
            if version is not None and version in wire.API_VERSIONS:
                headers["X-GitHub-Api-Version-Selected"] = version
            return Response(answered.body, status_code=answered.status, media_type=wire.JSON, headers=headers)

        return answer

    def _authenticate(self, request: Request) -> Caller:
        authorization = (_header(request, "authorization") or "").strip()
        if not authorization:
            return Caller(account=None, token=None)
        scheme, _, presented = authorization.partition(" ")
        if scheme.lower() not in ("bearer", "token") or not presented.strip():
            raise wire.bad_credentials()
        token = self._world.token(presented.strip())
        if token is None:
            raise wire.bad_credentials()
        account = self._world.account(token.login)
        if account is None:
            raise LookupError(f"a token acts as {token.login}, who is not in this GitHub")
        return Caller(account=account, token=token)

    def _fault(self, request: Request, caller: Caller) -> Answered | None:
        resource = resource_of(request.url.path)
        for ref, armed in self._world.faults():
            fault = armed.fault
            if armed.answered >= fault.times or (fault.resource is not None and fault.resource is not resource):
                continue
            self._world.spend(ref, armed)
            return self._faulted(fault, resource, caller)
        return None

    def _faulted(self, fault: wire.Fault, resource: wire.Resource, caller: Caller) -> Answered:
        who = f"user ID {caller.account.id}" if caller.account is not None else "this address"
        if isinstance(fault, wire.ServerError):
            return Answered(fault.status, json.dumps({"message": "Server Error"}).encode())
        if isinstance(fault, wire.SecondaryRateLimited):
            refusal = wire.Refusal(
                fault.status,
                "You have exceeded a secondary rate limit. Wait before you try again.",
                section="/using-the-rest-api/rate-limits-for-the-rest-api",
            )
            return Answered(fault.status, wire.error_body(refusal), {"Retry-After": str(fault.retry_after)})
        reset = math.ceil(self._clock.now().timestamp()) + wire.WINDOW_SECONDS[resource]
        limit = wire.LIMITS[resource]
        headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Used": str(limit),
            "X-RateLimit-Reset": str(reset),
            "X-RateLimit-Resource": resource.value,
        }
        message = f"API rate limit exceeded for {who}."
        if resource is wire.Resource.GRAPHQL:
            errors = wire.GraphErrorsOut(errors=[wire.GraphError(type="RATE_LIMITED", message=message)])
            return Answered(200, errors.model_dump_json(exclude_none=True).encode(), headers)
        refusal = wire.Refusal(fault.status, message, section="/using-the-rest-api/rate-limits-for-the-rest-api")
        return Answered(fault.status, wire.error_body(refusal), headers)

    # ------------------------------------------------------------------ who may see what

    def permission(self, caller: Caller, repository: wire.StoredRepository) -> wire.Permission | None:
        """The caller's role on the repository, or None when it may not see it at all."""
        public = None if repository.private else wire.Permission.PULL
        account, token = caller.account, caller.token
        if account is None or token is None:
            return public
        if token.kind is wire.TokenKind.FINE_GRAINED and token.repositories is not None:
            selected = {r.lower() for r in token.repositories}
            if repository.full_name.lower() not in selected:
                return public
        role = self._role(account, repository)
        if repository.private and token.kind is wire.TokenKind.CLASSIC and "repo" not in token.scopes:
            return None
        return role or public

    def _role(self, account: wire.StoredAccount, repository: wire.StoredRepository) -> wire.Permission | None:
        """The role the account holds by owning, collaborating or belonging, apart from the repository being public."""
        login = account.login.lower()
        if repository.owner.lower() == login:
            return wire.Permission.ADMIN
        roles = [c.permission for c in repository.collaborators if c.login.lower() == login]
        owner = self._world.account(repository.owner)
        if owner is not None and login in {m.lower() for m in owner.members}:
            roles.append(wire.Permission.PULL)
        return max(roles, key=wire.PERMISSION_ORDER.index) if roles else None

    def _visible(self, caller: Caller, owner: str, name: str, section: str) -> wire.StoredRepository:
        repository = self._world.repository(owner, name)
        if repository is None or self.permission(caller, repository) is None:
            raise wire.not_found(section)
        return repository

    # ------------------------------------------------------------------ presenting

    def _account_out(self, login: str) -> wire.AccountOut:
        account = self._world.account(login)
        if account is None:
            raise LookupError(f"{login} is named by a record and is not in this GitHub")
        return wire.AccountOut(
            login=account.login,
            id=account.id,
            node_id=wire.node_id("U" if account.type is wire.AccountType.USER else "O", account.id),
            avatar_url=f"https://avatars.githubusercontent.com/u/{account.id}?v=4",
            url=f"{wire.API}/users/{account.login}",
            html_url=f"{wire.WEB}/{account.login}",
            repos_url=f"{wire.API}/users/{account.login}/repos",
            type=account.type,
        )

    def _repository_out(self, caller: Caller, repository: wire.StoredRepository) -> wire.RepositoryOut:
        files = self._world.files(repository)
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
            id=repository.id,
            node_id=wire.node_id("R", repository.id),
            name=repository.name,
            full_name=full,
            private=repository.private,
            owner=self._account_out(repository.owner),
            html_url=f"{wire.WEB}/{full}",
            description=repository.description,
            url=f"{wire.API}/repos/{full}",
            contents_url=f"{wire.API}/repos/{full}/contents/{{+path}}",
            commits_url=f"{wire.API}/repos/{full}/commits{{/sha}}",
            trees_url=f"{wire.API}/repos/{full}/git/trees{{/sha}}",
            languages_url=f"{wire.API}/repos/{full}/languages",
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
                node_id=wire.node_id("L", len(license.key)),
            ),
            topics=repository.topics,
            visibility="private" if repository.private else "public",
            forks=repository.forks_count,
            watchers=repository.stargazers_count,
            default_branch=repository.default_branch,
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
        author = self._account_out(commit.author_login) if commit.author_login is not None else None
        tree = content.tree_sha(repository, "")
        return wire.CommitOut(
            sha=commit.sha,
            node_id=wire.node_id("C", int(commit.sha[:8], 16)),
            commit=wire.CommitDetailOut(
                author=person,
                committer=person,
                message=commit.message,
                tree=wire.ShaRefOut(sha=tree, url=f"{wire.API}/repos/{full}/git/trees/{tree}"),
                url=f"{wire.API}/repos/{full}/git/commits/{commit.sha}",
            ),
            url=f"{wire.API}/repos/{full}/commits/{commit.sha}",
            html_url=f"{wire.WEB}/{full}/commit/{commit.sha}",
            comments_url=f"{wire.API}/repos/{full}/commits/{commit.sha}/comments",
            author=author,
            committer=author,
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

    # ------------------------------------------------------------------ paging

    def _page(self, request: Request, total: int, *, ceiling: int | None = None) -> tuple[int, int, dict[str, str]]:
        """The window `per_page` and `page` ask for, and the `Link` header pointing at the others."""
        per_page = _number(_param(request, "per_page"), PAGE_DEFAULT)
        per_page = min(max(per_page, 1), PAGE_MAX)
        page = max(_number(_param(request, "page"), 1), 1)
        reachable = min(total, ceiling) if ceiling is not None else total
        last = max(math.ceil(reachable / per_page), 1)
        links: list[str] = []

        def at(number: int, rel: str) -> str:
            query = {k: v for k, v in request.query_params.items() if k != "page"}
            query["page"] = str(number)
            return f'<{wire.API}{request.url.path}?{urlencode(query)}>; rel="{rel}"'

        if page > 1:
            links.append(at(min(page - 1, last), "prev"))
        if page < last:
            links.append(at(page + 1, "next"))
            links.append(at(last, "last"))
        if page > 1:
            links.append(at(1, "first"))
        start = (page - 1) * per_page
        return start, start + per_page, {"Link": ", ".join(links)} if links else {}

    # ------------------------------------------------------------------ routes

    async def user(self, request: Request, caller: Caller) -> Answered:
        account = caller.account
        if account is None:
            raise wire.requires_authentication()
        self._world.saw(state.account_ref(account.login), Operation.READ)
        owned = [r for r in self._world.repositories() if r.owner.lower() == account.login.lower() and not r.private]
        out = wire.UserOut(
            **self._account_out(account.login).model_dump(),
            name=account.name,
            email=account.email,
            public_repos=len(owned),
            created_at=account.created_at,
            updated_at=account.created_at,
        )
        return _json(out)

    async def user_repos(self, request: Request, caller: Caller) -> Answered:
        account = caller.account
        if account is None:
            raise wire.requires_authentication()
        mine = [r for r in self._world.repositories() if self._listed(caller, account, r)]
        sort = _param(request, "sort") or "full_name"
        if sort == "full_name":
            mine.sort(key=lambda r: r.full_name.lower())
        else:
            pushed = {r.full_name: (r.commits[0].date if r.commits else r.created_at) for r in mine}
            created = {r.full_name: r.created_at for r in mine}
            by_creation = sort == "created"  # enum-lint: exempt GitHub's `sort` query value, its wire vocabulary
            moment = created if by_creation else pushed
            mine.sort(key=lambda r: (moment[r.full_name], r.full_name.lower()), reverse=True)
        start, end, links = self._page(request, len(mine))
        self._world.saw(state.account_ref(account.login), Operation.SEARCH)
        return _json([self._repository_out(caller, r) for r in mine[start:end]], headers=links)

    def _listed(self, caller: Caller, account: wire.StoredAccount, repository: wire.StoredRepository) -> bool:
        """`/user/repos` lists what the user owns, collaborates on or reaches through an organization; a
        fine-grained token lists only what it selected."""
        if self._role(account, repository) is None or self.permission(caller, repository) is None:
            return False
        token = caller.token
        if token is not None and token.kind is wire.TokenKind.FINE_GRAINED and token.repositories is not None:
            return repository.full_name.lower() in {r.lower() for r in token.repositories}
        return True

    async def repository(self, request: Request, caller: Caller) -> Answered:
        owner, name = request.path_params["owner"], request.path_params["repo"]
        repository = self._visible(caller, owner, name, "/repos/repos#get-a-repository")
        self._world.saw(state.repository_ref(owner, name), Operation.READ)
        return _json(self._repository_out(caller, repository))

    async def contents(self, request: Request, caller: Caller) -> Answered:
        section = "/repos/contents#get-repository-content"
        owner, name = request.path_params["owner"], request.path_params["repo"]
        path = (request.path_params["path"] if "path" in request.path_params else "").strip("/")
        repository = self._visible(caller, owner, name, section)
        if not repository.commits:
            raise wire.Refusal(404, "This repository is empty.", section=section)
        ref = _param(request, "ref")
        if content.resolve(repository, ref) is None:
            raise wire.Refusal(404, f"No commit found for the ref {ref}", section=section)
        shown = ref or repository.default_branch
        files = self._world.files(repository)
        self._world.saw(state.repository_ref(owner, name), Operation.READ)
        found = next((f for f in files if f.path == path), None) if path else None
        if found is not None:
            entry = content.Entry(name=found.path.rsplit("/", 1)[-1], path=found.path, file=found)
            return _json(self._content_out(repository, entry, shown, inline=True))
        if path and path not in content.directories(files):
            raise wire.not_found(section)
        entries = content.children(files, path)[: content.CONTENTS_DIRECTORY_LIMIT]
        return _json([self._content_out(repository, e, shown, inline=False) for e in entries])

    async def blob(self, request: Request, caller: Caller) -> Answered:
        section = "/git/blobs#get-a-blob"
        owner, name, sha = request.path_params["owner"], request.path_params["repo"], request.path_params["sha"]
        repository = self._visible(caller, owner, name, section)
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise wire.validation_failed(
                section,
                wire.FieldError(
                    resource="Blob",
                    field="sha",
                    code="invalid",
                    message="The sha must be 40 hexadecimal characters.",
                ),
            )
        found = next((f for f in self._world.files(repository) if f.sha == sha), None)
        if found is None:
            raise wire.not_found(section)
        self._world.saw(state.repository_ref(owner, name), Operation.READ)
        out = wire.BlobOut(
            sha=found.sha,
            node_id=wire.node_id("B", int(found.sha[:8], 16)),
            size=found.size,
            url=f"{wire.API}/repos/{repository.full_name}/git/blobs/{found.sha}",
            content=wire.wrapped_base64(_raw(found)),
        )
        return _json(out)

    async def tree(self, request: Request, caller: Caller) -> Answered:
        section = "/git/trees#get-a-tree"
        owner, name, wanted = request.path_params["owner"], request.path_params["repo"], request.path_params["tree"]
        repository = self._visible(caller, owner, name, section)
        files = self._world.files(repository)
        directory: str | None = "" if content.resolve(repository, wanted) is not None else None
        if directory is None:
            directory = next(
                (d for d in ["", *content.directories(files)] if content.tree_sha(repository, d) == wanted.lower()),
                None,
            )
        if directory is None or not repository.commits:
            raise wire.not_found(section)
        recursive = _param(request, "recursive") is not None
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
        self._world.saw(state.repository_ref(owner, name), Operation.READ)
        return _json(
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
        repository = self._visible(caller, owner, name, section)
        if not repository.commits:
            raise wire.Refusal(409, "Git Repository is empty.", section=section)
        sha = _param(request, "sha")
        start = content.resolve(repository, sha)
        if start is None:
            raise wire.Refusal(404, f"No commit found for SHA: {sha}", section=section)
        history = repository.commits[repository.commits.index(start) :]
        path = (_param(request, "path") or "").strip("/")
        if path:
            history = [c for c in history if any(p == path or p.startswith(path + "/") for p in c.paths)]
        first, end, links = self._page(request, len(history))
        self._world.saw(state.repository_ref(owner, name), Operation.READ)
        return _json([self._commit_out(repository, c) for c in history[first:end]], headers=links)

    async def search_code(self, request: Request, caller: Caller) -> Answered:
        if caller.account is None:
            raise wire.requires_authentication()
        query = search.parse(_param(request, "q") or "")
        visible = [r for r in self._world.repositories() if self.permission(caller, r) is not None]
        names = {r.full_name.lower(): r for r in visible}
        logins = {a.login.lower() for a in self._world.accounts()}
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
        per_page = min(max(_number(_param(request, "per_page"), PAGE_DEFAULT), 1), PAGE_MAX)
        page = max(_number(_param(request, "page"), 1), 1)
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
            self._world.saw(state.repository_ref(repository.owner, repository.name), Operation.SEARCH)
            for file in self._world.files(repository):
                raw = _raw(file)
                if file.size > content.SEARCH_INDEX_LIMIT or content.is_binary(raw):
                    continue
                if query.matches(file, raw.decode("utf-8")):
                    hits.append((repository, file))
        start, end, links = self._page(request, len(hits), ceiling=SEARCH_CEILING)
        items = [self._code_item(r, f) for r, f in hits[start:end]]
        return _json(wire.CodeSearchOut(total_count=len(hits), incomplete_results=False, items=items), headers=links)

    def _code_item(self, repository: wire.StoredRepository, file: wire.StoredFile) -> wire.CodeItemOut:
        full = repository.full_name
        return wire.CodeItemOut(
            name=file.path.rsplit("/", 1)[-1],
            path=file.path,
            sha=file.sha,
            url=f"{wire.API}/repositories/{repository.id}/contents/{file.path}?ref={repository.commits[0].sha}",
            git_url=f"{wire.API}/repositories/{repository.id}/git/blobs/{file.sha}",
            html_url=f"{wire.WEB}/{full}/blob/{repository.commits[0].sha}/{file.path}",
            repository=wire.SearchRepositoryOut(
                id=repository.id,
                node_id=wire.node_id("R", repository.id),
                name=repository.name,
                full_name=full,
                owner=self._account_out(repository.owner),
                private=repository.private,
                html_url=f"{wire.WEB}/{full}",
                description=repository.description,
                url=f"{wire.API}/repos/{full}",
            ),
            score=1.0,
        )

    async def graph(self, request: Request, caller: Caller) -> Answered:
        if caller.account is None:
            raise wire.Refusal(401, "This endpoint requires you to be authenticated.", section="/graphql")
        try:
            body = wire.GraphQLIn.model_validate_json(await request.body())
        except ValidationError as error:
            raise wire.Refusal(400, "Problems parsing JSON", section="/graphql") from error

        def find(owner: str, name: str) -> graphql.Visible | None:
            repository = self._world.repository(owner, name)
            if repository is None or self.permission(caller, repository) is None:
                return None
            return graphql.Visible(repository=repository, files=lambda: self._world.files(repository))

        answer = graphql.execute(body, find)
        for repository in answer.seen:
            self._world.saw(state.repository_ref(repository.owner, repository.name), Operation.READ)
        return Answered(200, answer.body)


def _number(text: str | None, default: int) -> int:
    if text is None:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def build_app(store: Store, clock: Clock) -> Starlette:
    api = GitHubApi(store, clock)
    table: list[tuple[str, str, Handler]] = [
        ("/user", "GET", api.user),
        ("/user/repos", "GET", api.user_repos),
        ("/repos/{owner}/{repo}", "GET", api.repository),
        ("/repos/{owner}/{repo}/contents", "GET", api.contents),
        ("/repos/{owner}/{repo}/contents/", "GET", api.contents),
        ("/repos/{owner}/{repo}/contents/{path:path}", "GET", api.contents),
        ("/repos/{owner}/{repo}/git/blobs/{sha}", "GET", api.blob),
        ("/repos/{owner}/{repo}/git/trees/{tree:path}", "GET", api.tree),
        ("/repos/{owner}/{repo}/commits", "GET", api.commits),
        ("/search/code", "GET", api.search_code),
        ("/graphql", "POST", api.graph),
    ]
    routes = [Route(path, api.endpoint(handler), methods=[method]) for path, method, handler in table]

    async def refused(request: Request, error: Exception) -> Response:
        status = error.status_code if isinstance(error, HTTPException) else 500
        message = "Not Found" if status == 404 else "Method Not Allowed" if status == 405 else "Server Error"
        refusal = wire.Refusal(status, message)
        return Response(wire.error_body(refusal), status_code=status, media_type=wire.JSON)

    return Starlette(routes=routes, exception_handlers={404: refused, 405: refused})
