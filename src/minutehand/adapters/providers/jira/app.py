"""The Jira Cloud REST API (v3 and Agile 1.0) and Atlassian's OAuth 2.0 (3LO) endpoints, as an ASGI app over the
run's store and clock.

One app answers three hosts, told apart by the request's host:

- `<site>.atlassian.net/rest/...` — the site, signed in with Basic (an account's email and an API token);
- `api.atlassian.com/ex/jira/<cloudId>/rest/...` — the same site for an OAuth app, signed in with a Bearer
  access token; `api.atlassian.com/oauth/token/accessible-resources` lists the sites a token reaches;
- `auth.atlassian.com/oauth/token` — a refresh token traded for a new pair (refresh tokens rotate).

Every refusal is Jira's `{"errorMessages": [...], "errors": {...}}` with its status; the OAuth endpoints answer
in OAuth's own `{"error": ..., "error_description": ...}`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from pydantic import JsonValue
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.answering import unrouted
from minutehand.adapters.providers.jira import jql, search, state, wire
from minutehand.adapters.providers.jira.moves import Desk
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

_JSON = wire.JSON
API_HOST = "api.atlassian.com"
AUTH_HOST = "auth.atlassian.com"
SCOPES = ["read:jira-work", "write:jira-work", "read:jira-user", "manage:jira-project", "offline_access"]
SEARCH_DEFAULT = 50
SEARCH_MOST = 100
ID_ONLY_MOST = 5000
_EX = re.compile(r"^/ex/jira/([^/]+)(/.*)$")
_TOKENS = uuid.UUID("5e0c5b1a-7a43-4d8e-a1f2-0d9b6f3c2e71")
_GLOBAL = ["ADMINISTER", "SYSTEM_ADMIN", "CREATE_PROJECT", "BULK_CHANGE", "USER_PICKER", "CREATE_SHARED_OBJECTS"]
_PROJECT_EDITS = ["CREATE_ISSUES", "EDIT_ISSUES", "TRANSITION_ISSUES", "DELETE_ISSUES", "ADD_COMMENTS",
                  "ASSIGN_ISSUES", "ASSIGNABLE_USER", "RESOLVE_ISSUES", "CLOSE_ISSUES", "LINK_ISSUES",
                  "SCHEDULE_ISSUES"]  # fmt: skip
_PROJECT = ["BROWSE_PROJECTS", "ADMINISTER_PROJECTS", *_PROJECT_EDITS]
_PERMISSION_IDS = {key: str(n) for n, key in enumerate(_GLOBAL)} | {key: str(100 + n) for n, key in enumerate(_PROJECT)}
"""Every key `mypermissions` answers, with its id; a key not here is not a permission."""


@dataclass(frozen=True)
class Call:
    """One authenticated call: who makes it, and the base its answers' `self` links are written against."""

    request: Request
    raw: bytes
    path: str
    params: dict[str, str]
    account: str
    base: str


Answer = tuple[int, object]
Handler = Callable[[Call], Answer]


def _param(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def _int(text: str | None, default: int, name: str) -> int:
    if text is None or text == "":
        return default
    try:
        return int(text)
    except ValueError as error:
        raise wire.bad(f"The '{name}' parameter must be a whole number.") from error


class JiraApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._desk = Desk(store)
        self._world = self._desk.world
        self._clock = clock
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = []
        self._served: list[str] = ["POST /oauth/token", "GET /oauth/token/accessible-resources"]
        """Every operation the fake answers, as `METHOD /path`, for naming the closest to one it does not."""

    # ------------------------------------------------------------------ routing

    def route(self, method: str, pattern: str, handler: Handler) -> None:
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
        self._routes.append((method, re.compile(f"^{regex}/?$"), handler))
        self._served.append(f"{method} {pattern}")

    async def answer(self, request: Request) -> Response:
        """The answer to `request`; a refusal, or an operation no route serves, leaves as the exception the guard
        renders (`adapters.answering`)."""
        host = (request.url.hostname or "").lower()
        path = request.url.path
        if host == AUTH_HOST:
            return await self._token(request, path)
        if host == API_HOST and path == "/oauth/token/accessible-resources":
            return self._accessible(request)
        ex = _EX.match(path) if host == API_HOST else None
        if host == API_HOST and ex is None:
            raise unrouted(request.method, path, self._served)
        if not self._world.seeded():
            raise wire.Refusal(404, [f"There is no Jira site at {host}."])
        site = self._world.site()
        if ex is not None:
            if ex.group(1) != site.cloudId:
                raise wire.GatewayRefusal(404, "No site has that cloud id")
            path = ex.group(2)
            base = f"https://{API_HOST}/ex/jira/{site.cloudId}"
            account = self._bearer(request)
        elif host == site.host:
            base = f"https://{site.host}"
            account = self._basic(request)
        else:
            raise wire.Refusal(404, [f"There is no Jira site at {host}."])
        for method, pattern, handler in self._routes:
            shape = pattern.match(path)
            if shape is None or method != request.method:
                continue
            self._throttle(site, request.method, path)
            call = Call(request, await request.body(), path, shape.groupdict(), account, base)
            status, tree = handler(call)
            if status == 204 or tree is None:
                return Response(status_code=status)
            return Response(wire.render(tree), status_code=status, media_type=_JSON)
        raise unrouted(request.method, path, self._served)

    # ------------------------------------------------------------------ sign-in

    def _basic(self, request: Request) -> str:
        authorization = request.headers["authorization"] if "authorization" in request.headers else ""
        scheme, _, value = authorization.strip().partition(" ")
        if scheme.lower() != "basic":
            raise wire.unauthenticated()
        try:
            email, _, token = base64.b64decode(value.strip()).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise wire.unauthenticated() from error
        for credential in self._world.credentials():
            if credential.kind is not wire.CredentialKind.API_TOKEN or credential.secret != token:
                continue
            user = self._world.user(credential.account)
            if user is not None and user.active and (user.emailAddress or "").lower() == email.strip().lower():
                return user.accountId
        raise wire.unauthenticated()

    def _oauth(self, request: Request) -> wire.StoredCredential | None:
        authorization = request.headers["authorization"] if "authorization" in request.headers else ""
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        now = self._clock.now()
        for credential in self._world.credentials():
            if credential.kind is wire.CredentialKind.OAUTH and credential.secret == token.strip():
                issued = credential.issued
                if issued is not None and issued + timedelta(seconds=credential.lifetime) <= now:
                    return None
                return credential
        return None

    def _bearer(self, request: Request) -> str:
        credential = self._oauth(request)
        if credential is None:
            raise wire.unauthenticated()
        user = self._world.user(credential.account)
        if user is None or not user.active:
            raise wire.unauthenticated()
        return user.accountId

    def _accessible(self, request: Request) -> Response:
        if not self._world.seeded() or self._oauth(request) is None:
            raise wire.GatewayRefusal(401, "Unauthorized")
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
        if path != "/oauth/token" or request.method != "POST":
            raise unrouted(request.method, path, self._served)
        raw = await request.body()
        content_type = request.headers["content-type"] if "content-type" in request.headers else ""
        if content_type.split(";")[0].strip().lower() == "application/x-www-form-urlencoded":
            form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", errors="replace")).items()}
            body = wire.TokenIn.model_validate(form)
        else:
            try:
                body = wire.read_body(wire.TokenIn, raw)
            except wire.Refusal as refusal:
                raise wire.OAuthRefusal(400, "invalid_request", "The body cannot be read.") from refusal
        if body.grant_type == "authorization_code":
            raise wire.OAuthRefusal(403, "invalid_grant", "That authorization code is unknown.")
        if body.grant_type != "refresh_token":
            raise wire.OAuthRefusal(400, "unsupported_grant_type", "Use refresh_token.")
        if not self._world.seeded():
            raise wire.OAuthRefusal(403, "invalid_grant", "Unknown or invalid refresh token.")
        found = next(
            (
                c
                for c in self._world.credentials()
                if c.kind is wire.CredentialKind.OAUTH and c.refreshToken == body.refresh_token
            ),
            None,
        )
        if found is None:
            raise wire.OAuthRefusal(403, "invalid_grant", "Unknown or invalid refresh token.")
        if found.clientId != body.client_id or found.clientSecret != body.client_secret:
            raise wire.OAuthRefusal(401, "access_denied", "The client is not who it says.")
        head = self._world.next_id()
        site = self._world.site()
        rotated = found.model_copy(
            update={
                "secret": f"mh-at-{uuid.uuid5(_TOKENS, f'{site.cloudId}/{head}/access')}",
                "refreshToken": f"mh-rt-{uuid.uuid5(_TOKENS, f'{site.cloudId}/{head}/refresh')}",
                "issued": self._clock.now(),
            }
        )
        self._world.write_credential(rotated, actor=Actor.AGENT, create=False)
        return _json(
            200,
            {
                "access_token": rotated.secret,
                "expires_in": rotated.lifetime,
                "token_type": "Bearer",
                "refresh_token": rotated.refreshToken,
                "scope": " ".join(SCOPES),
            },
        )

    def _throttle(self, site: wire.StoredSite, method: str, path: str) -> None:
        del site
        for index, limit in self._world.rate_limits():
            if limit.method is not None and limit.method.upper() != method:
                continue
            if not path.startswith(limit.path):
                continue
            used = self._world.fault_use(index)
            if used < limit.times:
                self._world.spend_fault(index, used + 1)
                raise wire.rate_limited(limit.retry_after)

    # ------------------------------------------------------------------ lookups

    def _me(self, call: Call) -> wire.StoredUser:
        user = self._world.user(call.account)
        if user is None:
            raise wire.unauthenticated()
        return user

    def _project(self, call: Call, reference: str) -> wire.StoredProject:
        project = self._world.find_project(reference)
        if project is None or not self._desk.can_browse(project, call.account):
            raise wire.no_project(reference)
        return project

    def _issue(self, call: Call, reference: str | None = None) -> tuple[wire.StoredIssue, wire.StoredProject]:
        issue = self._world.find_issue(reference or call.params["issue"])
        project = self._world.project(issue.project) if issue is not None else None
        if issue is None or project is None or not self._desk.can_browse(project, call.account):
            raise wire.no_issue()
        return issue, project

    def _may_edit(self, call: Call, project: wire.StoredProject, action: str) -> None:
        if not self._desk.can_edit(project, call.account):
            raise wire.forbidden(action)

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
            "statuscategorychangedate": wire.jira_time(issue.updated),
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

    def issue_out(self, call: Call, issue: wire.StoredIssue, wanted: list[str] | None, expand: str) -> wire.Json:
        """The issue with the fields `wanted` names (`*all`, `*navigable`, ids, `-id` to drop one); None: every one."""
        out: wire.Json = {
            "expand": "renderedFields,names,schema,operations,editmeta,changelog,versionedRepresentations",
            "id": issue.id,
            "self": f"{call.base}/rest/api/3/issue/{issue.id}",
            "key": issue.key,
        }
        every = self.all_fields(call, issue)
        chosen = _chosen(every, wanted)
        if chosen is not None:
            out["fields"] = chosen
        expands = {e.strip() for e in expand.split(",") if e.strip()}
        if "changelog" in expands:
            histories: list[JsonValue] = [self.history(call, h) for h in issue.history]
            out["changelog"] = {
                "startAt": 0,
                "maxResults": len(histories),
                "total": len(histories),
                "histories": histories,
            }
        if "names" in expands and chosen is not None:
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

    def project_out(self, call: Call, project: wire.StoredProject) -> wire.Json:
        site = self._world.site()
        return {
            "expand": "description,lead,issueTypes,url,projectKeys,permissions,insight",
            **wire.project_ref_out(call.base, project),
            "description": project.description,
            "lead": self.user(call, project.lead),
            "components": [],
            "issueTypes": [wire.issue_type_out(call.base, site.issue_type(s.issueType)) for s in project.screens],
            "assigneeType": "UNASSIGNED",
            "versions": [],
            "roles": self.roles(call, project),
            "style": "next-gen" if project.simplified else "classic",
            "isPrivate": False,
            "properties": {},
        }

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
            "scmInfo": "minutehand",
            "serverTitle": "Jira",
            "defaultLocale": {"locale": "en_US"},
        }

    def user_get(self, call: Call) -> Answer:
        account = _param(call.request, "accountId")
        if not account:
            raise wire.bad("The 'accountId' query parameter is required.")
        found = self._world.user(account)
        if found is None:
            raise wire.no_user()
        return 200, wire.user_out(call.base, found)

    def user_search(self, call: Call) -> Answer:
        query = (_param(call.request, "query") or "").strip().lower()
        account = _param(call.request, "accountId")
        if not query and not account:
            raise wire.bad("The 'query' or 'accountId' query parameter is required.")
        found = [
            u
            for u in self._world.users()
            if u.active and (account is None or u.accountId == account) and (not query or _user_matches(u, query))
        ]
        start = _int(_param(call.request, "startAt"), 0, "startAt")
        most = min(_int(_param(call.request, "maxResults"), 50, "maxResults"), 1000)
        self._world.saw(state.site_ref(), Operation.SEARCH)
        return 200, [wire.user_out(call.base, u) for u in found[start : start + most]]

    def users_search(self, call: Call) -> Answer:
        start = _int(_param(call.request, "startAt"), 0, "startAt")
        most = min(_int(_param(call.request, "maxResults"), 50, "maxResults"), 1000)
        everyone = sorted(self._world.users(), key=lambda u: u.accountId)
        self._world.saw(state.site_ref(), Operation.SEARCH)
        return 200, [wire.user_out(call.base, u) for u in everyone[start : start + most]]

    def assignable(self, call: Call) -> Answer:
        project_ref = _param(call.request, "project")
        issue_ref = _param(call.request, "issueKey") or _param(call.request, "issueId")
        if issue_ref is not None:
            _, project = self._issue(call, issue_ref)
        elif project_ref is not None:
            project = self._project(call, project_ref)
        else:
            raise wire.bad("Name a project or an issue: 'project', 'issueKey' or 'issueId'.")
        query = (_param(call.request, "query") or "").strip().lower()
        most = min(_int(_param(call.request, "maxResults"), 50, "maxResults"), 1000)
        found = [u for u in self._desk.assignable(project) if not query or _user_matches(u, query)]
        return 200, [wire.user_out(call.base, u) for u in found[:most]]

    def permissions(self, call: Call) -> Answer:
        asked = _param(call.request, "permissions")
        if not asked:
            raise wire.bad("The 'permissions' query parameter is required.")
        keys = [k.strip() for k in asked.split(",") if k.strip()]
        known = [k for k in keys if k in _PERMISSION_IDS]
        unknown = [k for k in keys if k not in _PERMISSION_IDS]
        if unknown:
            raise wire.bad(f"These permission keys are not valid: {', '.join(unknown)}.")
        me = self._me(call)
        reference = _param(call.request, "projectKey") or _param(call.request, "projectId")
        projects = [self._project(call, reference)] if reference else self._world.projects()
        answers: wire.Json = {}
        for key in known:
            if key in _GLOBAL:
                held = (me.siteAdmin and key != "SYSTEM_ADMIN") or key == "USER_PICKER"
                kind = "GLOBAL"
            elif key == "BROWSE_PROJECTS":
                held = any(self._desk.can_browse(p, me.accountId) for p in projects)
                kind = "PROJECT"
            elif key == "ADMINISTER_PROJECTS":
                held = any(self._desk.can_administer(p, me.accountId) for p in projects)
                kind = "PROJECT"
            else:
                held = any(self._desk.can_edit(p, me.accountId) for p in projects)
                kind = "PROJECT"
            answers[key] = {
                "id": _PERMISSION_IDS[key],
                "key": key,
                "name": key.replace("_", " ").title(),
                "type": kind,
                "description": "",
                "havePermission": held,
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
        query = (_param(call.request, "query") or "").strip().lower()
        found = [
            p
            for p in self._world.projects()
            if self._desk.can_browse(p, call.account)
            and (not query or query in p.key.lower() or query in p.name.lower())
        ]
        start = _int(_param(call.request, "startAt"), 0, "startAt")
        most = min(_int(_param(call.request, "maxResults"), 50, "maxResults"), 100)
        page = found[start : start + most]
        self._world.saw(state.site_ref(), Operation.SEARCH)
        return 200, {
            "self": f"{call.base}/rest/api/3/project/search?startAt={start}&maxResults={most}",
            "maxResults": most,
            "startAt": start,
            "total": len(found),
            "isLast": start + most >= len(found),
            "values": [self.project_out(call, p) for p in page],
        }

    def project_get(self, call: Call) -> Answer:
        project = self._project(call, call.params["project"])
        self._world.saw(state.project_ref(project.id), Operation.READ)
        return 200, self.project_out(call, project)

    def project_statuses(self, call: Call) -> Answer:
        project = self._project(call, call.params["project"])
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
        if not me.siteAdmin:
            raise wire.Refusal(403, ["You do not have permission to create projects."])
        projects = self._world.projects()
        errors: dict[str, str] = {}
        key = body.key or ""
        if problem := wire.project_key_problem(key):
            errors["projectKey"] = problem
        else:
            holder = next((p for p in projects if p.key == key), None)
            if holder is not None:
                errors["projectKey"] = f"The project '{holder.name}' already has this key."
        if not body.name:
            errors["projectName"] = "A project needs a name."
        elif any(p.name.lower() == body.name.lower() for p in projects):
            errors["projectName"] = "Another project already has this name."
        # The type may be left out when a template is named: the template builds exactly one type.
        template = body.projectTemplateKey
        project_type = body.projectTypeKey
        if template is not None and template not in wire.TEMPLATE_TYPES:
            errors["projectTemplateKey"] = "There is no project template with that key on this site."
        elif template is not None:
            builds = wire.TEMPLATE_TYPES[template]
            if not project_type:
                project_type = builds
            elif project_type != builds:
                errors["projectTemplateKey"] = f"This template makes a {builds} project, not a {project_type} one."
        if not project_type:
            errors["projectTypeKey"] = "A project needs a type, or a template that implies one."
        elif project_type not in wire.PROJECT_TYPES:
            errors["projectTypeKey"] = f"'{project_type}' is not a project type."
        lead = self._world.user(body.leadAccountId) if body.leadAccountId else None
        if lead is None:
            errors["leadAccountId"] = "The project lead must be an existing account, named by accountId."
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
        ).model_copy(update={"projectTypeKey": project_type, "simplified": True})
        self._world.write_project(made, actor=Actor.AGENT)
        board_id = len(self._world.boards()) + 1
        kanban = body.projectTemplateKey is None or "kanban" in body.projectTemplateKey
        self._world.write_board(
            wire.StoredBoard(id=board_id, name=f"{key} board", project=made.id, type="kanban" if kanban else "scrum"),
            actor=Actor.AGENT,
        )
        return 201, {"self": f"{call.base}/rest/api/3/project/{made.id}", "id": int(made.id), "key": key}

    def role_list(self, call: Call) -> Answer:
        project = self._project(call, call.params["project"])
        return 200, self.roles(call, project)

    def _role(self, call: Call) -> tuple[wire.StoredProject, wire.StoredRole]:
        project = self._project(call, call.params["project"])
        role = next((r for r in self._world.site().roles if r.id == call.params["role"]), None)
        if role is None:
            raise wire.Refusal(404, ["There is no project role with that id."])
        return project, role

    def role_out(self, call: Call, project: wire.StoredProject, role: wire.StoredRole) -> wire.Json:
        actors: list[JsonValue] = []
        for account in project.accounts(role.id):
            user = self._world.user(account)
            if user is not None:
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
            "description": f"The {role.name} of a project.",
            "actors": actors,
        }

    def role_get(self, call: Call) -> Answer:
        project, role = self._role(call)
        return 200, self.role_out(call, project, role)

    def role_add(self, call: Call) -> Answer:
        project, role = self._role(call)
        if not self._desk.can_administer(project, call.account):
            raise wire.forbidden("administer the project")
        body = wire.read_body(wire.RoleActorsIn, call.raw)
        if body.groupId:
            raise wire.bad("Groups cannot be added on this site.")
        for account in body.user:
            if self._world.user(account) is None:
                raise wire.Refusal(400, [], {"user": f"There is no user with accountId '{account}'."})
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
        raise wire.Refusal(410, ["This search has been removed: use /rest/api/3/search/jql."])

    def _query(self, text: str) -> jql.Query:
        query = jql.parse(text)
        if search.unbounded(query):
            raise wire.jql_error("A query with no restriction cannot be run here: add a condition to the JQL.")
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
        if call.request.method == "POST":
            body = wire.read_body(wire.SearchIn, call.raw)
        else:
            fields_param = _param(call.request, "fields")
            body = wire.SearchIn(
                jql=_param(call.request, "jql") or "",
                maxResults=_int(_param(call.request, "maxResults"), SEARCH_DEFAULT, "maxResults"),
                fields=[f for f in fields_param.split(",") if f] if fields_param is not None else None,
                expand=_param(call.request, "expand"),
                nextPageToken=_param(call.request, "nextPageToken"),
            )
        wanted = body.fields if body.fields else None
        id_only = wanted is None or wanted == ["id"]  # enum-lint: exempt Jira's `fields` value

        most = min(body.maxResults or SEARCH_DEFAULT, ID_ONLY_MOST if id_only else SEARCH_MOST)
        if most < 1:
            raise wire.bad("'maxResults' must be at least 1.")
        found = self._found(call, body.jql)
        start = _page_start(body.nextPageToken, body.jql)
        page = found[start : start + most]
        out: wire.Json = {
            "issues": [
                self.issue_out(call, i, wanted if wanted is not None else ["id"], body.expand or "") for i in page
            ],
            "isLast": start + most >= len(found),
        }
        if start + most < len(found):
            out["nextPageToken"] = _page_token(start + most, body.jql)
        return 200, out

    def approximate_count(self, call: Call) -> Answer:
        body = wire.read_body(wire.CountIn, call.raw)
        return 200, {"count": len(self._found(call, body.jql))}

    # ------------------------------------------------------------------ issues

    def issue_get(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        fields_param = _param(call.request, "fields")
        wanted = [f for f in fields_param.split(",") if f] if fields_param else None
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, self.issue_out(call, issue, wanted, _param(call.request, "expand") or "")

    def issue_create(self, call: Call) -> Answer:
        body = wire.read_body(wire.IssueIn, call.raw)
        site = self._world.site()
        project_ref = wire.read_ref(body.fields["project"]) if "project" in body.fields else None
        reference = (
            (project_ref.key or (str(project_ref.id) if project_ref.id is not None else None)) if project_ref else None
        )
        project = self._world.find_project(reference) if reference else None
        if project is None or not self._desk.can_browse(project, call.account):
            raise wire.bad_field("project", "Name a project you can see, by id or key.")
        self._may_edit(call, project, "create issues")
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
            raise wire.bad_field("issuetype", f"Name an issue type that {project.key} has, by id or name.")
        now = self._now()
        number = self._world.next_number(project.id)
        skeleton = wire.StoredIssue(
            id=self._world.next_issue_id(),
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
        return 201, {"id": issue.id, "key": issue.key, "self": f"{call.base}/rest/api/3/issue/{issue.id}"}

    def _update_ops(self, issue: wire.StoredIssue, project: wire.StoredProject, update: wire.Json) -> wire.StoredIssue:
        """The `update` block: `labels` take add, remove and set; any other field takes set."""
        for name, operations in update.items():
            if not isinstance(operations, list):
                raise wire.bad_field(name, "An update is a list of operations.")
            for operation in operations:
                if not isinstance(operation, dict) or len(operation) != 1:
                    raise wire.bad_field(name, "Each operation is one of set, add or remove.")
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
                    raise wire.bad_field(name, f"The operation '{verb}' is not supported for this field.")
        return issue

    def issue_edit(self, call: Call) -> Answer:
        issue, project = self._issue(call)
        self._may_edit(call, project, "edit issues")
        body = wire.read_body(wire.IssueIn, call.raw)
        changed = self._desk.apply_fields(issue, project, body.fields, creating=False)
        changed = self._update_ops(changed, project, body.update)
        self._desk.write(issue, changed, by=call.account, at=self._now(), actor=Actor.AGENT)
        return 204, None

    def issue_delete(self, call: Call) -> Answer:
        issue, project = self._issue(call)
        self._may_edit(call, project, "delete issues")
        with_subtasks = (_param(call.request, "deleteSubtasks") or "false").lower() == "true"
        if self._world.subtasks(issue) and not with_subtasks:
            raise wire.bad("The issue has subtasks: set deleteSubtasks=true to delete them with it.")
        self._desk.delete(issue, actor=Actor.AGENT)
        return 204, None

    def assign(self, call: Call) -> Answer:
        issue, project = self._issue(call)
        self._may_edit(call, project, "assign issues")
        body = wire.read_body(wire.AssigneeIn, call.raw)
        value: JsonValue = {"accountId": body.accountId} if body.accountId is not None else None
        changed = self._desk.apply_fields(issue, project, {"assignee": value}, creating=False)
        self._desk.write(issue, changed, by=call.account, at=self._now(), actor=Actor.AGENT)
        return 204, None

    def transitions_get(self, call: Call) -> Answer:
        issue, project = self._issue(call)
        site = self._world.site()
        with_fields = "transitions.fields" in (_param(call.request, "expand") or "")
        out: list[JsonValue] = []
        for transition in self._desk.transitions(issue, project):
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
        self._may_edit(call, project, "transition issues")
        body = wire.read_body(wire.TransitionIn, call.raw)
        wanted = body.transition.id if body.transition is not None else None
        if wanted is None:
            raise wire.bad("Name the transition to make by its id.")
        transition = next((t for t in self._desk.transitions(issue, project) if t.id == str(wanted)), None)
        if transition is None:
            raise wire.bad(f"Transition id '{wanted}' is not valid for this issue.")
        site = self._world.site()
        errors: dict[str, str] = {}
        resolution: str | None = None
        changed = issue
        for name, raw in body.fields.items():
            if name not in transition.screen:
                errors[name] = wire.not_on_screen(name)
            elif name == "resolution":
                ref = wire.read_ref(raw)
                found = None
                if ref is not None:
                    found = next(
                        (r for r in site.resolutions if str(ref.id) == r.id or (ref.name or "") == r.name), None
                    )
                if found is None:
                    errors[name] = "Name one of the site's resolutions by id or name."
                else:
                    resolution = found.id
            else:
                try:
                    changed = self._desk.apply_fields(
                        changed, project.model_copy(update={"screens": _with(project, issue, name)}), {name: raw},
                        creating=False,
                    )  # fmt: skip
                except wire.Refusal as refusal:
                    errors |= refusal.fields
        for name in transition.required:
            if name not in body.fields and name not in errors:
                errors[name] = f"{self.field_name(name)} is required."
        comment_body: JsonValue = None
        for name, operations in body.update.items():
            commenting = name == "comment"  # enum-lint: exempt Jira's own field id in a transition body
            if not commenting or not isinstance(operations, list):
                errors[name] = wire.not_on_screen(name)
                continue
            for operation in operations:
                add = operation["add"] if isinstance(operation, dict) and "add" in operation else None
                text = add["body"] if isinstance(add, dict) and "body" in add else None
                if not wire.is_document(text):
                    errors["comment"] = "A comment's body must be an Atlassian document."
                else:
                    comment_body = text
        if errors:
            raise wire.Refusal(400, [], errors)
        now = self._now()
        moved = self._desk.moved(changed, site.status(transition.to), resolution=resolution)
        self._desk.write(issue, moved, by=call.account, at=now, actor=Actor.AGENT)
        if comment_body is not None:
            self._desk.comment(moved, comment_body, by=call.account, at=now, actor=Actor.AGENT)
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
        project = self._project(call, call.params["project"])
        site = self._world.site()
        types = [wire.issue_type_out(call.base, site.issue_type(s.issueType)) for s in project.screens]
        start = _int(_param(call.request, "startAt"), 0, "startAt")
        most = _int(_param(call.request, "maxResults"), 50, "maxResults")
        return 200, {
            "issueTypes": types[start : start + most],
            "maxResults": most,
            "startAt": start,
            "total": len(types),
        }

    def createmeta_fields(self, call: Call) -> Answer:
        project = self._project(call, call.params["project"])
        screen = project.screen(call.params["type"])
        if screen is None:
            raise wire.Refusal(404, ["That issue type is not in this project."])
        fields = list(dict.fromkeys(["project", "issuetype", *screen.fields]))
        metas = [self.field_meta(call, project, f, f in screen.required) for f in fields]
        start = _int(_param(call.request, "startAt"), 0, "startAt")
        most = _int(_param(call.request, "maxResults"), 50, "maxResults")
        return 200, {"fields": metas[start : start + most], "maxResults": most, "startAt": start, "total": len(metas)}

    # ------------------------------------------------------------------ comments and changelog

    def comment_add(self, call: Call) -> Answer:
        issue, project = self._issue(call)
        self._may_edit(call, project, "add comments")
        body = wire.read_body(wire.CommentIn, call.raw)
        if not wire.is_document(body.body):
            raise wire.bad_field("comment", "A comment's body must be an Atlassian document (a 'doc' at version 1).")
        if not wire.adf_text(body.body).strip():
            raise wire.bad_field("comment", "A comment cannot be empty.")
        written = self._desk.comment(issue, body.body, by=call.account, at=self._now(), actor=Actor.AGENT)
        return 201, self.comment(call, written)

    def comments_get(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        found = self._world.comments(issue.id)
        if (_param(call.request, "orderBy") or "").startswith("-"):
            found = list(reversed(found))
        start = _int(_param(call.request, "startAt"), 0, "startAt")
        most = _int(_param(call.request, "maxResults"), 5000, "maxResults")
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, {
            "startAt": start,
            "maxResults": most,
            "total": len(found),
            "comments": [self.comment(call, c) for c in found[start : start + most]],
        }

    def changelog(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        start = _int(_param(call.request, "startAt"), 0, "startAt")
        most = _int(_param(call.request, "maxResults"), 100, "maxResults")
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
            raise wire.bad("Name an issue link type that exists on this site.")
        if body.inwardIssue is None or body.outwardIssue is None:
            raise wire.bad("A link needs both an inward and an outward issue.")
        ends: list[wire.StoredIssue] = []
        for ref in (body.outwardIssue, body.inwardIssue):
            reference = ref.key or (str(ref.id) if ref.id is not None else "")
            issue, project = self._issue(call, reference)
            self._may_edit(call, project, "link issues")
            ends.append(issue)
        outward, inward = ends
        self._world.write_link(
            wire.StoredLink(id=self._world.next_id(), type=link_type.id, source=outward.id, destination=inward.id),
            actor=Actor.AGENT,
        )
        if body.comment is not None and wire.is_document(body.comment.body):
            self._desk.comment(outward, body.comment.body, by=call.account, at=self._now(), actor=Actor.AGENT)
        return 201, None

    def _link(self, call: Call) -> wire.StoredLink:
        link = self._world.link(call.params["link"])
        if link is None:
            raise wire.Refusal(404, ["There is no issue link with that id."])
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
        source, project = self._issue(call, link.source)
        del source
        self._may_edit(call, project, "link issues")
        self._world.delete_link(link, actor=Actor.AGENT)
        return 204, None

    # ------------------------------------------------------------------ agile

    def boards(self, call: Call) -> Answer:
        reference = _param(call.request, "projectKeyOrId")
        project = None
        if reference:
            project = self._world.find_project(reference)
            if project is None or not self._desk.can_browse(project, call.account):
                raise wire.bad(f"No project you can see has the key or id '{reference}'.")
        kind = _param(call.request, "type")
        found = []
        for board in self._world.boards():
            home = self._world.project(board.project)
            if home is None or not self._desk.can_browse(home, call.account):
                continue
            if (project is not None and board.project != project.id) or (kind and board.type != kind):
                continue
            found.append((board, home))
        start = _int(_param(call.request, "startAt"), 0, "startAt")
        most = _int(_param(call.request, "maxResults"), 50, "maxResults")
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
        board_id = call.params["board"]
        board = self._world.board(int(board_id)) if board_id.isdigit() else None
        home = self._world.project(board.project) if board is not None else None
        if board is None or home is None or not self._desk.can_browse(home, call.account):
            raise wire.Refusal(404, ["That board does not exist, or you are not allowed to see it."])
        if board.type == "kanban":
            raise wire.bad("This board has no sprints: it is a Kanban board.")
        states = {s.strip() for s in (_param(call.request, "state") or "").split(",") if s.strip()}
        found = [s for s in self._world.sprints() if s.board == board.id and (not states or s.state.value in states)]
        start = _int(_param(call.request, "startAt"), 0, "startAt")
        most = _int(_param(call.request, "maxResults"), 50, "maxResults")
        page = found[start : start + most]
        return 200, {
            "maxResults": most,
            "startAt": start,
            "isLast": start + most >= len(found),
            "values": [wire.sprint_out(call.base, s) for s in page],
        }

    def sprint_move(self, call: Call) -> Answer:
        sprint_id = call.params["sprint"]
        sprint = self._world.sprint(int(sprint_id)) if sprint_id.isdigit() else None
        if sprint is None:
            raise wire.Refusal(404, ["That sprint does not exist, or you are not allowed to see it."])
        body = wire.read_body(wire.SprintIssuesIn, call.raw)
        if not body.issues:
            raise wire.bad("Name at least one issue to move.")
        if len(body.issues) > 50:
            raise wire.bad("At most 50 issues can be moved to a sprint at once.")
        if sprint.state is wire.SprintState.CLOSED:
            raise wire.bad("Issues cannot be moved into a closed sprint.")
        site = self._world.site()
        open_ids = {s.id for s in self._world.sprints() if s.state is not wire.SprintState.CLOSED}
        moving = [self._issue(call, reference) for reference in body.issues]
        for _, project in moving:
            self._may_edit(call, project, "schedule issues")
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


def _with(project: wire.StoredProject, issue: wire.StoredIssue, field: str) -> list[wire.StoredScreen]:
    """The project's screens with `field` on the issue's type: a transition screen holds what it holds."""
    return [
        s.model_copy(update={"fields": [*s.fields, field]}) if s.issueType == issue.issuetype else s
        for s in project.screens
    ]


def _user_matches(user: wire.StoredUser, query: str) -> bool:
    words = user.displayName.lower().split()
    email = (user.emailAddress or "").lower()
    return (
        any(w.startswith(query) for w in words)
        or user.displayName.lower().startswith(query)
        or (user.emailVisible and email.startswith(query))
    )


def _chosen(every: wire.Json, wanted: list[str] | None) -> wire.Json | None:
    if wanted is None:
        return every
    names = [w.strip() for w in wanted if w.strip()]
    if names == ["id"]:  # enum-lint: exempt Jira's `fields` parameter value, not a seed fact
        return None
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
        raise wire.bad("The nextPageToken is not valid for this query.") from error


def _json(status: int, tree: object) -> Response:
    return Response(wire.render(tree), status_code=status, media_type=_JSON)


def build_app(store: Store, clock: Clock) -> Starlette:
    api = JiraApi(store, clock)
    v3 = "/rest/api/3"
    table: list[tuple[str, str, Handler]] = [
        ("GET", f"{v3}/myself", api.myself),
        ("GET", f"{v3}/serverInfo", api.server_info),
        ("GET", f"{v3}/mypermissions", api.permissions),
        ("GET", f"{v3}/user", api.user_get),
        ("GET", f"{v3}/user/search", api.user_search),
        ("GET", f"{v3}/users/search", api.users_search),
        ("GET", f"{v3}/users", api.users_search),
        ("GET", f"{v3}/user/assignable/search", api.assignable),
        ("GET", f"{v3}/field", api.fields),
        ("GET", f"{v3}/status", api.statuses),
        ("GET", f"{v3}/statuscategory", api.categories),
        ("GET", f"{v3}/priority", api.priorities),
        ("GET", f"{v3}/issuetype", api.issue_types),
        ("GET", f"{v3}/resolution", api.resolutions),
        ("GET", f"{v3}/issueLinkType", api.link_types),
        ("GET", f"{v3}/project/search", api.project_search),
        ("POST", f"{v3}/project", api.project_create),
        ("GET", f"{v3}/project/{{project}}", api.project_get),
        ("GET", f"{v3}/project/{{project}}/statuses", api.project_statuses),
        ("GET", f"{v3}/project/{{project}}/role", api.role_list),
        ("GET", f"{v3}/project/{{project}}/role/{{role}}", api.role_get),
        ("POST", f"{v3}/project/{{project}}/role/{{role}}", api.role_add),
        ("GET", f"{v3}/search", api.search_removed),
        ("POST", f"{v3}/search", api.search_removed),
        ("GET", f"{v3}/search/jql", api.search_jql),
        ("POST", f"{v3}/search/jql", api.search_jql),
        ("POST", f"{v3}/search/approximate-count", api.approximate_count),
        ("GET", f"{v3}/issue/createmeta/{{project}}/issuetypes", api.createmeta_types),
        ("GET", f"{v3}/issue/createmeta/{{project}}/issuetypes/{{type}}", api.createmeta_fields),
        ("POST", f"{v3}/issue", api.issue_create),
        ("GET", f"{v3}/issue/{{issue}}", api.issue_get),
        ("PUT", f"{v3}/issue/{{issue}}", api.issue_edit),
        ("DELETE", f"{v3}/issue/{{issue}}", api.issue_delete),
        ("PUT", f"{v3}/issue/{{issue}}/assignee", api.assign),
        ("GET", f"{v3}/issue/{{issue}}/transitions", api.transitions_get),
        ("POST", f"{v3}/issue/{{issue}}/transitions", api.transition_do),
        ("GET", f"{v3}/issue/{{issue}}/comment", api.comments_get),
        ("POST", f"{v3}/issue/{{issue}}/comment", api.comment_add),
        ("GET", f"{v3}/issue/{{issue}}/changelog", api.changelog),
        ("POST", f"{v3}/issueLink", api.link_create),
        ("GET", f"{v3}/issueLink/{{link}}", api.link_get),
        ("DELETE", f"{v3}/issueLink/{{link}}", api.link_delete),
        ("GET", "/rest/agile/1.0/board", api.boards),
        ("GET", "/rest/agile/1.0/board/{board}/sprint", api.board_sprints),
        ("POST", "/rest/agile/1.0/sprint/{sprint}/issue", api.sprint_move),
    ]
    for method, path, handler in table:
        api.route(method, path, handler)

    async def every(request: Request) -> Response:
        return await api.answer(request)

    methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    return Starlette(routes=[Route("/{path:path}", every, methods=methods)])
