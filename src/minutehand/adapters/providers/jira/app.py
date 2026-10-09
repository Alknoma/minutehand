"""The Jira Cloud REST API (v3 and Agile 1.0) and Atlassian's OAuth 2.0 (3LO) endpoints, as an ASGI app over the
run's store and clock.

One app answers three hosts, told apart by the request's host:

- `<site>.atlassian.net/rest/...` — the site;
- `api.atlassian.com/ex/jira/<cloudId>/rest/...` — the same site for an OAuth app;
  `api.atlassian.com/oauth/token/accessible-resources` lists the site;
- `auth.atlassian.com/oauth/token` — a refresh token or an authorization code traded for a new pair.

Minutehand does not enforce credentials or permissions: any credential, or none, is let in. A credential tells who
calls only: an API token or an access token the seed names acts as its account, a Basic username that is an
account's email acts as that account, and anything else acts as the agent's account. Who can see what is the
world's data and stays: an issue or project in which the caller holds no role answers 404.

Every operation of the vendor's reference for the resources this fake claims is either served by a handler below
or refused by name (`surface.UNSERVED`, raised as `NotServed`, which the proxy answers 501 in Jira's
error body); so is a documented query parameter or body property a served operation does not act on. Every
refusal is Jira's `{"errorMessages": [...], "errors": {...}}` with its status; the OAuth endpoints answer in
OAuth's own `{"error": ..., "error_description": ...}`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs

from pydantic import JsonValue
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters import answering
from minutehand.adapters.providers.jira import jql, search, state, wire
from minutehand.adapters.providers.jira.moves import Desk
from minutehand.adapters.providers.jira.surface import UNSERVED
from minutehand.domain.errors import NotServed
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

_JSON = "application/json;charset=UTF-8"
API_HOST = "api.atlassian.com"
AUTH_HOST = "auth.atlassian.com"
SCOPES = ["read:jira-work", "write:jira-work", "read:jira-user", "manage:jira-project", "offline_access"]
SEARCH_DEFAULT = 50
SEARCH_MOST = 5000
"""`/search/jql` answers at most 5000 issues a page, its reference says; 50 when `maxResults` is not given."""
_EX = re.compile(r"^/ex/jira/([^/]+)(/.*)$")
_GRANTS = ("authorization_code", "refresh_token", "client_credentials")
"""The grants Atlassian's token endpoint takes: an app's code and refresh token
(https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/) and a service account's client credentials
(https://support.atlassian.com/user-management/docs/create-oauth-2-0-credential-for-service-accounts/). Any other is
Atlassian's `invalid_request`, as recorded (`tests/providers/jira/data/observed/token_unsupported_grant.http`)."""
_UNAVAILABLE = (
    "<html><head><title>Atlassian Cloud Notifications - Page Unavailable</title></head>"
    "<body><h1>Page unavailable</h1></body></html>"
)
"""What a host under atlassian.net that holds no site answers, cut from the recorded page
(`tests/providers/jira/data/observed/site_unknown.http`): its title and heading."""
_TOKENS = uuid.UUID("5e0c5b1a-7a43-4d8e-a1f2-0d9b6f3c2e71")
_GLOBAL = ["ADMINISTER", "SYSTEM_ADMIN", "CREATE_PROJECT", "BULK_CHANGE", "USER_PICKER", "CREATE_SHARED_OBJECTS"]
_PROJECT = ["BROWSE_PROJECTS", "ADMINISTER_PROJECTS", "CREATE_ISSUES", "EDIT_ISSUES", "TRANSITION_ISSUES",
            "DELETE_ISSUES", "ADD_COMMENTS", "ASSIGN_ISSUES", "ASSIGNABLE_USER", "RESOLVE_ISSUES", "CLOSE_ISSUES",
            "LINK_ISSUES", "SCHEDULE_ISSUES"]  # fmt: skip
_PERMISSION_IDS = {key: str(n) for n, key in enumerate(_GLOBAL)} | {key: str(100 + n) for n, key in enumerate(_PROJECT)}
"""Every key `mypermissions` answers, with its id; a key not here is not a permission."""
_CLAIMED = {
    "/rest/api/3": {"issue", "comment", "search", "jql", "user", "users", "myself", "mypermissions", "project",
                    "worklog", "attachment", "issueLink", "issueLinkType", "field", "status", "statuscategory",
                    "priority", "issuetype", "resolution", "serverInfo"},
    "/rest/agile/1.0": {"board", "sprint"},
}  # fmt: skip
"""The first path segment of each resource this fake claims, under each API's root: a path there that no
operation of the reference names is a 404, as on Jira; any other path is outside what this fake serves."""


@dataclass(frozen=True)
class Call:
    """One call: who makes it, and the base its answers' `self` links are written against."""

    request: Request
    raw: bytes
    path: str
    params: dict[str, str]
    account: str
    base: str


Answer = tuple[int, object]
Handler = Callable[[Call], Answer]


@dataclass(frozen=True)
class Served:
    """One operation of the reference this fake serves: its handler, the query parameters the reference documents
    for it that the handler acts on (or that change nothing here), and those it refuses by name."""

    method: str
    path: str
    """The reference's own template, `{issueIdOrKey}` and all."""
    handler: Handler
    reads: frozenset[str] = frozenset()
    refuses: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Template:
    text: str
    pattern: re.Pattern[str]
    literals: int


def _template(text: str) -> _Template:
    regex = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(text))
    literals = sum(1 for part in text.split("/") if part and not part.startswith("{"))
    return _Template(text, re.compile(f"^{regex}/?$"), literals)


def _split(path: str) -> tuple[str, str]:
    """`path` as the API root it is under and the rest: `("/rest/api/3", "issue/KEY-1")`."""
    for root in _CLAIMED:
        if path.startswith(f"{root}/"):
            return root, path.removeprefix(f"{root}/")
    return "", path


def _param(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def _int(request: Request, name: str, default: int) -> int:
    """A whole-number query parameter; one Jira cannot convert is its problem body, as recorded
    (`data/observed/comments_max_results_not_a_number.http`)."""
    text = _param(request, name)
    if text is None or text == "":
        return default
    try:
        return int(text)
    except ValueError as error:
        raise wire.Problem(
            400, "Bad Request", f"Failed to convert '{name}' with value: '{text}'", request.url.path
        ) from error


def _page(request: Request, default: int, *, most: int | None = None) -> tuple[int, int]:
    """`startAt` and `maxResults` as the reference describes them for this operation: `default` when not given,
    and `most` when the reference names a ceiling (a larger value is taken as the ceiling)."""
    start = _int(request, "startAt", 0)
    size = _int(request, "maxResults", default)
    if start < 0 or size < 0:
        raise NotServed("a negative startAt or maxResults: Jira's reference does not say what it answers")
    return start, size if most is None else min(size, most)


class JiraApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._desk = Desk(store)
        self._world = self._desk.world
        self._clock = clock
        self._templates: dict[str, _Template] = {}
        self._served: dict[tuple[str, str], Served] = {}
        self._unserved: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ routing

    def serve(self, served: Served) -> None:
        self._templates.setdefault(served.path, _template(served.path))
        self._served[(served.method, served.path)] = served

    def refuse(self, path: str, methods: tuple[str, ...]) -> None:
        self._templates.setdefault(path, _template(path))
        self._unserved |= {(method, path) for method in methods}

    def served(self) -> list[Served]:
        return list(self._served.values())

    def resolve(self, method: str, path: str) -> tuple[Served, dict[str, str]]:
        """The operation `path` names, the most literal template winning (`/issue/picker` over `/issue/{key}`)."""
        matched = [(t, shape) for t in self._templates.values() if (shape := t.pattern.match(path)) is not None]
        if not matched:
            root, rest = _split(path)
            if root in _CLAIMED and rest.split("/")[0] in _CLAIMED[root]:
                raise wire.Problem(404, "Not Found", f"No endpoint {method} {path}.", path)
            raise NotServed(f"{method} {path} is outside the resources this fake serves")
        template, shape = max(matched, key=lambda pair: pair[0].literals)
        served = self._served.get((method, template.text))
        if served is not None:
            return served, shape.groupdict()
        if (method, template.text) in self._unserved:
            raise NotServed(f"{method} {template.text} is in Jira Cloud's reference; this fake does not serve it")
        raise wire.Problem(405, "Method Not Allowed", f"Method '{method}' is not supported.", path)

    async def answer(self, request: Request) -> Response:
        host = (request.url.hostname or "").lower()
        path = request.url.path
        try:
            if host == AUTH_HOST:
                return await self._token(request, path)
            if host == API_HOST and path == "/oauth/token/accessible-resources":
                return self._accessible()
            site = self._world.site()
            if host == API_HOST:
                shape = _EX.match(path)
                if shape is None:
                    return self._gateway_404(path)
                if shape.group(1) != site.cloudId:
                    return self._gateway_404(path)
                path = shape.group(2)
                base = f"https://{API_HOST}/ex/jira/{site.cloudId}"
            elif host == site.host:
                base = f"https://{site.host}"
            else:
                return Response(_UNAVAILABLE, status_code=404, media_type="text/html")
            served, params = self.resolve(request.method, path)
            refused = sorted(served.refuses & set(request.query_params))
            if refused:
                raise NotServed(f"the '{refused[0]}' parameter of {served.method} {served.path}")
            self._throttle(request.method, path)
            call = Call(request, await request.body(), path, params, self._caller(request), base)
            status, tree = served.handler(call)
            if status == 204 or tree is None:
                return Response(status_code=status)
            return Response(wire.render(tree), status_code=status, media_type=_JSON)
        except wire.Refusal as refusal:
            headers = {"Retry-After": str(refusal.retry_after)} if refusal.retry_after is not None else None
            return Response(wire.error_body(refusal), status_code=refusal.status, media_type=_JSON, headers=headers)
        except wire.Problem as problem:
            return Response(problem.body(), status_code=problem.status, media_type=wire.PROBLEM_TYPE)

    def _gateway_404(self, path: str) -> Response:
        """api.atlassian.com's own 404, as recorded (`data/observed/gateway_unknown_cloud_id.http`)."""
        stamp = self._clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        body = {
            "timestamp": stamp,
            "status": 404,
            "error": "Not Found",
            "message": "No message available",
            "path": path,
        }
        return _json(404, body)

    # ------------------------------------------------------------------ who calls

    def _caller(self, request: Request) -> str:
        """Who makes the call; never a refusal. A seeded API token (Basic) or access token (Bearer) is its
        account's, a Basic username that is an account's email is that account's, and anything else, or no
        Authorization at all, is the agent's."""
        agent = self._world.site().agent
        authorization = request.headers["authorization"] if "authorization" in request.headers else ""
        scheme, _, value = authorization.strip().partition(" ")
        value = value.strip()
        match scheme.lower():
            case "basic":
                try:
                    email, _, token = base64.b64decode(value).decode("utf-8").partition(":")
                except (binascii.Error, UnicodeDecodeError):
                    return agent
                for credential in self._world.credentials():
                    if credential.kind is wire.CredentialKind.API_TOKEN and credential.secret == token:
                        return credential.account
                user = self._world.user_by_email(email)
                return user.accountId if user is not None else agent
            case "bearer":
                for credential in self._world.credentials():
                    if credential.kind is wire.CredentialKind.OAUTH and credential.secret == value:
                        return credential.account
        return agent

    def _accessible(self) -> Response:
        """The site, to any token: credentials are not checked."""
        if not self._world.seeded():
            return _json(200, [])
        site = self._world.site()
        return _json(
            200,
            [
                {
                    "id": site.cloudId,
                    "url": f"https://{site.host}",
                    "name": site.name,
                    "scopes": list(SCOPES),
                    "avatarUrl": "https://site-admin-avatar-cdn.minutehand.invalid/avatars/jira.png",
                }
            ],
        )

    async def _token(self, request: Request, path: str) -> Response:
        """A new access and refresh token for any refresh token, authorization code or client: nothing is checked.
        A seeded grant's refresh token rotates it, so its new access token still acts as its account; any other
        grant's tokens act as the agent, as any unseeded token does."""
        if path != "/oauth/token" or request.method != "POST":
            raise NotServed(f"{request.method} {path} at {AUTH_HOST}: only POST /oauth/token is served")
        raw = await request.body()
        content_type = request.headers["content-type"] if "content-type" in request.headers else ""
        if content_type.split(";")[0].strip().lower() == "application/x-www-form-urlencoded":
            form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", errors="replace")).items()}
            body = wire.TokenIn.model_validate(form)
        else:
            try:
                body = wire.read_body(wire.TokenIn, raw)
            except wire.Refusal:
                return _json(400, {"error": "invalid_request", "error_description": "Incorrect request parameters"})
        if body.grant_type not in _GRANTS:
            return _json(400, {"error": "invalid_request",
                               "error_description": f"grant_type must be one of [{'|'.join(_GRANTS)}]"})  # fmt: skip
        now = self._clock.now()
        cloud = self._world.site().cloudId if self._world.seeded() else "unseeded"
        found = next(
            (
                c
                for c in self._world.credentials()
                if c.kind is wire.CredentialKind.OAUTH and c.refreshToken == body.refresh_token
            ),
            None,
        ) if body.grant_type == "refresh_token" and self._world.seeded() else None  # fmt: skip
        if found is not None:
            head = self._world.next_id()
            found = found.model_copy(
                update={
                    "secret": f"mh-at-{uuid.uuid5(_TOKENS, f'{cloud}/{head}/access')}",
                    "refreshToken": f"mh-rt-{uuid.uuid5(_TOKENS, f'{cloud}/{head}/refresh')}",
                    "issued": now,
                }
            )
            self._world.write_credential(found, actor=Actor.AGENT, create=False)
            access, refresh, lifetime = found.secret, found.refreshToken, found.lifetime
        else:
            grant = f"{cloud}/{body.grant_type}/{body.refresh_token or body.code or ''}/{now.isoformat()}"
            access = f"mh-at-{uuid.uuid5(_TOKENS, f'{grant}/access')}"
            refresh = f"mh-rt-{uuid.uuid5(_TOKENS, f'{grant}/refresh')}"
            lifetime = 3600
        return _json(
            200,
            {
                "access_token": access,
                "expires_in": lifetime,
                "token_type": "Bearer",
                "refresh_token": refresh,
                "scope": " ".join(SCOPES),
            },
        )

    def _throttle(self, method: str, path: str) -> None:
        for index, limit in self._world.rate_limits():
            if limit.method is not None and limit.method.upper() != method:
                continue
            if not path.startswith(limit.path):
                continue
            used = self._world.fault_use(index)
            if used < limit.times:
                self._world.spend_fault(index, used + 1)
                answering.injected()
                raise wire.rate_limited(limit.retry_after)

    # ------------------------------------------------------------------ lookups

    def _me(self, call: Call) -> wire.StoredUser:
        user = self._world.user(call.account)
        if user is None:
            raise LookupError(f"the caller {call.account} has no Jira account in this world")
        return user

    def _project(self, call: Call, reference: str) -> wire.StoredProject:
        project = self._world.find_project(reference)
        if project is None or not self._desk.can_browse(project, call.account):
            raise wire.no_project(reference)
        return project

    def _issue(self, call: Call, reference: str | None = None) -> tuple[wire.StoredIssue, wire.StoredProject]:
        issue = self._world.find_issue(reference or call.params["issueIdOrKey"])
        project = self._world.project(issue.project) if issue is not None else None
        if issue is None or project is None or not self._desk.can_browse(project, call.account):
            raise wire.no_issue()
        return issue, project

    def _now(self) -> datetime:
        return self._clock.now()

    # ------------------------------------------------------------------ presenting

    def user(self, call: Call, account: str | None) -> JsonValue:
        found = self._world.user(account) if account is not None else None
        return wire.user_out(call.base, found) if found is not None else None

    def issue_link(self, call: Call, issue: wire.StoredIssue) -> wire.Json:
        site = self._world.site()
        return {
            "id": issue.id,
            "key": issue.key,
            "self": f"{call.base}/rest/api/3/issue/{issue.id}",
            "fields": {
                "summary": issue.summary,
                "status": wire.status_out(call.base, site.status(issue.status)),
                "priority": wire.priority_out(call.base, site.priority(issue.priority)),
                "issuetype": wire.issue_type_out(call.base, site.issue_type(issue.issuetype)),
            },
        }

    def all_fields(self, call: Call, issue: wire.StoredIssue) -> wire.Json:
        site = self._world.site()
        project = self._world.project(issue.project)
        assert project is not None
        links: list[JsonValue] = []
        for link in self._world.links():
            if issue.id not in (link.source, link.destination):
                continue
            link_type = next(t for t in site.linkTypes if t.id == link.type)
            other = self._world.issue(link.destination if link.source == issue.id else link.source)
            if other is None:
                continue
            side = "outwardIssue" if link.source == issue.id else "inwardIssue"
            links.append(
                {
                    "id": link.id,
                    "self": f"{call.base}/rest/api/3/issueLink/{link.id}",
                    "type": wire.link_type_out(call.base, link_type),
                    side: self.issue_link(call, other),
                }
            )
        parent = self._world.issue(issue.parent) if issue.parent is not None else None
        timetracking: wire.Json = {}
        if issue.originalEstimateSeconds is not None:
            remaining = max(0, issue.originalEstimateSeconds - (issue.timeSpentSeconds or 0))
            timetracking |= {
                "originalEstimate": wire.duration(issue.originalEstimateSeconds),
                "remainingEstimate": wire.duration(remaining),
                "originalEstimateSeconds": issue.originalEstimateSeconds,
                "remainingEstimateSeconds": remaining,
            }
        if issue.timeSpentSeconds is not None:
            timetracking |= {
                "timeSpent": wire.duration(issue.timeSpentSeconds),
                "timeSpentSeconds": issue.timeSpentSeconds,
            }
        comments = self._world.comments(issue.id)
        fields: wire.Json = {
            "summary": issue.summary,
            "description": issue.description,
            "project": wire.project_ref_out(call.base, project),
            "issuetype": wire.issue_type_out(call.base, site.issue_type(issue.issuetype)),
            "status": wire.status_out(call.base, site.status(issue.status)),
            "statusCategory": wire.category_out(call.base, site.status(issue.status).category),
            "priority": wire.priority_out(call.base, site.priority(issue.priority)),
            "resolution": wire.resolution_out(call.base, site.resolution(issue.resolution))
            if issue.resolution
            else None,
            "resolutiondate": wire.jira_time(issue.resolutiondate) if issue.resolutiondate else None,
            "assignee": self.user(call, issue.assignee),
            "reporter": self.user(call, issue.reporter),
            "creator": self.user(call, issue.creator),
            "created": wire.jira_time(issue.created),
            "updated": wire.jira_time(issue.updated),
            "statuscategorychangedate": wire.jira_time(self._desk.category_changed(issue)),
            "duedate": wire.jira_date(issue.duedate) if issue.duedate else None,
            "labels": list(issue.labels),
            "subtasks": [self.issue_link(call, s) for s in self._world.subtasks(issue)],
            "issuelinks": links,
            "components": [],
            "fixVersions": [],
            "versions": [],
            "timetracking": timetracking,
            "watches": {
                "self": f"{call.base}/rest/api/3/issue/{issue.key}/watchers",
                "watchCount": 0,
                "isWatching": False,
            },
            "comment": {
                "comments": [self.comment(call, c) for c in comments],
                "self": f"{call.base}/rest/api/3/issue/{issue.id}/comment",
                "maxResults": len(comments),
                "total": len(comments),
                "startAt": 0,
            },
        }
        if parent is not None:
            fields["parent"] = self.issue_link(call, parent)
        for field in site.fields:
            value = issue.value(field.id)
            fields[field.id] = self.custom_value(call, field, value)
        return fields

    def custom_value(self, call: Call, field: wire.StoredField, value: JsonValue) -> JsonValue:
        if value is None:
            return None
        match field.kind:
            case wire.CustomFieldType.OPTION:
                option = next((o for o in field.options if o.id == value), None)
                return self.option(call, field, option) if option is not None else None
            case wire.CustomFieldType.OPTIONS:
                ids = value if isinstance(value, list) else []
                return [self.option(call, field, o) for o in field.options if o.id in ids]
            case wire.CustomFieldType.SPRINT:
                ids = value if isinstance(value, list) else []
                return [wire.sprint_value_out(s) for s in self._world.sprints() if s.id in ids] or None
            case wire.CustomFieldType.USER:
                return self.user(call, str(value))
            case _:
                return value

    def option(self, call: Call, field: wire.StoredField, option: wire.StoredOption) -> wire.Json:
        return {
            "self": f"{call.base}/rest/api/3/customFieldOption/{option.id}",
            "value": option.value,
            "id": option.id,
        }

    def issue_out(
        self, call: Call, issue: wire.StoredIssue, wanted: list[str] | None, expand: set[str], *, default: str
    ) -> wire.Json:
        """The issue with the fields `wanted` names (`*all`, `*navigable`, ids, `-id` to drop one; with only
        exclusions, `default` less those); None: `default`'s fields. `expand` holds `changelog` and `names` at
        most: its histories come most recent first, as the reference says of this expansion."""
        out: wire.Json = {
            "expand": "renderedFields,names,schema,operations,editmeta,changelog,versionedRepresentations",
            "id": issue.id,
            "self": f"{call.base}/rest/api/3/issue/{issue.id}",
            "key": issue.key,
        }
        every = self.all_fields(call, issue)
        chosen = _chosen(every, wanted if wanted is not None else [default], default)
        if chosen is not None:
            out["fields"] = chosen
        if "changelog" in expand:
            histories: list[JsonValue] = [self.history(call, h) for h in reversed(issue.history)]
            out["changelog"] = {
                "startAt": 0,
                "maxResults": len(histories),
                "total": len(histories),
                "histories": histories,
            }
        if "names" in expand and chosen is not None:
            out["names"] = {k: self.field_name(k) for k in chosen}
        return out

    def field_name(self, field_id: str) -> str:
        found = self._world.site().field(field_id)
        if found is not None:
            return found.name
        known = {key for key, _, _ in wire.SYSTEM_FIELDS}
        return wire.system_name(field_id) if field_id in known else field_id

    def history(self, call: Call, entry: wire.StoredHistory) -> wire.Json:
        out: wire.Json = {"id": entry.id}
        author = self.user(call, entry.author)
        if author is not None:
            out["author"] = author
        out["created"] = wire.jira_time(entry.created)
        out["items"] = [item.model_dump(mode="json", by_alias=True) for item in entry.items]
        return out

    def comment(self, call: Call, comment: wire.StoredComment) -> wire.Json:
        return {
            "self": f"{call.base}/rest/api/3/issue/{comment.issue}/comment/{comment.id}",
            "id": comment.id,
            "author": self.user(call, comment.author),
            "body": comment.body,
            "updateAuthor": self.user(call, comment.updateAuthor),
            "created": wire.jira_time(comment.created),
            "updated": wire.jira_time(comment.updated),
            "jsdPublic": True,
        }

    def project_out(self, call: Call, project: wire.StoredProject, expand: set[str]) -> wire.Json:
        """A project as `GET /project/{key}` answers it: description, issue types and lead always, as its
        reference says; `projectKeys` when expanded."""
        site = self._world.site()
        out: wire.Json = {
            "expand": "description,lead,issueTypes,url,projectKeys,permissions,insight",
            **wire.project_ref_out(call.base, project),
            "description": project.description,
            "lead": self.user(call, project.lead),
            "components": [],
            "issueTypes": [wire.issue_type_out(call.base, site.issue_type(s.issueType)) for s in project.screens],
            "assigneeType": project.assigneeType,
            "versions": [],
            "roles": self.roles(call, project),
            "style": "next-gen" if project.simplified else "classic",
            "isPrivate": False,
            "properties": {},
        }
        if "projectKeys" in expand:
            out["projectKeys"] = [project.key]
        return out

    def project_found(self, call: Call, project: wire.StoredProject, expand: set[str]) -> wire.Json:
        """A project as `GET /project/search` lists it: what its reference shows by default, and description,
        lead, issue types and keys only when expanded."""
        site = self._world.site()
        out: wire.Json = {
            "expand": "description,lead,issueTypes,url,projectKeys,permissions,insight",
            **wire.project_ref_out(call.base, project),
            "style": "next-gen" if project.simplified else "classic",
            "isPrivate": False,
            "properties": {},
        }
        if "description" in expand:
            out["description"] = project.description
        if "lead" in expand:
            out["lead"] = self.user(call, project.lead)
        if "issueTypes" in expand:
            out["issueTypes"] = [wire.issue_type_out(call.base, site.issue_type(s.issueType)) for s in project.screens]
        if "projectKeys" in expand:
            out["projectKeys"] = [project.key]
        return out

    def roles(self, call: Call, project: wire.StoredProject) -> wire.Json:
        return {r.name: f"{call.base}/rest/api/3/project/{project.id}/role/{r.id}" for r in self._world.site().roles}

    # ------------------------------------------------------------------ users

    def myself(self, call: Call) -> Answer:
        me = self._me(call)
        self._world.saw(state.user_ref(me.accountId), Operation.READ)
        return 200, wire.user_out(call.base, me) | {"groups": {"size": 0, "items": []}}

    def server_info(self, call: Call) -> Answer:
        return 200, {
            "baseUrl": call.base,
            "displayUrl": call.base,
            "version": "1001.0.0-SNAPSHOT",
            "versionNumbers": [1001, 0, 0],
            "deploymentType": "Cloud",
            "buildNumber": 100300,
            "buildDate": "2026-01-01T00:00:00.000+0000",
            "serverTime": wire.jira_time(self._now()),
            "scmInfo": "075425bbde28f069e4778bc256e773dd780217cf",
            "serverTitle": "Jira",
            "defaultLocale": {"locale": "en_US"},
        }

    def user_get(self, call: Call) -> Answer:
        account = _param(call.request, "accountId")
        if not account:
            raise NotServed("GET /rest/api/3/user without accountId: what Jira answers is not recorded")
        found = self._world.user(account)
        if found is None:
            raise wire.no_user()
        return 200, wire.user_out(call.base, found)

    def user_search(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-user-search/#api-rest-api-3-user-search-get
        — `query` or `accountId` is required, and both together is a 400; `maxResults` defaults to 50."""
        query = (_param(call.request, "query") or "").strip().lower()
        account = _param(call.request, "accountId")
        if not query and not account:
            raise wire.bad("The username or property query parameter must be provided")
        if query and account:
            raise wire.bad("The query parameters 'query' and 'accountId' are mutually exclusive.")
        found = [
            u
            for u in self._world.users()
            if u.active and (account is None or u.accountId == account) and (not query or _user_matches(u, query))
        ]
        start, most = _page(call.request, 50)
        self._world.saw(state.site_ref(), Operation.SEARCH)
        return 200, [wire.user_out(call.base, u) for u in found[start : start + most]]

    def users_search(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-users/#api-rest-api-3-users-search-get
        — `maxResults` defaults to 50 and is limited to 1000."""
        start, most = _page(call.request, 50, most=1000)
        everyone = sorted(self._world.users(), key=lambda u: u.accountId)
        self._world.saw(state.site_ref(), Operation.SEARCH)
        return 200, [wire.user_out(call.base, u) for u in everyone[start : start + most]]

    def assignable(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-user-search/#api-rest-api-3-user-assignable-search-get
        — a 400 when none of `issueKey`, `issueId` or `project` is present, when `query` and `accountId` are
        both missing, or both given."""
        project_ref = _param(call.request, "project")
        issue_ref = _param(call.request, "issueKey") or _param(call.request, "issueId")
        if issue_ref is None and project_ref is None:
            raise wire.bad("No project, issue key or issue ID was provided")
        query = (_param(call.request, "query") or "").strip().lower()
        account = _param(call.request, "accountId")
        if not query and not account:
            raise wire.bad("Returned if `query` or `accountId` is missing.")
        if query and account:
            raise wire.bad("Returned if `query` and `accountId` are provided.")
        if issue_ref is not None:
            _, project = self._issue(call, issue_ref)
        else:
            project = self._project(call, project_ref or "")
        start, most = _page(call.request, 50)
        found = [
            u
            for u in self._desk.assignable(project)
            if (account is None or u.accountId == account) and (not query or _user_matches(u, query))
        ]
        return 200, [wire.user_out(call.base, u) for u in found[start : start + most]]

    def permissions(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-permissions/#api-rest-api-3-mypermissions-get
        — `permissions` is required and every key in it must be a permission. Minutehand enforces no permission,
        so every global permission is held, and a project permission is held wherever the caller can see the
        project (`projectKey`, `projectId`, `issueKey`, `issueId`, or any project when none is named)."""
        asked = _param(call.request, "permissions")
        if not asked:
            raise wire.bad("The 'permissions' query parameter is required.")
        keys = [k.strip() for k in asked.split(",") if k.strip()]
        unknown = [k for k in keys if k not in _PERMISSION_IDS]
        if unknown:
            raise wire.Refusal(400, [], dict.fromkeys(unknown, "Unrecognized permission"))
        me = self._me(call)
        project_ref = _param(call.request, "projectKey") or _param(call.request, "projectId")
        issue_ref = _param(call.request, "issueKey") or _param(call.request, "issueId")
        if issue_ref:
            projects = [self._issue(call, issue_ref)[1]]
        elif project_ref:
            found = self._world.find_project(project_ref)
            if found is None or not self._desk.can_browse(found, call.account):
                raise wire.Refusal(404, ["Could not find project with provided key."])
            projects = [found]
        else:
            projects = self._world.projects()
        sees = any(self._desk.can_browse(p, me.accountId) for p in projects)
        answers: wire.Json = {}
        for key in keys:
            answers[key] = {
                "id": _PERMISSION_IDS[key],
                "key": key,
                "name": key.replace("_", " ").title(),
                "type": "GLOBAL" if key in _GLOBAL else "PROJECT",
                "description": "",
                "havePermission": key in _GLOBAL or sees,
            }
        return 200, {"permissions": answers}

    # ------------------------------------------------------------------ site objects

    def fields(self, call: Call) -> Answer:
        site = self._world.site()
        out: list[JsonValue] = [wire.system_field_out(k, n, s) for k, n, s in wire.SYSTEM_FIELDS]
        out += [wire.custom_field_out(f) for f in site.fields]
        return 200, out

    def statuses(self, call: Call) -> Answer:
        return 200, [wire.status_out(call.base, s) for s in self._world.site().statuses]

    def categories(self, call: Call) -> Answer:
        return 200, [wire.category_out(call.base, c) for c in wire.Category]

    def priorities(self, call: Call) -> Answer:
        site = self._world.site()
        return 200, [
            wire.priority_out(call.base, p) | {"isDefault": p.id == site.defaultPriority} for p in site.priorities
        ]

    def issue_types(self, call: Call) -> Answer:
        return 200, [wire.issue_type_out(call.base, t) for t in self._world.site().issueTypes]

    def resolutions(self, call: Call) -> Answer:
        return 200, [wire.resolution_out(call.base, r) for r in self._world.site().resolutions]

    def link_types(self, call: Call) -> Answer:
        return 200, {"issueLinkTypes": [wire.link_type_out(call.base, t) for t in self._world.site().linkTypes]}

    # ------------------------------------------------------------------ projects

    def project_search(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-projects/#api-rest-api-3-project-search-get
        — ordered by key unless `orderBy` says otherwise, filtered by `id`, `keys`, `query` and `typeKey`;
        `maxResults` defaults to 50, and more than 100 is taken as 100."""
        params = call.request.query_params
        query = (_param(call.request, "query") or "").strip().lower()
        keys, ids = params.getlist("keys"), params.getlist("id")
        types = {t.strip() for t in (_param(call.request, "typeKey") or "").split(",") if t.strip()}
        expand = _expand(_param(call.request, "expand"), served={"description", "lead", "issueTypes", "projectKeys"},
                         documented={"url", "insight"})  # fmt: skip
        found = [
            p
            for p in self._world.projects()
            if self._desk.can_browse(p, call.account)
            and (not query or query in p.key.lower() or query in p.name.lower())
            and (not keys or p.key in keys)
            and (not ids or p.id in ids)
            and (not types or p.projectTypeKey in types)
        ]
        order = _param(call.request, "orderBy") or "key"
        match order.lstrip("+-"):
            case "key":
                found.sort(key=lambda p: p.key)
            case "name":
                found.sort(key=lambda p: p.name.lower())
            case "owner" | "category" | "issueCount" | "lastIssueUpdatedTime" | "archivedDate" | "deletedDate":
                raise NotServed(f"orderBy={order} on GET /rest/api/3/project/search")
            case other:
                raise wire.bad(
                    "The field to order by should be one of [name, key, owner, category, issueCount, "
                    f"lastIssueUpdatedTime, archivedDate, deletedDate]. Instead, it was: {other}."
                )
        if order.startswith("-"):
            found.reverse()
        start, most = _page(call.request, 50, most=100)
        page = found[start : start + most]
        self._world.saw(state.site_ref(), Operation.SEARCH)
        out: wire.Json = {
            "self": f"{call.base}/rest/api/3/project/search?startAt={start}&maxResults={most}",
            "maxResults": most,
            "startAt": start,
            "total": len(found),
            "isLast": start + most >= len(found),
            "values": [self.project_found(call, p, expand) for p in page],
        }
        if start + most < len(found):
            out["nextPage"] = f"{call.base}/rest/api/3/project/search?startAt={start + most}&maxResults={most}"
        return 200, out

    def project_get(self, call: Call) -> Answer:
        project = self._project(call, call.params["projectIdOrKey"])
        expand = _expand(_param(call.request, "expand"), served={"description", "issueTypes", "lead", "projectKeys"},
                         documented={"issueTypeHierarchy"})  # fmt: skip
        self._world.saw(state.project_ref(project.id), Operation.READ)
        return 200, self.project_out(call, project, expand)

    def project_statuses(self, call: Call) -> Answer:
        project = self._project(call, call.params["projectIdOrKey"])
        site = self._world.site()
        return 200, [
            {
                "self": f"{call.base}/rest/api/3/issuetype/{s.issueType}",
                "id": s.issueType,
                "name": site.issue_type(s.issueType).name,
                "subtask": site.issue_type(s.issueType).subtask,
                "statuses": [wire.status_out(call.base, site.status(i)) for i in project.statuses],
            }
            for s in project.screens
        ]

    def project_create(self, call: Call) -> Answer:
        from minutehand.adapters.providers.jira.seed import default_project

        body = wire.read_body(wire.ProjectIn, call.raw)
        me = self._me(call)
        projects = self._world.projects()
        errors: dict[str, str] = {}
        key = body.key or ""
        if problem := wire.project_key_problem(key):
            errors["projectKey"] = problem
        else:
            holder = next((p for p in projects if p.key == key), None)
            if holder is not None:
                errors["projectKey"] = wire.PROJECT_INVALID
        if not body.name or any(p.name.lower() == body.name.lower() for p in projects):
            errors["projectName"] = wire.PROJECT_INVALID
        # The type may be left out when a template is named: the template builds exactly one type.
        template = body.projectTemplateKey
        project_type = body.projectTypeKey
        if template is not None and template not in wire.TEMPLATE_TYPES:
            errors["projectTemplateKey"] = wire.PROJECT_INVALID
        elif template is not None:
            builds = wire.TEMPLATE_TYPES[template]
            if not project_type:
                project_type = builds
            elif project_type != builds:
                errors["projectTemplateKey"] = wire.PROJECT_INVALID
        if not project_type or project_type not in wire.PROJECT_TYPES:
            errors["projectTypeKey"] = wire.PROJECT_INVALID
        lead = self._world.user(body.leadAccountId) if body.leadAccountId else None
        if lead is None:
            errors["leadAccountId"] = wire.PROJECT_INVALID
        if body.assigneeType not in (None, "PROJECT_LEAD", "UNASSIGNED"):
            errors["assigneeType"] = wire.PROJECT_INVALID
        if errors or lead is None or body.name is None or project_type is None:
            raise wire.Refusal(400, [], errors)
        site = self._world.site()
        made = default_project(
            site,
            self._world.next_id(),
            key,
            body.name,
            lead=lead.accountId,
            members=list(dict.fromkeys([lead.accountId, me.accountId])),
            administrators=list(dict.fromkeys([lead.accountId, me.accountId])),
            description=body.description or "",
            type_names=["Epic", "Task", "Subtask"],
        ).model_copy(
            update={"projectTypeKey": project_type, "simplified": True}
            | ({"assigneeType": body.assigneeType} if body.assigneeType is not None else {})
        )
        self._world.write_project(made, actor=Actor.AGENT)
        board_id = len(self._world.boards()) + 1
        kanban = body.projectTemplateKey is None or "kanban" in body.projectTemplateKey
        self._world.write_board(
            wire.StoredBoard(id=board_id, name=f"{key} board", project=made.id, type="kanban" if kanban else "scrum"),
            actor=Actor.AGENT,
        )
        return 201, {"self": f"{call.base}/rest/api/3/project/{made.id}", "id": int(made.id), "key": key}

    def role_list(self, call: Call) -> Answer:
        project = self._project(call, call.params["projectIdOrKey"])
        return 200, self.roles(call, project)

    def _role(self, call: Call) -> tuple[wire.StoredProject, wire.StoredRole]:
        project = self._project(call, call.params["projectIdOrKey"])
        role = next((r for r in self._world.site().roles if r.id == call.params["id"]), None)
        if role is None:
            raise wire.Refusal(404, ["Returned if the project or project role is not found."])
        return project, role

    def role_out(self, call: Call, project: wire.StoredProject, role: wire.StoredRole) -> wire.Json:
        actors: list[JsonValue] = []
        inactive_too = (_param(call.request, "excludeInactiveUsers") or "false").lower() != "true"
        for account in project.accounts(role.id):
            user = self._world.user(account)
            if user is not None and (user.active or inactive_too):
                actors.append(
                    {
                        "id": int(hashlib.sha1(account.encode()).hexdigest()[:6], 16),
                        "displayName": user.displayName,
                        "type": "atlassian-user-role-actor",
                        "actorUser": {"accountId": account},
                    }
                )
        return {
            "self": f"{call.base}/rest/api/3/project/{project.id}/role/{role.id}",
            "name": role.name,
            "id": int(role.id),
            "description": role.description,
            "actors": actors,
        }

    def role_get(self, call: Call) -> Answer:
        project, role = self._role(call)
        return 200, self.role_out(call, project, role)

    def role_add(self, call: Call) -> Answer:
        project, role = self._role(call)
        body = wire.read_body(wire.RoleActorsIn, call.raw)
        for account in body.user:
            if self._world.user(account) is None:
                raise wire.Refusal(404, ["Returned if the user or group is not found."])
        members = []
        for entry in project.members:
            if entry.role == role.id:
                entry = entry.model_copy(update={"accounts": list(dict.fromkeys([*entry.accounts, *body.user]))})
            members.append(entry)
        changed = project.model_copy(update={"members": members})
        self._world.write_project(changed, actor=Actor.AGENT)
        return 200, self.role_out(call, changed, role)

    # ------------------------------------------------------------------ search

    def search_removed(self, call: Call) -> Answer:
        raise wire.Refusal(
            410,
            ["The requested API has been removed. Please migrate to the /rest/api/3/search/jql API. A full migration "
             "guideline is available at https://developer.atlassian.com/changelog/#CHANGE-2046"],
        )  # fmt: skip

    def _query(self, text: str) -> jql.Query:
        query = jql.parse(text)
        if search.unbounded(query):
            raise wire.jql_error(
                "Unbounded JQL queries are not allowed here. Please add a search restriction to your query."
            )
        return query

    def _found(self, call: Call, text: str) -> list[wire.StoredIssue]:
        query = self._query(text)
        context = search.Context(self._desk, call.account, self._now())
        visible = [
            i
            for p in self._world.projects()
            if self._desk.can_browse(p, call.account)
            for i in self._world.issues(p.id)
        ]
        found = search.matching(query, visible, context)
        self._world.saw(state.site_ref(), Operation.SEARCH)
        return found

    def search_jql(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/#api-rest-api-3-search-jql-get
        — ids only unless `fields` says otherwise (exclusions alone start from the navigable fields), 50 a page
        unless `maxResults` says otherwise, at most 5000; `nextPageToken` on every page but the last; with
        `expand=names`, the results' field names beside the issues."""
        if call.request.method == "POST":
            body = wire.read_body(wire.SearchIn, call.raw)
        else:
            body = wire.SearchIn(
                jql=_param(call.request, "jql") or "",
                maxResults=_int(call.request, "maxResults", SEARCH_DEFAULT),
                fields=_listed(call.request, "fields"),
                expand=_param(call.request, "expand"),
                nextPageToken=_param(call.request, "nextPageToken"),
                fieldsByKeys=(_param(call.request, "fieldsByKeys") or "false").lower() == "true",
            )
        if body.fieldsByKeys:
            raise NotServed("fieldsByKeys=true: fields are named by id here")
        most = body.maxResults if body.maxResults is not None else SEARCH_DEFAULT
        if not 1 <= most <= SEARCH_MOST:
            raise wire.bad("The max results parameter has to be between 1 and 5,000.")
        expand = _expand(body.expand, served={"names", "changelog"}, documented=_ISSUE_EXPANDS | {"operations"})
        found = self._found(call, body.jql)
        start = _page_start(body.nextPageToken, body.jql)
        page = found[start : start + most]
        issues = [self.issue_out(call, i, body.fields or None, expand - {"names"}, default="id") for i in page]
        out: wire.Json = {"issues": list[JsonValue](issues), "isLast": start + most >= len(found)}
        if start + most < len(found):
            out["nextPageToken"] = _page_token(start + most, body.jql)
        if "names" in expand:
            shown: list[str] = []
            for issue in issues:
                fields = issue["fields"] if "fields" in issue else None
                if isinstance(fields, dict):
                    shown += [k for k in fields if k not in shown]
            out["names"] = {k: self.field_name(k) for k in shown}
        return 200, out

    def approximate_count(self, call: Call) -> Answer:
        body = wire.read_body(wire.CountIn, call.raw)
        return 200, {"count": len(self._found(call, body.jql))}

    # ------------------------------------------------------------------ issues

    def issue_get(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-get
        — every field unless `fields` says otherwise, which may be given more than once."""
        issue, _ = self._issue(call)
        if (_param(call.request, "fieldsByKeys") or "false").lower() == "true":
            raise NotServed("fieldsByKeys=true: fields are named by id here")
        expand = _expand(_param(call.request, "expand"), served={"names", "changelog"}, documented=_ISSUE_EXPANDS)
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, self.issue_out(call, issue, _listed(call.request, "fields"), expand, default="*all")

    def issue_create(self, call: Call) -> Answer:
        body = wire.read_body(wire.IssueIn, call.raw, missing=wire.bad("No Issue Create payload supplied"))
        site = self._world.site()
        project_ref = wire.read_ref(body.fields["project"]) if "project" in body.fields else None
        reference = (
            (project_ref.key or (str(project_ref.id) if project_ref.id is not None else None)) if project_ref else None
        )
        project = self._world.find_project(reference) if reference else None
        if project is None or not self._desk.can_browse(project, call.account):
            raise wire.bad_field("project", "Specify a valid project ID or key")
        type_ref = wire.read_ref(body.fields["issuetype"]) if "issuetype" in body.fields else None
        issue_type = None
        if type_ref is not None:
            issue_type = next(
                (
                    site.issue_type(s.issueType)
                    for s in project.screens
                    if str(type_ref.id) == s.issueType
                    or (type_ref.name or "").lower() == site.issue_type(s.issueType).name.lower()
                ),
                None,
            )
        if issue_type is None:
            raise wire.bad_field("issuetype", "Specify an issue type" if type_ref is None else wire.INVALID_VALUE)
        now = self._now()
        number = self._world.next_number(project.id)
        skeleton = wire.StoredIssue(
            id=self._world.next_id(),
            key=f"{project.key}-{number}",
            project=project.id,
            issuetype=issue_type.id,
            summary="",
            status=project.statuses[0],
            priority=site.defaultPriority,
            reporter=call.account,
            creator=call.account,
            created=now,
            updated=now,
        )
        issue = self._desk.apply_fields(skeleton, project, body.fields, creating=True)
        issue = self._update_ops(issue, project, body.update)
        self._world.create_issue(issue, actor=Actor.AGENT)
        self._desk.created(issue, at=now, actor=Actor.AGENT, who=None)
        return 201, {"id": issue.id, "key": issue.key, "self": f"{call.base}/rest/api/3/issue/{issue.id}"}

    def _update_ops(self, issue: wire.StoredIssue, project: wire.StoredProject, update: wire.Json) -> wire.StoredIssue:
        """The `update` block: `labels` take add, remove and set; any other field takes set."""
        for name, operations in update.items():
            if not isinstance(operations, list):
                raise NotServed(f"an update of '{name}' that is not a list of operations")
            for operation in operations:
                if not isinstance(operation, dict) or len(operation) != 1:
                    raise NotServed(f"an update operation on '{name}' that is not one verb and its value")
                verb, value = next(iter(operation.items()))
                labels_op = verb in ("add", "remove") and isinstance(value, str)
                if name == "labels" and labels_op:  # enum-lint: exempt Jira's own field id
                    labels = [*issue.labels, value] if verb == "add" else [x for x in issue.labels if x != value]
                    issue = self._desk.apply_fields(
                        issue, project, {"labels": list(dict.fromkeys(labels))}, creating=False
                    )
                elif verb == "set":
                    issue = self._desk.apply_fields(issue, project, {name: value}, creating=False)
                else:
                    raise NotServed(f"the update operation '{verb}' on '{name}': only set, and add or "
                                              "remove of a label, are served")  # fmt: skip
        return issue

    def issue_edit(self, call: Call) -> Answer:
        issue, project = self._issue(call)
        body = wire.read_body(wire.IssueIn, call.raw, missing=wire.bad("Returned if the request body is missing."))
        if (_param(call.request, "overrideScreenSecurity") or "false").lower() == "true" or (
            _param(call.request, "overrideEditableFlag") or "false"
        ).lower() == "true":
            raise NotServed("overrideScreenSecurity and overrideEditableFlag: screens hold here")
        changed = self._desk.apply_fields(issue, project, body.fields, creating=False)
        changed = self._update_ops(changed, project, body.update)
        written = self._desk.write(issue, changed, by=call.account, at=self._now(), actor=Actor.AGENT)
        if (_param(call.request, "returnIssue") or "false").lower() != "true":
            return 204, None
        expand = _expand(_param(call.request, "expand"), served={"names", "changelog"}, documented=_ISSUE_EXPANDS)
        return 200, self.issue_out(call, written, None, expand, default="*all")

    def issue_delete(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        with_subtasks = (_param(call.request, "deleteSubtasks") or "false").lower() == "true"
        if self._world.subtasks(issue) and not with_subtasks:
            raise wire.bad("Returned if the issue has subtasks and `deleteSubtasks` is not set to *true*.")
        self._desk.delete(issue, actor=Actor.AGENT)
        return 204, None

    def assign(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-assignee-put
        — `accountId` null unassigns, `"-1"` gives the project's default assignee, and none at all is a 400."""
        issue, project = self._issue(call)
        body = wire.read_body(wire.AssigneeIn, call.raw)
        if "accountId" not in body.model_fields_set:
            raise wire.bad("Returned if `name`, `key`, or `accountId` is missing.")
        account = body.accountId
        if account == "-1":
            account = project.lead if project.assigneeType == "PROJECT_LEAD" else None
        value: JsonValue = {"accountId": account} if account is not None else None
        changed = self._desk.apply_fields(issue, project, {"assignee": value}, creating=False)
        self._desk.write(issue, changed, by=call.account, at=self._now(), actor=Actor.AGENT)
        return 204, None

    def transitions_get(self, call: Call) -> Answer:
        issue, project = self._issue(call)
        site = self._world.site()
        with_fields = "transitions.fields" in _expand(
            _param(call.request, "expand"), served={"transitions.fields"}, documented=set()
        )
        only = _param(call.request, "transitionId")
        out: list[JsonValue] = []
        for transition in self._desk.transitions(issue, project):
            if only is not None and transition.id != only:
                continue
            entry: wire.Json = {
                "id": transition.id,
                "name": transition.name,
                "to": wire.status_out(call.base, site.status(transition.to)),
                "hasScreen": bool(transition.screen),
                "isGlobal": not transition.sources,
                "isInitial": False,
                "isAvailable": True,
                "isConditional": False,
                "isLooped": False,
            }
            if with_fields:
                entry["fields"] = {
                    f: self.field_meta(call, project, f, f in transition.required) for f in transition.screen
                }
            out.append(entry)
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, {"expand": "transitions", "transitions": out}

    def transition_do(self, call: Call) -> Answer:
        issue, project = self._issue(call)
        body = wire.read_body(wire.TransitionIn, call.raw)
        wanted = body.transition.id if body.transition is not None else None
        if wanted is None:
            raise wire.bad("Missing 'transition' identifier")
        transition = next((t for t in self._desk.transitions(issue, project) if t.id == str(wanted)), None)
        if transition is None:
            raise wire.bad("Returned if the request is invalid for any other reason.")
        self._desk.transition(
            issue,
            project,
            transition,
            fields=body.fields,
            update=body.update,
            by=call.account,
            at=self._now(),
            actor=Actor.AGENT,
            who=None,
        )
        return 204, None

    def field_meta(self, call: Call, project: wire.StoredProject, field_id: str, required: bool) -> wire.Json:
        site = self._world.site()
        custom = site.field(field_id)
        known = {key for key, _, _ in wire.SYSTEM_FIELDS}
        schema: wire.Json
        if custom is not None:
            schema, name = wire.field_schema(custom), custom.name
        elif field_id in known:
            schema, name = wire.system_schema(field_id), wire.system_name(field_id)
        else:
            schema, name = wire.Json({"type": "string", "system": field_id}), field_id
        operations: list[JsonValue] = ["set", "add", "remove"] if field_id == "labels" else ["set"]
        out: wire.Json = {
            "required": required,
            "schema": schema,
            "name": name,
            "key": field_id,
            "fieldId": field_id,
            "hasDefaultValue": field_id in ("priority", "issuetype"),
            "operations": operations,
        }
        if field_id == "priority":
            out["allowedValues"] = [wire.priority_out(call.base, p) for p in site.priorities]
        elif field_id == "resolution":
            out["allowedValues"] = [wire.resolution_out(call.base, r) for r in site.resolutions]
        elif custom is not None and custom.options:
            out["allowedValues"] = [self.option(call, custom, o) for o in custom.options]
        return out

    def createmeta_types(self, call: Call) -> Answer:
        project = self._project(call, call.params["projectIdOrKey"])
        site = self._world.site()
        types = [wire.issue_type_out(call.base, site.issue_type(s.issueType)) for s in project.screens]
        start, most = _page(call.request, 50, most=200)
        return 200, {
            "issueTypes": types[start : start + most],
            "maxResults": most,
            "startAt": start,
            "total": len(types),
        }

    def createmeta_fields(self, call: Call) -> Answer:
        project = self._project(call, call.params["projectIdOrKey"])
        screen = project.screen(call.params["issueTypeId"])
        if screen is None:
            raise wire.bad(wire.INVALID)
        fields = list(dict.fromkeys(["project", "issuetype", *screen.fields]))
        metas = [self.field_meta(call, project, f, f in screen.required) for f in fields]
        start, most = _page(call.request, 50, most=200)
        return 200, {"fields": metas[start : start + most], "maxResults": most, "startAt": start, "total": len(metas)}

    # ------------------------------------------------------------------ comments and changelog

    def comment_add(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        body = wire.read_body(wire.CommentIn, call.raw)
        if not wire.is_document(body.body):
            raise wire.bad_field("comment", wire.COMMENT_NOT_VALID)
        if not wire.adf_text(body.body).strip():
            raise wire.bad(wire.INVALID)
        written = self._desk.comment(issue, body.body, by=call.account, at=self._now(), actor=Actor.AGENT)
        return 201, self.comment(call, written)

    def comments_get(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/#api-rest-api-3-issue-issueidorkey-comment-get
        — 100 a page unless `maxResults` says otherwise; `orderBy` takes `created` (with `-` for newest first),
        and any other value is a 400."""
        issue, _ = self._issue(call)
        found = self._world.comments(issue.id)
        order = _param(call.request, "orderBy")
        if order is not None and order.lstrip("+-") != "created":
            raise wire.bad(f"The field to order by should be one of [created]. Instead, it was: {order.lstrip('+-')}.")
        if order is not None and order.startswith("-"):
            found = list(reversed(found))
        start, most = _page(call.request, 100)
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, {
            "startAt": start,
            "maxResults": most,
            "total": len(found),
            "comments": [self.comment(call, c) for c in found[start : start + most]],
        }

    def changelog(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        start, most = _page(call.request, 100)
        page = issue.history[start : start + most]
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, {
            "self": f"{call.base}/rest/api/3/issue/{issue.key}/changelog?maxResults={most}&startAt={start}",
            "maxResults": most,
            "startAt": start,
            "total": len(issue.history),
            "isLast": start + most >= len(issue.history),
            "values": [self.history(call, h) for h in page],
        }

    # ------------------------------------------------------------------ links

    def link_create(self, call: Call) -> Answer:
        body = wire.read_body(wire.LinkIn, call.raw)
        site = self._world.site()
        link_type = None
        if body.type is not None:
            link_type = next(
                (
                    t
                    for t in site.linkTypes
                    if str(body.type.id) == t.id or (body.type.name or "").lower() == t.name.lower()
                ),
                None,
            )
        if link_type is None:
            named = body.type.name if body.type is not None and body.type.name else None
            raise wire.Refusal(404, [f"No issue link type with name '{named}' found."] if named is not None else
                               [f"No issue link type with id '{body.type.id if body.type else ''}' found or you do "
                                "not have permission to view it."])  # fmt: skip
        if body.inwardIssue is None or body.outwardIssue is None:
            raise wire.Refusal(400, [wire.INVALID_PAYLOAD], bare=True)
        if body.comment is not None and not wire.is_document(body.comment.body):
            raise wire.bad_field("comment", wire.COMMENT_NOT_VALID)
        ends: list[wire.StoredIssue] = []
        for ref in (body.outwardIssue, body.inwardIssue):
            reference = ref.key or (str(ref.id) if ref.id is not None else "")
            issue, _ = self._issue(call, reference)
            ends.append(issue)
        outward, inward = ends
        self._world.write_link(
            wire.StoredLink(id=self._world.next_id(), type=link_type.id, source=outward.id, destination=inward.id),
            actor=Actor.AGENT,
        )
        if body.comment is not None:
            self._desk.comment(outward, body.comment.body, by=call.account, at=self._now(), actor=Actor.AGENT)
        return 201, None

    def _link(self, call: Call) -> wire.StoredLink:
        link = self._world.link(call.params["linkId"])
        if link is None:
            raise wire.Refusal(404, [f"No issue link with id '{call.params['linkId']}' exists."])
        for end in (link.source, link.destination):
            self._issue(call, end)
        return link

    def link_get(self, call: Call) -> Answer:
        link = self._link(call)
        site = self._world.site()
        outward, inward = self._world.issue(link.source), self._world.issue(link.destination)
        assert inward is not None and outward is not None
        return 200, {
            "id": link.id,
            "self": f"{call.base}/rest/api/3/issueLink/{link.id}",
            "type": wire.link_type_out(call.base, next(t for t in site.linkTypes if t.id == link.type)),
            "inwardIssue": self.issue_link(call, inward),
            "outwardIssue": self.issue_link(call, outward),
        }

    def link_delete(self, call: Call) -> Answer:
        link = self._link(call)
        self._world.delete_link(link, actor=Actor.AGENT)
        return 204, None

    # ------------------------------------------------------------------ agile

    def boards(self, call: Call) -> Answer:
        reference = _param(call.request, "projectKeyOrId")
        project = None
        if reference:
            project = self._world.find_project(reference)
            if project is None or not self._desk.can_browse(project, call.account):
                raise wire.bad(wire.INVALID)
        kind = _param(call.request, "type")
        name = (_param(call.request, "name") or "").lower()
        found = []
        for board in self._world.boards():
            home = self._world.project(board.project)
            if home is None or not self._desk.can_browse(home, call.account):
                continue
            if (project is not None and board.project != project.id) or (kind and board.type != kind):
                continue
            if name and name not in board.name.lower():
                continue
            found.append((board, home))
        start, most = _page(call.request, 50)
        page = found[start : start + most]
        return 200, {
            "maxResults": most,
            "startAt": start,
            "total": len(found),
            "isLast": start + most >= len(found),
            "values": [
                {
                    "id": board.id,
                    "self": f"{call.base}/rest/agile/1.0/board/{board.id}",
                    "name": board.name,
                    "type": board.type,
                    "location": {
                        "projectId": int(home.id),
                        "displayName": f"{home.name} ({home.key})",
                        "projectName": home.name,
                        "projectKey": home.key,
                        "projectTypeKey": home.projectTypeKey,
                        "name": f"{home.name} ({home.key})",
                    },
                }
                for board, home in page
            ],
        }

    def board_sprints(self, call: Call) -> Answer:
        board_id = call.params["boardId"]
        board = self._world.board(int(board_id)) if board_id.isdigit() else None
        home = self._world.project(board.project) if board is not None else None
        if board is None or home is None or not self._desk.can_browse(home, call.account):
            raise wire.Refusal(
                404, ["Returned if board does not exist or the user does not have permission to view it."]
            )
        if board.type == "kanban":
            raise wire.bad(wire.INVALID)
        states = {s.strip() for s in (_param(call.request, "state") or "").split(",") if s.strip()}
        found = [s for s in self._world.sprints() if s.board == board.id and (not states or s.state.value in states)]
        start, most = _page(call.request, 50)
        page = found[start : start + most]
        return 200, {
            "maxResults": most,
            "startAt": start,
            "isLast": start + most >= len(found),
            "values": [wire.sprint_out(call.base, s) for s in page],
        }

    def sprint_move(self, call: Call) -> Answer:
        sprint_id = call.params["sprintId"]
        sprint = self._world.sprint(int(sprint_id)) if sprint_id.isdigit() else None
        if sprint is None:
            raise wire.Refusal(
                404, ["Returned if the sprint does not exist or the user does not have permission to view it."]
            )
        body = wire.read_body(wire.SprintIssuesIn, call.raw)
        if not body.issues:
            raise wire.bad(wire.INVALID)
        if len(body.issues) > 50:
            raise wire.bad(wire.INVALID)
        if sprint.state is wire.SprintState.CLOSED:
            raise wire.bad(wire.INVALID)
        site = self._world.site()
        open_ids = {s.id for s in self._world.sprints() if s.state is not wire.SprintState.CLOSED}
        moving = [self._issue(call, reference) for reference in body.issues]
        now = self._now()
        for issue, _ in moving:
            held = issue.value(site.sprintField)
            kept = [s for s in held if isinstance(s, int) and s not in open_ids] if isinstance(held, list) else []
            value: list[JsonValue] = [*kept, sprint.id]
            custom = [v for v in issue.custom if v.field != site.sprintField]
            custom.append(wire.StoredValue(field=site.sprintField, value=value))
            self._desk.write(issue, issue.model_copy(update={"custom": custom}), by=call.account, at=now,
                             actor=Actor.AGENT)  # fmt: skip
        return 204, None


def _user_matches(user: wire.StoredUser, query: str) -> bool:
    words = user.displayName.lower().split()
    email = (user.emailAddress or "").lower()
    return (
        any(w.startswith(query) for w in words)
        or user.displayName.lower().startswith(query)
        or (user.emailVisible and email.startswith(query))
    )


_ISSUE_EXPANDS = {"renderedFields", "names", "schema", "transitions", "editmeta", "changelog",
                  "versionedRepresentations"}  # fmt: skip
"""The expansions the issue references document; this fake serves `names` and `changelog`."""


def _expand(text: str | None, *, served: set[str], documented: set[str]) -> set[str]:
    """The expansions asked for: one the reference documents and this fake does not serve is refused by name;
    one the reference does not document is ignored, as Jira ignores it."""
    asked = {e.strip() for e in (text or "").split(",") if e.strip()}
    for name in sorted(asked & (documented - served)):
        raise NotServed(f"expand={name}")
    return asked & served


def _listed(request: Request, name: str) -> list[str] | None:
    """A comma-separated parameter the reference lets be given more than once; None when it is not given."""
    if name not in request.query_params:
        return None
    return [part.strip() for value in request.query_params.getlist(name) for part in value.split(",") if part.strip()]


def _chosen(every: wire.Json, wanted: list[str], default: str) -> wire.Json | None:
    """The fields `wanted` names; None for ids only. Exclusions alone take from `default` (`*all`, `*navigable`)."""
    names = [w.strip() for w in wanted if w.strip()]
    if names == ["id"]:
        return None
    if names and all(n.startswith("-") for n in names):
        names = [default if default != "id" else "*navigable", *names]
    keep: set[str] = set()
    for name in names:
        if name in ("*all", "*navigable"):
            keep |= set(every) - ({"comment"} if name == "*navigable" else set())
        elif not name.startswith("-") and name in every:
            keep.add(name)
    for name in names:
        if name.startswith("-"):
            keep.discard(name[1:])
    return {k: v for k, v in every.items() if k in keep}


def _page_token(offset: int, text: str) -> str:
    check = hashlib.sha256(text.encode()).hexdigest()[:12]
    return base64.urlsafe_b64encode(f"{offset}:{check}".encode()).decode().rstrip("=")


def _page_start(token: str | None, text: str) -> int:
    if not token:
        return 0
    try:
        offset, _, check = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode().partition(":")
        if check != hashlib.sha256(text.encode()).hexdigest()[:12]:
            raise ValueError("another query's token")
        return int(offset)
    except (ValueError, binascii.Error, UnicodeDecodeError) as error:
        raise wire.bad("The provided next page token is invalid or expired.") from error


def _json(status: int, tree: object) -> Response:
    return Response(wire.render(tree), status_code=status, media_type=_JSON)


def _served(api: JiraApi) -> list[Served]:
    """Every operation served, by the reference's template, with the query parameters its reference documents:
    those the handler acts on (or that change nothing here: `failFast`, `notifyUsers`, `updateHistory`, a
    condition this fake has not got) and those refused by name."""
    v3, agile = "/rest/api/3", "/rest/agile/1.0"
    s = Served
    f = frozenset
    issue = f"{v3}/issue/{{issueIdOrKey}}"
    project = f"{v3}/project/{{projectIdOrKey}}"
    search_reads = f({"jql", "nextPageToken", "maxResults", "fields", "expand", "fieldsByKeys", "failFast",
                      "reconcileIssues", "includeArchivedProjects"})  # fmt: skip
    return [
        s("GET", f"{v3}/myself", api.myself, refuses=f({"expand"})),
        s("GET", f"{v3}/serverInfo", api.server_info),
        s("GET", f"{v3}/mypermissions", api.permissions,
          reads=f({"projectKey", "projectId", "issueKey", "issueId", "permissions"}),
          refuses=f({"projectUuid", "projectConfigurationUuid", "commentId"})),
        s("GET", f"{v3}/user", api.user_get, reads=f({"accountId"}), refuses=f({"username", "key", "expand"})),
        s("GET", f"{v3}/user/search", api.user_search,
          reads=f({"query", "accountId", "startAt", "maxResults"}), refuses=f({"username", "property"})),
        s("GET", f"{v3}/users/search", api.users_search, reads=f({"startAt", "maxResults"}), refuses=f({"expand"})),
        s("GET", f"{v3}/users", api.users_search, reads=f({"startAt", "maxResults"}), refuses=f({"expand"})),
        s("GET", f"{v3}/user/assignable/search", api.assignable,
          reads=f({"query", "sessionId", "accountId", "project", "issueKey", "issueId", "startAt", "maxResults"}),
          refuses=f({"username", "actionDescriptorId", "recommend", "accountType", "appType"})),
        s("GET", f"{v3}/field", api.fields),
        s("GET", f"{v3}/status", api.statuses),
        s("GET", f"{v3}/statuscategory", api.categories),
        s("GET", f"{v3}/priority", api.priorities),
        s("GET", f"{v3}/issuetype", api.issue_types),
        s("GET", f"{v3}/resolution", api.resolutions),
        s("GET", f"{v3}/issueLinkType", api.link_types),
        s("GET", f"{v3}/project/search", api.project_search,
          reads=f({"startAt", "maxResults", "orderBy", "id", "keys", "query", "typeKey", "action", "expand"}),
          refuses=f({"categoryId", "status", "properties", "propertyQuery"})),
        s("POST", f"{v3}/project", api.project_create),
        s("GET", project, api.project_get, reads=f({"expand"}), refuses=f({"properties"})),
        s("GET", f"{project}/statuses", api.project_statuses),
        s("GET", f"{project}/role", api.role_list),
        s("GET", f"{project}/role/{{id}}", api.role_get, reads=f({"excludeInactiveUsers"})),
        s("POST", f"{project}/role/{{id}}", api.role_add),
        s("GET", f"{v3}/search", api.search_removed,
          reads=f({"jql", "startAt", "maxResults", "validateQuery", "fields", "expand", "properties",
                   "fieldsByKeys", "failFast"})),
        s("POST", f"{v3}/search", api.search_removed),
        s("GET", f"{v3}/search/jql", api.search_jql, reads=search_reads, refuses=f({"properties"})),
        s("POST", f"{v3}/search/jql", api.search_jql),
        s("POST", f"{v3}/search/approximate-count", api.approximate_count),
        s("GET", f"{v3}/issue/createmeta/{{projectIdOrKey}}/issuetypes", api.createmeta_types,
          reads=f({"startAt", "maxResults"})),
        s("GET", f"{v3}/issue/createmeta/{{projectIdOrKey}}/issuetypes/{{issueTypeId}}", api.createmeta_fields,
          reads=f({"startAt", "maxResults"})),
        s("POST", f"{v3}/issue", api.issue_create, reads=f({"updateHistory"})),
        s("GET", issue, api.issue_get, reads=f({"fields", "fieldsByKeys", "expand", "updateHistory", "failFast"}),
          refuses=f({"properties"})),
        s("PUT", issue, api.issue_edit,
          reads=f({"notifyUsers", "overrideScreenSecurity", "overrideEditableFlag", "returnIssue", "expand"})),
        s("DELETE", issue, api.issue_delete, reads=f({"deleteSubtasks"})),
        s("PUT", f"{issue}/assignee", api.assign),
        s("GET", f"{issue}/transitions", api.transitions_get,
          reads=f({"expand", "transitionId", "skipRemoteOnlyCondition", "includeUnavailableTransitions"}),
          refuses=f({"sortByOpsBarAndStatus"})),
        s("POST", f"{issue}/transitions", api.transition_do),
        s("GET", f"{issue}/comment", api.comments_get, reads=f({"startAt", "maxResults", "orderBy"}),
          refuses=f({"expand"})),
        s("POST", f"{issue}/comment", api.comment_add, refuses=f({"expand"})),
        s("GET", f"{issue}/changelog", api.changelog, reads=f({"startAt", "maxResults"})),
        s("POST", f"{v3}/issueLink", api.link_create),
        s("GET", f"{v3}/issueLink/{{linkId}}", api.link_get),
        s("DELETE", f"{v3}/issueLink/{{linkId}}", api.link_delete),
        s("GET", f"{agile}/board", api.boards,
          reads=f({"startAt", "maxResults", "type", "name", "projectKeyOrId"}),
          refuses=f({"accountIdLocation", "projectLocation", "includePrivate", "negateLocationFiltering", "orderBy",
                     "expand", "projectTypeLocation", "filterId"})),
        s("GET", f"{agile}/board/{{boardId}}/sprint", api.board_sprints, reads=f({"startAt", "maxResults", "state"})),
        s("POST", f"{agile}/sprint/{{sprintId}}/issue", api.sprint_move),
    ]  # fmt: skip


def build(store: Store, clock: Clock) -> JiraApi:
    """The API with every operation it serves, and every other operation of its reference refused by name."""
    api = JiraApi(store, clock)
    for served in _served(api):
        api.serve(served)
    for path, methods in UNSERVED.items():
        api.refuse(path, methods)
    return api


def build_app(store: Store, clock: Clock) -> Starlette:
    api = build(store, clock)

    async def every(request: Request) -> Response:
        return await api.answer(request)

    methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    return Starlette(routes=[Route("/{path:path}", every, methods=methods)])
