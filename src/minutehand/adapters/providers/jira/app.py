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

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email import policy
from email.parser import BytesParser
from typing import Literal, TypeVar
from urllib.parse import parse_qs

from pydantic import JsonValue
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from minutehand.adapters import answering
from minutehand.adapters.providers.jira import jql, search, state, webhooks, wire
from minutehand.adapters.providers.jira.moves import Adjust, Desk, Estimate, Event, Happened, remaining
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
                    "priority", "issuetype", "resolution", "serverInfo", "component", "version"},
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


_FIXED_FIELDS = ("project", "issuetype")  # enum-lint: exempt Jira's own field ids, which an edit cannot set
_FILE_PARAMETER = "file"  # enum-lint: exempt the name of the multipart parameter in Jira's attachment reference
REMOTE_LINK_404 = "the issue or remote issue link is not found or the user does not have permission to view the issue."
"""The remote link operations' 404, in the reference's words."""


@dataclass(frozen=True)
class Raw:
    """An answer that is not JSON: an attachment's bytes, or a redirect."""

    body: bytes
    content_type: str | None
    headers: dict[str, str]


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


_Item = TypeVar("_Item")


class JiraApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._desk = Desk(store)
        self._world = self._desk.world
        self._clock = clock
        self._templates: dict[str, _Template] = {}
        self._served: dict[tuple[str, str], Served] = {}
        self._unserved: set[tuple[str, str]] = set()
        self._sending: set[asyncio.Task[None]] = set()

    @property
    def desk(self) -> Desk:
        return self._desk

    def delivering(self) -> int:
        """Webhook deliveries started and not yet answered (`DeliversInBackground`)."""
        return len(self._sending)

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
            self._desk.outbox.clear()
            status, tree = served.handler(call)
            for outgoing in self.dispatch():
                task = asyncio.create_task(webhooks.send(outgoing))
                self._sending.add(task)
                task.add_done_callback(self._sending.discard)
            if isinstance(tree, Raw):
                return Response(tree.body, status_code=status, media_type=tree.content_type, headers=tree.headers)
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
        left = remaining(issue)
        if issue.originalEstimateSeconds is not None:
            timetracking |= {
                "originalEstimate": wire.duration(issue.originalEstimateSeconds),
                "originalEstimateSeconds": issue.originalEstimateSeconds,
            }
        if left is not None:
            timetracking |= {"remainingEstimate": wire.duration(left), "remainingEstimateSeconds": left}
        if issue.timeSpentSeconds is not None:
            timetracking |= {
                "timeSpent": wire.duration(issue.timeSpentSeconds),
                "timeSpentSeconds": issue.timeSpentSeconds,
            }
        watching = self._world.watchers(issue.id)
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
                "watchCount": len(watching),
                "isWatching": call.account in watching,
            },
            "attachment": [self.attachment_out(call, a) for a in self._world.attachments(issue.id)],
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
            "components": [self.component_out(call, c) for c in self._world.components(project.id)],
            "issueTypes": [wire.issue_type_out(call.base, site.issue_type(s.issueType)) for s in project.screens],
            "assigneeType": project.assigneeType,
            "versions": [self.version_out(call, v) for v in self._world.versions(project.id)],
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
        try:
            query = self._query(text)
        except jql.Unmatched:
            self._world.saw(state.site_ref(), Operation.SEARCH)
            return []
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

    # ------------------------------------------------------------------ comments: read, update, delete

    def _comment(self, call: Call) -> tuple[wire.StoredIssue, wire.StoredComment]:
        issue, _ = self._issue(call)
        found = next((c for c in self._world.comments(issue.id) if c.id == call.params["id"]), None)
        if found is None:
            raise wire.Refusal(
                404,
                ["Returned if the issue or comment is not found or the user does not have permission to view the "
                 "issue or comment."],
            )  # fmt: skip
        return issue, found

    def comment_get(self, call: Call) -> Answer:
        issue, found = self._comment(call)
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, self.comment(call, found)

    def comment_update(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/#api-rest-api-3-issue-issueidorkey-comment-id-put
        — the comment's body is replaced; its author and creation stay, `updated` and `updateAuthor` are now."""
        issue, found = self._comment(call)
        body = wire.read_body(wire.CommentIn, call.raw)
        if _flag(call.request, "overrideEditableFlag"):
            raise NotServed("overrideEditableFlag=true on PUT /rest/api/3/issue/{issueIdOrKey}/comment/{id}")
        if "body" not in body.model_fields_set:
            raise NotServed("a comment update with no body: what Jira keeps is not documented")
        if not wire.is_document(body.body):
            raise wire.bad_field("comment", wire.COMMENT_NOT_VALID)
        if not wire.adf_text(body.body).strip():
            raise wire.bad(wire.INVALID)
        changed = self._desk.edit_comment(issue, found, body.body, by=call.account, at=self._now(), actor=Actor.AGENT)
        return 200, self.comment(call, changed)

    def comment_delete(self, call: Call) -> Answer:
        _, found = self._comment(call)
        self._world.delete_comment(found, actor=Actor.AGENT)
        return 204, None

    # ------------------------------------------------------------------ worklogs

    def worklog(self, call: Call, worklog: wire.StoredWorklog) -> wire.Json:
        out: wire.Json = {
            "self": f"{call.base}/rest/api/3/issue/{worklog.issue}/worklog/{worklog.id}",
            "author": self.user(call, worklog.author),
            "updateAuthor": self.user(call, worklog.updateAuthor),
        }
        if worklog.comment is not None:
            out["comment"] = worklog.comment
        out |= {
            "created": wire.jira_time(worklog.created),
            "updated": wire.jira_time(worklog.updated),
            "started": wire.jira_time(worklog.started),
            "timeSpent": wire.worked(worklog.timeSpentSeconds),
            "timeSpentSeconds": worklog.timeSpentSeconds,
            "id": worklog.id,
            "issueId": worklog.issue,
        }
        return out

    def _seconds(self, body: wire.WorklogIn, *, required: bool) -> int | None:
        """`timeSpent` or `timeSpentSeconds`: at most one, and one when creating."""
        if body.timeSpent is not None and body.timeSpentSeconds is not None:
            raise wire.Refusal(400, [wire.INVALID_PAYLOAD], bare=True)
        if body.timeSpentSeconds is not None:
            if body.timeSpentSeconds <= 0:
                raise wire.Refusal(400, [wire.INVALID_PAYLOAD], bare=True)
            return body.timeSpentSeconds
        if body.timeSpent is not None:
            seconds = wire.parse_duration(body.timeSpent)
            if seconds is None:
                raise NotServed(f"timeSpent {body.timeSpent!r}: only days (#d), hours (#h) and minutes (#m or #) "
                                "are documented")  # fmt: skip
            if seconds <= 0:
                raise wire.Refusal(400, [wire.INVALID_PAYLOAD], bare=True)
            return seconds
        if required:
            raise wire.Refusal(400, [wire.INVALID_PAYLOAD], bare=True)
        return None

    def _started(self, text: str | None, *, required: bool) -> datetime | None:
        if text is None:
            if required:
                raise wire.Refusal(400, [wire.INVALID_PAYLOAD], bare=True)
            return None
        try:
            at = datetime.fromisoformat(text)
        except ValueError as error:
            raise wire.Refusal(400, [wire.INVALID_PAYLOAD], bare=True) from error
        if at.tzinfo is None:
            raise NotServed("a worklog `started` with no UTC offset: what Jira answers is not documented")
        return at

    def _adjust(self, call: Call, *, allowed: tuple[Estimate, ...], amount: str) -> Adjust:
        """`adjustEstimate` (`auto` unless given) with the `newEstimate` it needs, or the `amount` parameter
        (`reduceBy`, `increaseBy`) that `manual` needs."""
        text = _param(call.request, "adjustEstimate")
        if text is None:
            mode = Estimate.AUTO
        else:
            mode = next((m for m in Estimate if m.value == text), None)
            if mode is None:
                raise NotServed(f"adjustEstimate={text}: only new, leave, manual and auto are documented")
            if mode not in allowed:
                raise NotServed(f"adjustEstimate={text} on this operation: its reference does not document it")
        if mode is Estimate.NEW:
            return Adjust(mode, self._estimate_text(call, "newEstimate", mode))
        if mode is Estimate.MANUAL:
            return Adjust(mode, self._estimate_text(call, amount, mode))
        return Adjust(mode)

    def _estimate_text(self, call: Call, name: str, mode: Estimate) -> int:
        text = _param(call.request, name)
        seconds = wire.parse_duration(text) if text else None
        if seconds is None:
            raise wire.bad(f"`adjustEstimate` is set to `{mode.value}` but `{name}` is not provided or is invalid.")
        return seconds

    def _work(self, call: Call) -> tuple[wire.StoredIssue, wire.StoredWorklog]:
        issue, _ = self._issue(call)
        found = self._world.worklog(call.params["id"])
        if found is None or found.issue != issue.id:
            raise wire.Refusal(404, ["the worklog is not found or the user does not have permission to view it."])
        return issue, found

    def worklog_add(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-post
        — `started` and one of `timeSpent` and `timeSpentSeconds` are required; the remaining estimate moves as
        `adjustEstimate` says (`auto` unless given)."""
        issue, _ = self._issue(call)
        body = wire.read_body(wire.WorklogIn, call.raw)
        if _flag(call.request, "overrideEditableFlag"):
            raise NotServed("overrideEditableFlag=true on POST /rest/api/3/issue/{issueIdOrKey}/worklog")
        if body.comment is not None and not wire.is_document(body.comment):
            raise wire.bad_field("comment", wire.COMMENT_NOT_VALID)
        started = self._started(body.started, required=True)
        seconds = self._seconds(body, required=True)
        adjust = self._adjust(call, allowed=tuple(Estimate), amount="reduceBy")
        assert started is not None and seconds is not None
        made = self._desk.log_work(
            issue, started=started, seconds=seconds, comment=body.comment, adjust=adjust, by=call.account,
            at=self._now(), actor=Actor.AGENT,
        )  # fmt: skip
        return 201, self.worklog(call, made)

    def worklogs_get(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-get
        — oldest first, from the worklog started on or after `startedAfter`, or before `startedBefore`
        (milliseconds since the epoch)."""
        issue, _ = self._issue(call)
        if _param(call.request, "maxResults") is None:
            raise NotServed("GET /rest/api/3/issue/{issueIdOrKey}/worklog without maxResults: its default page size "
                            "is not documented")  # fmt: skip
        found = self._world.worklogs(issue.id)
        after = _int(call.request, "startedAfter", -1)
        before = _int(call.request, "startedBefore", -1)
        if _param(call.request, "startedAfter") is not None:
            found = [w for w in found if int(w.started.timestamp() * 1000) >= after]
        if _param(call.request, "startedBefore") is not None:
            found = [w for w in found if int(w.started.timestamp() * 1000) < before]
        start, most = _page(call.request, 0)
        page = found[start : start + most]
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, {
            "startAt": start,
            "maxResults": most,
            "total": len(page),
            "worklogs": [self.worklog(call, w) for w in page],
        }

    def worklog_get(self, call: Call) -> Answer:
        issue, found = self._work(call)
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, self.worklog(call, found)

    def worklog_update(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-id-put
        — what is sent changes; `auto` moves the remaining estimate by the difference in time spent."""
        issue, found = self._work(call)
        body = wire.read_body(wire.WorklogIn, call.raw)
        if _flag(call.request, "overrideEditableFlag"):
            raise NotServed("overrideEditableFlag=true on PUT /rest/api/3/issue/{issueIdOrKey}/worklog/{id}")
        if body.comment is not None and not wire.is_document(body.comment):
            raise wire.bad_field("comment", wire.COMMENT_NOT_VALID)
        started = self._started(body.started, required=False)
        seconds = self._seconds(body, required=False)
        adjust = self._adjust(call, allowed=(Estimate.NEW, Estimate.LEAVE, Estimate.AUTO), amount="")
        changed = self._desk.change_work(
            issue, found, started=started, seconds=seconds, comment=body.comment,
            set_comment=body.comment is not None, adjust=adjust, by=call.account, at=self._now(), actor=Actor.AGENT,
        )  # fmt: skip
        return 200, self.worklog(call, changed)

    def worklog_delete(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-id-delete"""
        issue, found = self._work(call)
        if _flag(call.request, "overrideEditableFlag"):
            raise NotServed("overrideEditableFlag=true on DELETE /rest/api/3/issue/{issueIdOrKey}/worklog/{id}")
        adjust = self._adjust(call, allowed=tuple(Estimate), amount="increaseBy")
        self._desk.remove_work(issue, [found], adjust=adjust, actor=Actor.AGENT)
        return 204, None

    def worklogs_delete(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/#api-rest-api-3-issue-issueidorkey-worklog-delete
        — at most 5000 worklogs, every one of them the issue's."""
        issue, _ = self._issue(call)
        body = wire.read_body(wire.WorklogIdsIn, call.raw, missing=wire.bad(wire.INVALID))
        if _flag(call.request, "overrideEditableFlag"):
            raise NotServed("overrideEditableFlag=true on DELETE /rest/api/3/issue/{issueIdOrKey}/worklog")
        if len(body.ids) > 5000:
            raise wire.bad("the number of worklogs being deleted exceeds the limit")
        adjust = self._adjust(call, allowed=(Estimate.LEAVE, Estimate.AUTO), amount="")
        found: list[wire.StoredWorklog] = []
        for number in body.ids:
            worklog = self._world.worklog(str(number))
            if worklog is None or worklog.issue != issue.id:
                raise wire.Refusal(404, ["at least one of the worklogs is not associated with the provided issue"])
            found.append(worklog)
        self._desk.remove_work(issue, found, adjust=adjust, actor=Actor.AGENT)
        return 204, None

    # ------------------------------------------------------------------ attachments

    def attachment_out(self, call: Call, attachment: wire.StoredAttachment) -> wire.Json:
        return {
            "self": f"{call.base}/rest/api/3/attachment/{attachment.id}",
            "id": attachment.id,
            "filename": attachment.filename,
            "author": self.user(call, attachment.author),
            "created": wire.jira_time(attachment.created),
            "size": attachment.size,
            "mimeType": attachment.mimeType,
            "content": f"{call.base}/rest/api/3/attachment/content/{attachment.id}",
        }

    def attachment_add(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/#api-rest-api-3-issue-issueidorkey-attachments-post
        — multipart/form-data whose parameter is named `file`, with the header `X-Atlassian-Token: no-check`; at
        most 60 files; the answer lists the attachments made."""
        issue, _ = self._issue(call)
        if call.request.headers.get("x-atlassian-token", "").lower() != "no-check":
            raise NotServed("an attachment upload without the header X-Atlassian-Token: no-check: the reference says "
                            "it is blocked, and not how")  # fmt: skip
        files = _files(call.request.headers.get("content-type", ""), call.raw)
        if not files:
            raise NotServed("an attachment upload with no file part: what Jira answers is not documented")
        if len(files) > 60:
            raise wire.Refusal(413, ["more than 60 files are requested to be uploaded."])
        now = self._now()
        made: list[JsonValue] = []
        for filename, mime, content in files:
            attachment = wire.StoredAttachment(
                id=self._world.next_id(), issue=issue.id, filename=filename, author=call.account, created=now,
                mimeType=mime, size=len(content),
            )  # fmt: skip
            self._world.write_attachment(attachment, content, actor=Actor.AGENT)
            made.append(self.attachment_out(call, attachment))
        return 200, made

    def _attachment(self, call: Call) -> wire.StoredAttachment:
        found = self._world.attachment(call.params["id"])
        if found is None:
            raise wire.Refusal(404, ["the attachment is not found."])
        self._issue(call, found.issue)
        return found

    def attachment_get(self, call: Call) -> Answer:
        found = self._attachment(call)
        self._world.saw(state.issue_ref(found.issue), Operation.READ)
        return 200, self.attachment_out(call, found)

    def attachment_delete(self, call: Call) -> Answer:
        found = self._attachment(call)
        self._world.delete_attachment(found, actor=Actor.AGENT)
        return 204, None

    def attachment_content(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/#api-rest-api-3-attachment-content-id-get
        — a redirect to the download unless `redirect` is false, which answers the bytes, or the part a `Range`
        header asks for (206), or 416 when it cannot be satisfied, or 400 when it is malformed."""
        found = self._attachment(call)
        content = self._world.blob(found.id)
        assert content is not None
        redirect = (_param(call.request, "redirect") or "true").lower()
        if redirect not in ("true", "false"):
            raise wire.Problem(
                400, "Bad Request", f"Failed to convert 'redirect' with value: '{redirect}'", call.request.url.path
            )
        if redirect == "true":
            where = f"{call.base}/rest/api/3/attachment/content/{found.id}?redirect=false"
            return 303, Raw(b"", None, {"Location": where})
        wanted = call.request.headers.get("range")
        if wanted is None:
            return 200, Raw(content, found.mimeType, {})
        span = _span(wanted, len(content))
        if span is None:
            raise wire.Refusal(400, ["the range supplied in the Range header is malformed."])
        first, last = span
        if first >= len(content) or first > last:
            return 416, Raw(b"", None, {"Content-Range": f"bytes */{len(content)}"})
        last = min(last, len(content) - 1)
        return 206, Raw(
            content[first : last + 1], found.mimeType, {"Content-Range": f"bytes {first}-{last}/{len(content)}"}
        )

    # ------------------------------------------------------------------ watchers

    def watchers_get(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        accounts = self._world.watchers(issue.id)
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, {
            "self": f"{call.base}/rest/api/3/issue/{issue.key}/watchers",
            "isWatching": call.account in accounts,
            "watchCount": len(accounts),
            "watchers": [self.user(call, a) for a in accounts],
        }

    def watcher_add(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-watchers/#api-rest-api-3-issue-issueidorkey-watchers-post
        — the body is the account's id as a JSON string; with none, the caller is added."""
        issue, _ = self._issue(call)
        account = call.account
        if call.raw.strip():
            try:
                given = json.loads(call.raw)
            except json.JSONDecodeError as error:
                raise wire.bad(wire.INVALID) from error
            if not isinstance(given, str):
                raise wire.bad(wire.INVALID)
            account = given
        if self._world.user(account) is None:
            raise wire.Refusal(
                404, ["Returned if the issue or the user is not found or the user does not have permission to view "
                      "the issue."]
            )  # fmt: skip
        held = self._world.watchers(issue.id)
        if account in held:
            raise NotServed("adding a watcher who already watches the issue: what Jira answers is not documented")
        self._world.write_watchers(issue.id, [*held, account], actor=Actor.AGENT)
        return 204, None

    def watcher_remove(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        account = _param(call.request, "accountId")
        if not account:
            raise wire.bad("Returned if `accountId` is not supplied.")
        if self._world.user(account) is None:
            raise wire.Refusal(
                404, ["Returned if the issue or the user is not found or the user does not have permission to view "
                      "the issue."]
            )  # fmt: skip
        held = self._world.watchers(issue.id)
        if account not in held:
            raise NotServed("removing an account that does not watch the issue: what Jira answers is not documented")
        self._world.write_watchers(issue.id, [a for a in held if a != account], actor=Actor.AGENT)
        return 204, None

    # ------------------------------------------------------------------ remote links

    def remote_link_out(self, call: Call, link: wire.StoredRemoteLink) -> wire.Json:
        out: wire.Json = {"id": int(link.id), "self": f"{call.base}/rest/api/3/issue/{link.issue}/remotelink/{link.id}"}
        if link.globalId is not None:
            out["globalId"] = link.globalId
        if link.application is not None:
            out["application"] = link.application
        if link.relationship is not None:
            out["relationship"] = link.relationship
        out["object"] = link.object
        return out

    def _remote_body(self, call: Call) -> wire.RemoteLinkIn:
        body = wire.read_body(wire.RemoteLinkIn, call.raw, missing=wire.bad(wire.INVALID))
        linked = body.object
        if (
            not isinstance(linked, dict)
            or not isinstance(linked.get("title"), str)
            or not isinstance(linked.get("url"), str)
            or (body.globalId is not None and len(body.globalId) > 255)
        ):
            raise wire.bad(wire.INVALID)
        return body

    def remote_link_post(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/#api-rest-api-3-issue-issueidorkey-remotelink-post
        — with a `globalId` that a link of the issue has, that link is replaced (what the request leaves out is
        null), else one is created."""
        issue, _ = self._issue(call)
        body = self._remote_body(call)
        held = next(
            (
                link
                for link in self._world.remote_links(issue.id)
                if body.globalId is not None and link.globalId == body.globalId
            ),
            None,
        )
        link = wire.StoredRemoteLink(
            id=held.id if held is not None else self._world.next_id(), issue=issue.id, globalId=body.globalId,
            application=body.application, object=body.object, relationship=body.relationship,
        )  # fmt: skip
        self._world.write_remote_link(link, actor=Actor.AGENT, create=held is None)
        return (200 if held is not None else 201), {
            "id": int(link.id),
            "self": f"{call.base}/rest/api/3/issue/{issue.id}/remotelink/{link.id}",
        }

    def remote_links_get(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        found = self._world.remote_links(issue.id)
        wanted = _param(call.request, "globalId")
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        if wanted is None:
            return 200, [self.remote_link_out(call, link) for link in found]
        one = next((link for link in found if link.globalId == wanted), None)
        if one is None:
            raise wire.Refusal(404, [REMOTE_LINK_404])
        return 200, self.remote_link_out(call, one)

    def _remote_link(self, call: Call) -> tuple[wire.StoredIssue, wire.StoredRemoteLink]:
        issue, _ = self._issue(call)
        number = call.params["linkId"]
        if not number.isdigit():
            raise wire.bad("the link ID is invalid.")
        link = self._world.remote_link(number)
        if link is None:
            raise wire.Refusal(404, [REMOTE_LINK_404])
        if link.issue != issue.id:
            raise wire.bad("the remote issue link does not belong to the issue.")
        return issue, link

    def remote_link_get(self, call: Call) -> Answer:
        _, link = self._remote_link(call)
        return 200, self.remote_link_out(call, link)

    def remote_link_put(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/#api-rest-api-3-issue-issueidorkey-remotelink-linkid-put
        — what the request leaves out is set to null."""
        issue, link = self._remote_link(call)
        body = self._remote_body(call)
        changed = wire.StoredRemoteLink(
            id=link.id, issue=issue.id, globalId=body.globalId, application=body.application, object=body.object,
            relationship=body.relationship,
        )  # fmt: skip
        self._world.write_remote_link(changed, actor=Actor.AGENT, create=False)
        return 204, None

    def remote_link_delete(self, call: Call) -> Answer:
        _, link = self._remote_link(call)
        self._world.delete_remote_link(link, actor=Actor.AGENT)
        return 204, None

    def remote_link_delete_global(self, call: Call) -> Answer:
        issue, _ = self._issue(call)
        wanted = _param(call.request, "globalId")
        if not wanted:
            raise wire.bad("Returned if a global ID isn't provided.")
        link = next((x for x in self._world.remote_links(issue.id) if x.globalId == wanted), None)
        if link is None:
            raise wire.Refusal(404, [REMOTE_LINK_404])
        self._world.delete_remote_link(link, actor=Actor.AGENT)
        return 204, None

    # ------------------------------------------------------------------ edit metadata

    def editmeta(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-editmeta-get
        — the fields of the issue's screen that an edit can set (the project and the issue type it cannot)."""
        issue, project = self._issue(call)
        if _flag(call.request, "overrideScreenSecurity") or _flag(call.request, "overrideEditableFlag"):
            raise NotServed("overrideScreenSecurity and overrideEditableFlag: screens hold here")
        screen = project.screen(issue.issuetype)
        names = [f for f in (screen.fields if screen is not None else []) if f not in _FIXED_FIELDS]
        required = screen.required if screen is not None else []
        return 200, {"fields": {f: self.field_meta(call, project, f, f in required) for f in names}}

    # ------------------------------------------------------------------ components and versions

    def component_out(self, call: Call, component: wire.StoredComponent) -> wire.Json:
        """The component with the assignee its `assigneeType` names and the one `realAssigneeType` says it falls back
        to, as the reference describes them."""
        project = self._world.project(component.project)
        assert project is not None
        default = project.lead if project.assigneeType == "PROJECT_LEAD" else None
        lead = component.leadAccountId
        nominal = {"PROJECT_LEAD": project.lead, "COMPONENT_LEAD": lead, "UNASSIGNED": None, "PROJECT_DEFAULT": default}
        real = "PROJECT_DEFAULT"
        if component.assigneeType == "COMPONENT_LEAD" and lead is None:
            pass
        elif component.assigneeType != "PROJECT_DEFAULT":
            real = component.assigneeType
        out: wire.Json = {"self": f"{call.base}/rest/api/3/component/{component.id}", "id": component.id}
        out["name"] = component.name
        if component.description is not None:
            out["description"] = component.description
        if lead is not None:
            out["lead"] = self.user(call, lead)
        out["assigneeType"] = component.assigneeType
        if nominal[component.assigneeType] is not None:
            out["assignee"] = self.user(call, nominal[component.assigneeType])
        out["realAssigneeType"] = real
        if nominal[real] is not None:
            out["realAssignee"] = self.user(call, nominal[real])
        out["isAssigneeTypeValid"] = not (component.assigneeType == "COMPONENT_LEAD" and lead is None)
        out["project"] = project.key
        out["projectId"] = int(project.id)
        return out

    def version_out(self, call: Call, version: wire.StoredVersion) -> wire.Json:
        out: wire.Json = {
            "self": f"{call.base}/rest/api/3/version/{version.id}",
            "id": version.id,
            "name": version.name,
        }
        if version.description is not None:
            out["description"] = version.description
        out["archived"] = version.archived
        out["released"] = version.released
        if version.startDate is not None:
            out["startDate"] = wire.jira_date(version.startDate)
        if version.releaseDate is not None:
            out["releaseDate"] = wire.jira_date(version.releaseDate)
        out["projectId"] = int(version.project)
        return out

    def component_create(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-components/#api-rest-api-3-component-post
        — `project` (the key) and `name` (at most 255 characters) are required; `assigneeType` is one of the four
        the reference lists (`PROJECT_DEFAULT` unless given); `leadAccountId` names an account."""
        body = wire.read_body(wire.ComponentIn, call.raw, missing=wire.bad(wire.INVALID))
        if not body.project:
            raise wire.bad("`projectId` is not provided.")
        if not body.name:
            raise wire.bad("`name` is not provided.")
        if len(body.name) > 255:
            raise wire.bad("`name` is over 255 characters in length.")
        kinds = ("PROJECT_DEFAULT", "COMPONENT_LEAD", "PROJECT_LEAD", "UNASSIGNED")
        if body.assigneeType is not None and body.assigneeType not in kinds:
            raise wire.bad("`assigneeType` is an invalid value.")
        if body.leadAccountId and self._world.user(body.leadAccountId) is None:
            raise wire.bad("the user is not found.")
        project = self._world.find_project(body.project)
        if project is None or not self._desk.can_browse(project, call.account):
            raise wire.Refusal(
                404, ["Returned if the project is not found or the user does not have permission to browse the "
                      "project containing the component."]
            )  # fmt: skip
        if any(c.name == body.name for c in self._world.components(project.id)):
            raise NotServed("a component whose name another component of the project holds: what Jira answers is not "
                            "documented")  # fmt: skip
        made = wire.StoredComponent(
            id=self._world.next_id(), project=project.id, name=body.name, description=body.description,
            leadAccountId=body.leadAccountId or None,
            assigneeType=_assignee_kind(body.assigneeType),
        )  # fmt: skip
        self._world.write_component(made, actor=Actor.AGENT)
        return 201, self.component_out(call, made)

    def component_get(self, call: Call) -> Answer:
        found = self._world.component(call.params["id"])
        project = self._world.project(found.project) if found is not None else None
        if found is None or project is None or not self._desk.can_browse(project, call.account):
            raise wire.Refusal(
                404, ["Returned if the component is not found or the user does not have permission to browse the "
                      "project containing the component."]
            )  # fmt: skip
        return 200, self.component_out(call, found)

    def _components(self, call: Call) -> list[wire.StoredComponent]:
        project = self._project(call, call.params["projectIdOrKey"])
        if (_param(call.request, "componentSource") or "jira") == "compass":
            raise NotServed("componentSource=compass: no project here uses Compass")
        return self._world.components(project.id)

    def project_components(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-components/#api-rest-api-3-project-projectidorkey-components-get"""
        return 200, [self.component_out(call, c) for c in self._components(call)]

    def project_components_page(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-components/#api-rest-api-3-project-projectidorkey-component-get
        — 50 a page; ordered by `description`, `issueCount`, `lead` or `name` (`-` reverses); `query` keeps the
        components whose name or description holds it, in any case."""
        found = self._components(call)
        query = (_param(call.request, "query") or "").lower()
        if query:
            found = [c for c in found if query in c.name.lower() or query in (c.description or "").lower()]
        order = _param(call.request, "orderBy")
        if order is not None:
            keys: dict[str, Callable[[wire.StoredComponent], str | int]] = {
                "description": lambda c: (c.description or "").lower(),
                "issueCount": lambda c: 0,
                "lead": lambda c: c.leadAccountId or "",
                "name": lambda c: c.name.lower(),
            }
            asked = order.lstrip("+-")
            if asked not in keys:
                raise wire.bad(f"The field to order by should be one of [{', '.join(keys)}]. Instead, it was: {asked}.")
            found.sort(key=keys[asked], reverse=order.startswith("-"))
        return 200, self._paged(call, found, lambda c: self.component_out(call, c))

    def version_create(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-versions/#api-rest-api-3-version-post
        — `name` (at most 255 characters) and `projectId` are required; dates are `yyyy-mm-dd`."""
        body = wire.read_body(wire.VersionIn, call.raw, missing=wire.bad(wire.INVALID))
        if not body.name or body.projectId is None or len(body.name) > 255:
            raise wire.bad(wire.INVALID)
        if body.released:
            raise NotServed("a version created already released: the reference says `released` is not applicable "
                            "when creating a version")  # fmt: skip
        try:
            start = date.fromisoformat(body.startDate) if body.startDate else None
            release = date.fromisoformat(body.releaseDate) if body.releaseDate else None
        except ValueError as error:
            raise wire.bad(wire.INVALID) from error
        project = self._world.project(str(body.projectId))
        if project is None or not self._desk.can_browse(project, call.account):
            raise wire.Refusal(404, ["the project is not found."])
        if any(v.name == body.name for v in self._world.versions(project.id)):
            raise NotServed("a version whose name another version of the project holds: what Jira answers is not "
                            "documented")  # fmt: skip
        made = wire.StoredVersion(
            id=self._world.next_id(), project=project.id, name=body.name, description=body.description,
            archived=bool(body.archived), startDate=start, releaseDate=release,
        )  # fmt: skip
        self._world.write_version(made, actor=Actor.AGENT)
        return 201, self.version_out(call, made)

    def version_get(self, call: Call) -> Answer:
        found = self._world.version(call.params["id"])
        project = self._world.project(found.project) if found is not None else None
        if found is None or project is None or not self._desk.can_browse(project, call.account):
            raise wire.Refusal(404, ["Returned if the version is not found or the user does not have the necessary "
                                     "permission."])  # fmt: skip
        return 200, self.version_out(call, found)

    def project_versions(self, call: Call) -> Answer:
        project = self._project(call, call.params["projectIdOrKey"])
        return 200, [self.version_out(call, v) for v in self._world.versions(project.id)]

    def project_versions_page(self, call: Call) -> Answer:
        """https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-project-versions/#api-rest-api-3-project-projectidorkey-version-get
        — 50 a page; ordered by `description`, `name`, `releaseDate` (those with none last), `sequence` (the order
        they were made in) or `startDate` (`-` reverses); `query` as for components; `status` is any of `released`,
        `unreleased` and `archived`, each by the flags the version holds."""
        project = self._project(call, call.params["projectIdOrKey"])
        found = self._world.versions(project.id)
        query = (_param(call.request, "query") or "").lower()
        if query:
            found = [v for v in found if query in v.name.lower() or query in (v.description or "").lower()]
        statuses = {s.strip() for s in (_param(call.request, "status") or "").split(",") if s.strip()}
        if statuses - {"released", "unreleased", "archived"}:
            raise wire.bad(wire.INVALID)
        if statuses:
            found = [v for v in found if _version_statuses(v) & statuses]
        order = _param(call.request, "orderBy")
        if order is not None:
            name, down = order.lstrip("+-"), order.startswith("-")
            dates: dict[str, Callable[[wire.StoredVersion], date | None]] = {
                "releaseDate": lambda v: v.releaseDate,
                "startDate": lambda v: v.startDate,
            }
            texts: dict[str, Callable[[wire.StoredVersion], str | int]] = {
                "description": lambda v: (v.description or "").lower(),
                "name": lambda v: v.name.lower(),
                "sequence": lambda v: int(v.id),
            }
            if name in dates:
                moment = dates[name]
                dated = sorted((v for v in found if moment(v) is not None), key=lambda v: moment(v) or date.min,
                               reverse=down)  # fmt: skip
                found = [*dated, *(v for v in found if moment(v) is None)]
            elif name in texts:
                found.sort(key=texts[name], reverse=down)
            else:
                raise wire.bad(wire.INVALID)
        return 200, self._paged(call, found, lambda v: self.version_out(call, v))

    def _paged(self, call: Call, found: list[_Item], show: Callable[[_Item], wire.Json]) -> wire.Json:
        start, most = _page(call.request, 50)
        where = f"{call.base}{call.request.url.path}"
        out: wire.Json = {
            "self": f"{where}?startAt={start}&maxResults={most}",
            "maxResults": most,
            "startAt": start,
            "total": len(found),
            "isLast": start + most >= len(found),
            "values": [show(x) for x in found[start : start + most]],
        }
        if start + most < len(found):
            out["nextPage"] = f"{where}?startAt={start + most}&maxResults={most}"
        return out

    # ------------------------------------------------------------------ webhooks

    def dispatch(self) -> list[webhooks.Outgoing]:
        """What the webhooks are owed for what has happened since last asked, each body built now from the world as
        it stands."""
        happened, self._desk.outbox = self._desk.outbox, []
        site = self._world.site() if self._world.seeded() else None
        if site is None or not site.hooks:
            return []
        out: list[webhooks.Outgoing] = []
        head = self._world.store.head()
        for n, event in enumerate(happened):
            issue = self._world.issue(event.issue)
            if issue is None:
                continue
            for hook in site.hooks:
                if event.event.value in hook.events and self._in_scope(hook, issue):
                    body = wire.render(self.hooked(event, issue, f"https://{site.host}"))
                    identifier = str(uuid.uuid5(_TOKENS, f"{site.cloudId}/{hook.id}/{head}/{n}"))
                    out.append(webhooks.Outgoing(hook, identifier, body))
        return out

    def _in_scope(self, hook: wire.StoredHook, issue: wire.StoredIssue) -> bool:
        if hook.jql is None:
            return True
        query = webhooks.check_filter(hook.jql)
        context = search.Context(self._desk, self._world.site().agent, self._now())
        return bool(search.matching(query, [issue], context))

    def hears(self, issue: wire.StoredIssue, event: Event) -> bool:
        """Whether some webhook is sent `event` for the issue."""
        if not self._world.seeded():
            return False
        return any(event.value in h.events and self._in_scope(h, issue) for h in self._world.site().hooks)

    def hooked(self, event: Happened, issue: wire.StoredIssue, base: str) -> wire.Json:
        """The body of a webhook delivery (https://developer.atlassian.com/cloud/jira/platform/webhooks/)."""
        who = self._world.user(event.by)
        assert who is not None
        shown = wire.user_out(base, who)
        for left_out in ("locale", "emailAddress"):
            shown.pop(left_out, None)
        presenter = Call(Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}),
                         b"", "/", {}, event.by, base)  # fmt: skip
        out: wire.Json = {
            "timestamp": int(event.at.timestamp() * 1000),
            "webhookEvent": event.event.value,
        }
        if event.event is Event.ISSUE_UPDATED:
            out["issue_event_type_name"] = "issue_generic"
        out["user"] = shown
        out["issue"] = self.issue_out(presenter, issue, None, set(), default="*all")
        if event.history is not None:
            entry = next(h for h in issue.history if h.id == event.history)
            items: list[JsonValue] = [i.model_dump(mode="json", by_alias=True) for i in entry.items]
            out["changelog"] = {"id": int(entry.id), "items": items}
        if event.comment is not None:
            comment = next(c for c in self._world.comments(issue.id) if c.id == event.comment)
            out["comment"] = self.comment(presenter, comment)
        return out

    async def flush(self) -> None:
        """Send, and wait for, what the webhooks are owed (a person's move made outside a request)."""
        for outgoing in self.dispatch():
            await webhooks.send(outgoing)

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


def _assignee_kind(
    text: str | None,
) -> Literal["PROJECT_DEFAULT", "COMPONENT_LEAD", "PROJECT_LEAD", "UNASSIGNED"]:
    """The component's `assigneeType`, `PROJECT_DEFAULT` unless given (the reference's default)."""
    match text:
        case "COMPONENT_LEAD" | "PROJECT_LEAD" | "UNASSIGNED" as kind:
            return kind
        case _:
            return "PROJECT_DEFAULT"


def _version_statuses(version: wire.StoredVersion) -> set[str]:
    """The statuses a version has by its flags: `released` or `unreleased`, and `archived` when it is."""
    held = {"released" if version.released else "unreleased"}
    return held | {"archived"} if version.archived else held


def _flag(request: Request, name: str) -> bool:
    return (_param(request, name) or "false").lower() == "true"


def _files(content_type: str, raw: bytes) -> list[tuple[str, str, bytes]]:
    """The files of a multipart/form-data upload as (file name, media type, bytes). The reference names one
    parameter, `file`; any other part is refused by name."""
    if content_type.split(";")[0].strip().lower() != "multipart/form-data":
        raise NotServed("an attachment upload that is not multipart/form-data")
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + raw
    )
    found: list[tuple[str, str, bytes]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        if name != _FILE_PARAMETER:
            raise NotServed(f"the multipart parameter {name!r}: the reference names only `file`")
        if not filename:
            raise NotServed("an attachment part with no file name: what Jira answers is not documented")
        content = part.get_payload(decode=True)
        media = part.get_content_type() if "content-type" in part else None
        found.append(
            (str(filename), media or mimetypes.guess_type(str(filename))[0] or "application/octet-stream",
             content if isinstance(content, bytes) else b"")
        )  # fmt: skip
    return found


_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _span(header: str, size: int) -> tuple[int, int] | None:
    """The first and last byte a `Range: bytes=a-b` header names; None when it is malformed."""
    if "," in header:
        raise NotServed("a Range header naming several ranges")
    found = _RANGE.match(header.strip())
    if found is None or (found.group(1) == "" and found.group(2) == ""):
        return None
    first, last = found.group(1), found.group(2)
    if first == "":
        return max(0, size - int(last)), size - 1
    return int(first), int(last) if last else size - 1


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
        s("PUT", f"{issue}/comment/{{id}}", api.comment_update, reads=f({"notifyUsers", "overrideEditableFlag"}),
          refuses=f({"expand"})),
        s("GET", f"{issue}/comment/{{id}}", api.comment_get, refuses=f({"expand"})),
        s("DELETE", f"{issue}/comment/{{id}}", api.comment_delete),
        s("GET", f"{issue}/changelog", api.changelog, reads=f({"startAt", "maxResults"})),
        s("GET", f"{issue}/worklog", api.worklogs_get,
          reads=f({"startAt", "maxResults", "startedAfter", "startedBefore"}), refuses=f({"expand"})),
        s("POST", f"{issue}/worklog", api.worklog_add,
          reads=f({"notifyUsers", "adjustEstimate", "newEstimate", "reduceBy", "overrideEditableFlag"}),
          refuses=f({"expand"})),
        s("DELETE", f"{issue}/worklog", api.worklogs_delete, reads=f({"adjustEstimate", "overrideEditableFlag"})),
        s("GET", f"{issue}/worklog/{{id}}", api.worklog_get, refuses=f({"expand"})),
        s("PUT", f"{issue}/worklog/{{id}}", api.worklog_update,
          reads=f({"notifyUsers", "adjustEstimate", "newEstimate", "overrideEditableFlag"}), refuses=f({"expand"})),
        s("DELETE", f"{issue}/worklog/{{id}}", api.worklog_delete,
          reads=f({"notifyUsers", "adjustEstimate", "newEstimate", "increaseBy", "overrideEditableFlag"})),
        s("POST", f"{issue}/attachments", api.attachment_add),
        s("GET", f"{v3}/attachment/{{id}}", api.attachment_get),
        s("DELETE", f"{v3}/attachment/{{id}}", api.attachment_delete),
        s("GET", f"{v3}/attachment/content/{{id}}", api.attachment_content, reads=f({"redirect"})),
        s("GET", f"{issue}/watchers", api.watchers_get),
        s("POST", f"{issue}/watchers", api.watcher_add),
        s("DELETE", f"{issue}/watchers", api.watcher_remove, reads=f({"accountId"}), refuses=f({"username"})),
        s("GET", f"{issue}/remotelink", api.remote_links_get, reads=f({"globalId"})),
        s("POST", f"{issue}/remotelink", api.remote_link_post),
        s("DELETE", f"{issue}/remotelink", api.remote_link_delete_global, reads=f({"globalId"})),
        s("GET", f"{issue}/remotelink/{{linkId}}", api.remote_link_get),
        s("PUT", f"{issue}/remotelink/{{linkId}}", api.remote_link_put),
        s("DELETE", f"{issue}/remotelink/{{linkId}}", api.remote_link_delete),
        s("GET", f"{issue}/editmeta", api.editmeta, reads=f({"overrideScreenSecurity", "overrideEditableFlag"})),
        s("POST", f"{v3}/component", api.component_create),
        s("GET", f"{v3}/component/{{id}}", api.component_get),
        s("GET", f"{project}/components", api.project_components, reads=f({"componentSource"})),
        s("GET", f"{project}/component", api.project_components_page,
          reads=f({"startAt", "maxResults", "orderBy", "componentSource", "query"})),
        s("POST", f"{v3}/version", api.version_create),
        s("GET", f"{v3}/version/{{id}}", api.version_get, refuses=f({"expand"})),
        s("GET", f"{project}/versions", api.project_versions, refuses=f({"expand"})),
        s("GET", f"{project}/version", api.project_versions_page,
          reads=f({"startAt", "maxResults", "orderBy", "query", "status"}), refuses=f({"expand"})),
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


class JiraApp:
    """The ASGI app, and what it still has to send: `DeliversInBackground`, as Notion's webhooks are."""

    def __init__(self, api: JiraApi, routes: Starlette) -> None:
        self._api = api
        self._routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._routes(scope, receive, send)

    def delivering(self) -> int:
        return self._api.delivering()


def build_app(store: Store, clock: Clock) -> JiraApp:
    api = build(store, clock)

    async def every(request: Request) -> Response:
        return await api.answer(request)

    methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    return JiraApp(api, Starlette(routes=[Route("/{path:path}", every, methods=methods)]))
