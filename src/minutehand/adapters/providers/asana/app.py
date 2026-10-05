"""The Asana REST API, as an ASGI app over the run's store and clock.

Paths are Asana's with `/api/1.0` already removed by the proxy; `/-/oauth_token`
is outside that prefix and arrives as it is. Every answer is Asana's envelope:
`{"data": ...}` (with `next_page` on a paginated collection), or
`{"errors": [{"message", "help"}]}` with Asana's status code. A single resource is
answered in full, a collection compact, and `opt_fields` narrows either.

Who calls is the user the bearer token maps to: a token the scenario seeded or the
token endpoint minted; any bearer token acts as the agent when the scenario seeded
none. `me` is that user, and what they create is theirs.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from pydantic import JsonValue
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.asana import state, wire
from minutehand.adapters.providers.asana.state import AGENT_GID, AsanaWorld
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

Handler = Callable[[Request, wire.AsanaUser], Awaitable[Response]]

_JSON = "application/json; charset=utf-8"
_SEARCH = (
    "text",
    "completed",
    "assignee.any",
    "assignee.not",
    "projects.any",
    "projects.not",
    "sections.any",
    "tags.any",
    "is_subtask",
    "sort_by",
    "sort_ascending",
    "limit",
    "opt_fields",
    "opt_pretty",
)
_SEARCH_UNSUPPORTED = (
    "projects.all",
    "sections.not",
    "sections.all",
    "tags.not",
    "tags.all",
    "teams.any",
    "followers.any",
    "followers.not",
    "created_by.any",
    "created_by.not",
    "assigned_by.any",
    "assigned_by.not",
    "liked_by.not",
    "commented_on_by.not",
    "portfolios.any",
    "due_on",
    "due_on.before",
    "due_on.after",
    "due_at.before",
    "due_at.after",
    "start_on",
    "start_on.before",
    "start_on.after",
    "created_on",
    "created_on.before",
    "created_on.after",
    "created_at.before",
    "created_at.after",
    "completed_on",
    "completed_on.before",
    "completed_on.after",
    "completed_at.before",
    "completed_at.after",
    "modified_on",
    "modified_on.before",
    "modified_on.after",
    "modified_at.before",
    "modified_at.after",
    "is_blocking",
    "is_blocked",
    "has_attachment",
    "resource_subtype",
)
_CUSTOM_FIELD_SEARCH = "custom_fields."
_SORTS = ("modified_at", "created_at")
_TYPEAHEAD = ("task", "user", "project", "tag")
_NEEDS_FILTER = "Must specify exactly one of project, tag, section, user task list, or assignee + workspace"
_NEEDS_TEAM = (
    "If the workspace for your project is an organization, you must also supply a team to share the project with."
)
_NOT_ORGANIZATION = "organization: Not an organization"


def _answer(body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(body, status_code=status, media_type=_JSON, headers=headers)


def _at(value: str) -> datetime:
    parsed = wire.parse_stamp(value)
    if parsed is None:
        raise ValueError(f"stored asana timestamp {value!r} is not one")
    return parsed


class View:
    """The world as one caller sees it during one request: lookups, refusals, and representations."""

    def __init__(self, world: AsanaWorld, caller: wire.AsanaUser) -> None:
        self.world = world
        self.caller = caller
        self._users: dict[str, wire.UserOut] = {}
        self._projects: dict[str, wire.ProjectOut] = {}
        self._children: dict[str, list[wire.AsanaTask]] | None = None

    # ------------------------------------------------------------------ lookups

    def workspace(self, gid: str, *, status: int = 404, field: str = "workspace") -> wire.AsanaWorkspace:
        found = self.world.workspace(wire.gid_in_path(field, gid))
        if found is None:
            raise wire.unknown(field, gid, status=status)
        return found

    def organization(self, gid: str | None) -> wire.AsanaWorkspace:
        if gid is None:
            raise wire.bad("organization: Missing input")
        found = self.workspace(gid, status=400, field="organization")
        if not found.is_organization:
            raise wire.bad(_NOT_ORGANIZATION)
        return found

    def visible(self, project: wire.AsanaProject) -> bool:
        return not project.private or self.caller.gid in project.members

    def project(self, gid: str, *, field: str = "project", status: int = 404) -> wire.AsanaProject:
        found = self.world.project(wire.gid_in_path(field, gid))
        if found is None:
            raise wire.unknown(field, gid, status=status)
        if not self.visible(found):
            raise wire.forbidden()
        return found

    def sees(self, task: wire.AsanaTask) -> bool:
        """A task is seen through any of its projects, or by its assignee and its creator."""
        if self.caller.gid in (task.assignee, task.created_by):
            return True
        projects = [self.world.project(m.project) for m in task.memberships]
        if not projects:
            parent = self.world.task(task.parent) if task.parent is not None else None
            return parent is None or self.sees(parent)
        return any(p is not None and self.visible(p) for p in projects)

    def task(self, gid: str, *, field: str = "task", status: int = 404) -> wire.AsanaTask:
        found = self.world.task(wire.gid_in_path(field, gid))
        if found is None:
            raise wire.unknown(field, gid, status=status)
        if not self.sees(found):
            raise wire.forbidden()
        return found

    def user(self, identifier: str, *, field: str, status: int) -> wire.AsanaUser:
        if not wire.is_user_identifier(identifier):
            raise wire.bad(f"{field}: Not a Recognized ID")
        found = self.world.resolve_user(identifier, me=self.caller.gid)
        if found is None or found.removed:
            raise wire.unknown(field, identifier, status=status)
        return found

    def team(self, gid: str, *, field: str = "team", status: int = 404) -> wire.AsanaTeam:
        found = self.world.team(wire.gid_in_path(field, gid))
        if found is None:
            raise wire.unknown(field, gid, status=status)
        return found

    def section(self, gid: str, *, field: str = "section", status: int = 404) -> wire.AsanaSection:
        found = self.world.section(wire.gid_in_path(field, gid))
        if found is None:
            raise wire.unknown(field, gid, status=status)
        self.project(found.project)
        return found

    def tag(self, gid: str, *, field: str = "tag", status: int = 404) -> wire.AsanaTag:
        found = self.world.tag(wire.gid_in_path(field, gid))
        if found is None:
            raise wire.unknown(field, gid, status=status)
        return found

    def custom_field(self, gid: str, *, field: str = "custom_field", status: int = 404) -> wire.AsanaCustomField:
        found = self.world.custom_field(wire.gid_in_path(field, gid))
        if found is None:
            raise wire.unknown(field, gid, status=status)
        return found

    def user_ids(self) -> dict[str, str]:
        """Every identifier a write may name a user by (gid, email, `me`) to its gid."""
        found: dict[str, str] = {"me": self.caller.gid}
        for user in self.world.users():
            found[user.gid] = user.gid
            found[user.email] = user.gid
        return found

    # ------------------------------------------------------------------ representations

    def workspace_out(self, workspace: wire.AsanaWorkspace) -> wire.WorkspaceOut:
        return wire.WorkspaceOut(
            gid=workspace.gid,
            name=workspace.name,
            is_organization=workspace.is_organization,
            email_domains=workspace.email_domains,
        )

    def workspace_of(self, gid: str) -> wire.WorkspaceOut:
        return self.workspace_out(_held(self.world.workspace(gid), gid))

    def user_out(self, user: wire.AsanaUser) -> wire.UserOut:
        if user.gid not in self._users:
            self._users[user.gid] = wire.UserOut(
                gid=user.gid,
                name=user.name,
                email=user.email,
                workspaces=[self.workspace_out(w) for w in self.world.workspaces()],
            )
        return self._users[user.gid]

    def user_of(self, gid: str) -> wire.UserOut:
        if gid in self._users:
            return self._users[gid]
        return self.user_out(_held(self.world.user(gid), gid))

    def team_out(self, team: wire.AsanaTeam) -> wire.TeamOut:
        return wire.TeamOut(
            gid=team.gid,
            name=team.name,
            organization=self.workspace_of(team.workspace),
            permalink_url=f"https://app.asana.com/0/{team.gid}/overview",
        )

    def project_out(self, project: wire.AsanaProject) -> wire.ProjectOut:
        if project.gid not in self._projects:
            team = self.world.team(project.team) if project.team is not None else None
            self._projects[project.gid] = wire.ProjectOut(
                gid=project.gid,
                name=project.name,
                notes=project.notes,
                archived=project.archived,
                public=not project.private,
                created_at=project.created_at,
                workspace=self.workspace_of(project.workspace),
                team=self.team_out(team) if team is not None else None,
                members=[self.user_of(m) for m in project.members],
                permalink_url=f"https://app.asana.com/0/{project.gid}/list",
            )
        return self._projects[project.gid]

    def project_of(self, gid: str) -> wire.ProjectOut:
        if gid in self._projects:
            return self._projects[gid]
        return self.project_out(_held(self.world.project(gid), gid))

    def section_out(self, section: wire.AsanaSection) -> wire.SectionOut:
        return wire.SectionOut(
            gid=section.gid, name=section.name, created_at=section.created_at, project=self.project_of(section.project)
        )

    def field_out(self, field: wire.AsanaCustomField) -> wire.CustomFieldOut:
        takes = field.subtype in ("enum", "multi_enum")
        return wire.CustomFieldOut(
            gid=field.gid,
            name=field.name,
            resource_subtype=field.subtype,
            type=field.subtype,
            enum_options=[_option_out(o) for o in field.enum_options] if takes else None,
            precision=field.precision if field.subtype == "number" else None,  # enum-lint: exempt Asana subtype
        )

    def setting_out(self, project: wire.AsanaProject, field: wire.AsanaCustomField) -> wire.CustomFieldSettingOut:
        out = self.project_out(project)
        return wire.CustomFieldSettingOut(
            gid=state.setting_gid(project.gid, field.gid),
            custom_field=self.field_out(field),
            project=out,
            parent=out,
        )

    def membership_out(self, project: wire.AsanaProject, user: str) -> wire.ProjectMembershipOut:
        out = self.project_out(project)
        member = self.user_of(user)
        return wire.ProjectMembershipOut(
            gid=state.membership_gid(project.gid, user), user=member, member=member, project=out, parent=out
        )

    def tag_out(self, tag: wire.AsanaTag) -> wire.TagOut:
        return wire.TagOut(
            gid=tag.gid,
            name=tag.name,
            color=tag.color,
            created_at=tag.created_at,
            workspace=self.workspace_of(tag.workspace),
            permalink_url=f"https://app.asana.com/0/{tag.gid}/list",
        )

    def value_out(self, field: wire.AsanaCustomField, value: wire.AsanaFieldValue | None) -> wire.CustomFieldValueOut:
        """A field as a task carries it, set or not: the member for its kind and `display_value` say the value."""
        out = wire.CustomFieldValueOut(
            gid=field.gid,
            name=field.name,
            resource_subtype=field.subtype,
            type=field.subtype,
            display_value=None,
        )
        options = {o.gid: o for o in field.enum_options}
        match field.subtype:
            case "enum":
                chosen = options[value.option] if value is not None and value.option is not None else None
                return out.model_copy(
                    update={
                        "enum_options": [_option_out(o) for o in field.enum_options],
                        "enum_value": _option_out(chosen) if chosen is not None else None,
                        "display_value": chosen.name if chosen is not None else None,
                    }
                )
            case "multi_enum":
                chosen_many = [options[g] for g in value.options] if value is not None else []
                return out.model_copy(
                    update={
                        "enum_options": [_option_out(o) for o in field.enum_options],
                        "multi_enum_values": [_option_out(o) for o in chosen_many],
                        "display_value": ", ".join(o.name for o in chosen_many) or None,
                    }
                )
            case "text":
                text = value.text if value is not None else None
                return out.model_copy(update={"text_value": text, "display_value": text})
            case "number":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
                number = value.number if value is not None else None
                return out.model_copy(
                    update={
                        "number_value": number,
                        "precision": field.precision,
                        "display_value": wire.display_number(number, field.precision) if number is not None else None,
                    }
                )
            case "date":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
                on = value.date if value is not None else None
                at = value.date_time if value is not None else None
                return out.model_copy(
                    update={
                        "date_value": wire.DateValueOut(date=on, date_time=at) if on is not None else None,
                        "display_value": at or on,
                    }
                )
            case "people":  # enum-lint: exempt Asana's custom field resource_subtype, its wire vocabulary
                people = [self.user_of(g) for g in value.people] if value is not None else []
                return out.model_copy(
                    update={"people_value": people, "display_value": ", ".join(p.name for p in people) or None}
                )

    def _ref_out(self, task: wire.AsanaTask) -> wire.TaskRefOut:
        return wire.TaskRefOut(gid=task.gid, name=task.name, completed=task.completed, permalink_url=_permalink(task))

    def children(self, parent: str) -> list[wire.AsanaTask]:
        if self._children is None:
            self._children = {}
            for task in self.world.tasks():
                if task.parent is not None:
                    self._children.setdefault(task.parent, []).append(task)
        return self._children[parent] if parent in self._children else []

    def task_out(self, task: wire.AsanaTask) -> wire.TaskOut:
        parent = self.world.task(task.parent) if task.parent is not None else None
        subtasks = self.children(task.gid)
        values = {v.field: v for v in task.custom_fields}
        fields = [_held(self.world.custom_field(f), f) for f in self.world.fields_of(task)]
        return wire.TaskOut(
            gid=task.gid,
            name=task.name,
            notes=task.notes,
            html_notes=wire.html_notes(task.notes),
            completed=task.completed,
            completed_at=task.completed_at,
            due_on=task.due_on,
            due_at=task.due_at,
            created_at=task.created_at,
            modified_at=task.modified_at,
            assignee=self.user_of(task.assignee) if task.assignee is not None else None,
            created_by=self.user_of(task.created_by),
            parent=self._ref_out(parent) if parent is not None else None,
            subtasks=[self._ref_out(t) for t in subtasks],
            num_subtasks=len(subtasks),
            memberships=[
                wire.MembershipOut(
                    project=self.project_of(m.project),
                    section=self.section_out(_held(self.world.section(m.section), m.section)),
                )
                for m in task.memberships
            ],
            projects=[self.project_of(m.project) for m in task.memberships],
            tags=[self.tag_out(_held(self.world.tag(t), t)) for t in task.tags],
            custom_fields=[self.value_out(f, values[f.gid] if f.gid in values else None) for f in fields],
            workspace=self.workspace_of(task.workspace),
            permalink_url=_permalink(task),
        )

    def story_out(self, story: wire.AsanaStory) -> wire.StoryOut:
        task = self.world.task(story.task)
        return wire.StoryOut(
            gid=story.gid,
            text=story.text,
            html_text=wire.html_notes(story.text),
            created_at=story.created_at,
            created_by=self.user_of(story.created_by),
            target=wire.Compact(gid=story.task, resource_type="task", name=task.name if task is not None else ""),
        )


def _option_out(option: wire.AsanaEnumOption) -> wire.EnumOptionOut:
    return wire.EnumOptionOut(gid=option.gid, name=option.name, enabled=option.enabled, color=option.color)


def _permalink(task: wire.AsanaTask) -> str:
    first = task.memberships[0].project if task.memberships else "0"
    return f"https://app.asana.com/0/{first}/{task.gid}"


def _held[Found](found: Found | None, gid: str) -> Found:
    """Something a stored record refers to; its absence is a broken world, not a refusal."""
    if found is None:
        raise LookupError(f"asana gid {gid} is referenced and names nothing in the world")
    return found


def _query(request: Request) -> wire.Query:
    return wire.Query(list(request.query_params.multi_items()))


def _one(request: Request, item: wire.Representation, status: int = 200) -> Response:
    return _answer(wire.one(item, wire.field_tree(_query(request))), status)


def _completed_since(query: wire.Query, tasks: list[wire.AsanaTask]) -> list[wire.AsanaTask]:
    """`completed_since=now` keeps open tasks; a moment keeps open tasks and those completed since."""
    value = query.text("completed_since")
    if value is None:
        return tasks
    if value == "now":
        return [t for t in tasks if not t.completed]
    since = query.moment("completed_since")
    assert since is not None
    return [t for t in tasks if not t.completed or (t.completed_at is not None and _at(t.completed_at) >= since)]


def _due_on(due_on: str | None, due_at: str | None) -> str | None:
    """A due time also answers its date, in UTC."""
    if due_at is None:
        return due_on
    return _at(due_at).date().isoformat()


class AsanaApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = AsanaWorld(store)
        self._clock = clock

    def _now(self) -> str:
        return wire.stamp(self._clock.now())

    def _caller(self, request: Request) -> wire.AsanaUser:
        authorization = request.headers["authorization"] if "authorization" in request.headers else ""
        scheme, _, token = authorization.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise wire.Refusal(401, "Not Authorized")
        home = self._world.home()
        credential = self._world.credential(token)
        if credential is None or credential.kind is not wire.CredentialKind.ACCESS:
            if home.strict_tokens:
                raise wire.Refusal(401, "Not Authorized")
            return _held(self._world.user(AGENT_GID), AGENT_GID)
        if credential.expires_at is not None and self._clock.now() >= _at(credential.expires_at):
            raise wire.Refusal(
                401, "The bearer token has expired. If you have a refresh token, use it to get a new one."
            )
        user = _held(self._world.user(credential.user), credential.user)
        if user.removed:
            raise wire.Refusal(401, "Not Authorized")
        return user

    def _listed[Out: wire.Representation](self, request: Request, items: list[Out]) -> Response:
        """One page of a collection, by the workspace's own threshold for a read without `limit`."""
        query = _query(request)
        limit = self._world.home().unpaginated_limit
        chosen, next_page = wire.page(items, query, request.url.path, unpaginated_limit=limit)
        return _answer(wire.many(chosen, wire.field_tree(query), next_page))

    def _throttle(self) -> None:
        now = self._clock.now()
        for window in self._world.home().rate_limits:
            if _at(window.start) <= now < _at(window.end):
                wait = math.ceil((_at(window.end) - now) / timedelta(seconds=1))
                raise wire.Refusal(429, wire.RATE_LIMITED, retry_after=max(wait, 1))

    def guarded(self, handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        """Authenticate, throttle, then answer; a refusal becomes Asana's error envelope and records nothing."""

        async def endpoint(request: Request) -> Response:
            try:
                caller = self._caller(request)
                self._throttle()
                return await handler(request, caller)
            except wire.Refusal as refusal:
                headers = {"Retry-After": str(refusal.retry_after)} if refusal.retry_after is not None else None
                return _answer(wire.failed(refusal.message), refusal.status, headers)

        return endpoint

    def _view(self, caller: wire.AsanaUser) -> View:
        return View(self._world, caller)

    # ------------------------------------------------------------------ sign-in

    async def oauth_token(self, request: Request) -> Response:
        """A refresh: the refresh token the scenario seeded buys a new access token for its user, valid for an
        hour of the run's time. The code grant needs a browser and is not served."""
        try:
            grant = wire.token_grant(await request.body())
            if grant.grant_type != "refresh_token":
                raise wire.OAuthRefusal(
                    "unsupported_grant_type", f"The grant type {grant.grant_type} is not served by this simulation."
                )
            refresh = self._world.credential(grant.refresh_token or "")
            if refresh is None or refresh.kind is not wire.CredentialKind.REFRESH:
                raise wire.OAuthRefusal("invalid_grant", "The refresh token is invalid or has been revoked.")
        except wire.OAuthRefusal as refusal:
            return _answer(wire.oauth_failed(refusal), 400)
        user = _held(self._world.user(refresh.user), refresh.user)
        token = f"1/{user.gid}:{self._world.next_gid()}"
        expires = self._clock.now() + timedelta(seconds=wire.TOKEN_LIFETIME_SECONDS)
        self._world.put_record(
            wire.AsanaCredential(
                gid=state.credential_gid(token),
                kind=wire.CredentialKind.ACCESS,
                user=user.gid,
                expires_at=wire.stamp(expires),
            ),
            parent=state.CREDENTIALS,
            actor=Actor.AGENT,
        )
        return _answer(wire.token_answer(token, user))

    # ------------------------------------------------------------------ users, workspaces, teams

    async def me(self, request: Request, caller: wire.AsanaUser) -> Response:
        self._world.saw(state.record_ref(caller.gid), Operation.READ)
        return _one(request, self._view(caller).user_out(caller))

    async def users(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        query = _query(request)
        workspace = query.text("workspace")
        if workspace is not None:
            view.workspace(workspace, status=400)
        team = query.text("team")
        found = self._world.users()
        if team is not None:
            members = view.team(team, status=400).members
            found = [u for u in found if u.gid in members]
        self._world.saw(state.record_ref(state.WORKSPACE_GID), Operation.SEARCH)
        return self._listed(request, [view.user_out(u) for u in found])

    async def user(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        user = view.user(request.path_params["gid"], field="user", status=404)
        self._world.saw(state.record_ref(user.gid), Operation.READ)
        return _one(request, view.user_out(user))

    async def user_teams(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        user = view.user(request.path_params["gid"], field="user", status=404)
        organization = view.organization(_query(request).text("organization"))
        found = [t for t in self._world.teams() if t.workspace == organization.gid and user.gid in t.members]
        self._world.saw(state.record_ref(organization.gid), Operation.SEARCH)
        return self._listed(request, [view.team_out(t) for t in found])

    async def workspaces(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        self._world.saw(state.record_ref(state.WORKSPACE_GID), Operation.SEARCH)
        return self._listed(request, [view.workspace_out(w) for w in self._world.workspaces()])

    async def workspace(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        workspace = view.workspace(request.path_params["gid"])
        self._world.saw(state.record_ref(workspace.gid), Operation.READ)
        return _one(request, view.workspace_out(workspace))

    async def workspace_users(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        workspace = view.workspace(request.path_params["gid"])
        self._world.saw(state.record_ref(workspace.gid), Operation.SEARCH)
        return self._listed(request, [view.user_out(u) for u in self._world.users()])

    async def workspace_teams(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        workspace = view.workspace(request.path_params["gid"])
        if not workspace.is_organization:
            raise wire.bad(_NOT_ORGANIZATION)
        self._world.saw(state.record_ref(workspace.gid), Operation.SEARCH)
        return self._listed(request, [view.team_out(t) for t in self._world.teams() if t.workspace == workspace.gid])

    async def team(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        team = view.team(request.path_params["gid"])
        self._world.saw(state.record_ref(team.gid), Operation.READ)
        return _one(request, view.team_out(team))

    async def team_users(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        team = view.team(request.path_params["gid"])
        self._world.saw(state.record_ref(team.gid), Operation.SEARCH)
        return self._listed(request, [view.user_out(u) for u in self._world.users() if u.gid in team.members])

    # ------------------------------------------------------------------ projects

    def _projects(self, request: Request, view: View, workspace: str | None, team: str | None) -> Response:
        archived = _query(request).boolean("archived")
        found = [
            p
            for p in self._world.projects()
            if (workspace is None or p.workspace == workspace)
            and (team is None or p.team == team)
            and (archived is None or p.archived == archived)
            and view.visible(p)
        ]
        self._world.saw(state.record_ref(team or workspace or state.WORKSPACE_GID), Operation.SEARCH)
        return self._listed(request, [view.project_out(p) for p in found])

    async def workspace_projects(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        workspace = view.workspace(request.path_params["gid"])
        team = _query(request).text("team")
        return self._projects(request, view, workspace.gid, view.team(team, status=400).gid if team else None)

    async def team_projects(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        return self._projects(request, view, None, view.team(request.path_params["gid"]).gid)

    async def projects(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        query = _query(request)
        workspace, team = query.text("workspace"), query.text("team")
        return self._projects(
            request,
            view,
            view.workspace(workspace, status=400).gid if workspace is not None else None,
            view.team(team, status=400).gid if team is not None else None,
        )

    async def _create_project(
        self, request: Request, caller: wire.AsanaUser, *, workspace: str | None, team: str | None
    ) -> Response:
        """A new project: its creator its only member, one `Untitled section`, and no custom fields."""
        view = self._view(caller)
        sent = wire.project_create(wire.envelope(await request.body()))
        team_gid = team or sent.team
        chosen_team = view.team(team_gid, status=400) if team_gid is not None else None
        workspace_gid = workspace or sent.workspace or (chosen_team.workspace if chosen_team is not None else None)
        if workspace_gid is None:
            raise wire.bad("workspace: Missing input")
        home = view.workspace(workspace_gid, status=400)
        if chosen_team is not None and chosen_team.workspace != home.gid:
            raise wire.bad("team: Must be in the same workspace as the project")
        if home.is_organization and chosen_team is None:
            raise wire.bad(_NEEDS_TEAM)
        if chosen_team is not None and caller.gid not in chosen_team.members:
            raise wire.forbidden()
        now = self._now()
        project = wire.AsanaProject(
            gid=self._world.next_gid(),
            name=sent.name,
            notes=sent.notes,
            archived=sent.archived,
            workspace=home.gid,
            team=chosen_team.gid if chosen_team is not None else None,
            members=[caller.gid],
            created_at=now,
        )
        self._world.put_record(project, parent=state.PROJECTS, actor=Actor.AGENT)
        self._world.put_record(
            wire.AsanaSection(gid=self._world.next_gid(), name=state.NEW_SECTION, project=project.gid, created_at=now),
            parent=project.gid,
            actor=Actor.AGENT,
        )
        return _one(request, view.project_out(project), 201)

    async def create_project(self, request: Request, caller: wire.AsanaUser) -> Response:
        return await self._create_project(request, caller, workspace=None, team=None)

    async def create_workspace_project(self, request: Request, caller: wire.AsanaUser) -> Response:
        workspace = self._view(caller).workspace(request.path_params["gid"]).gid
        return await self._create_project(request, caller, workspace=workspace, team=None)

    async def create_team_project(self, request: Request, caller: wire.AsanaUser) -> Response:
        team = self._view(caller).team(request.path_params["gid"]).gid
        return await self._create_project(request, caller, workspace=None, team=team)

    async def project(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        project = view.project(request.path_params["gid"])
        self._world.saw(state.record_ref(project.gid), Operation.READ)
        return _one(request, view.project_out(project))

    async def project_sections(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        project = view.project(request.path_params["gid"])
        self._world.saw(state.record_ref(project.gid), Operation.SEARCH)
        return self._listed(request, [view.section_out(s) for s in self._world.sections(project.gid)])

    async def create_section(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        project = view.project(request.path_params["gid"])
        sent = wire.section_create(wire.envelope(await request.body()))
        section = wire.AsanaSection(
            gid=self._world.next_gid(), name=sent.name, project=project.gid, created_at=self._now()
        )
        self._world.put_record(section, parent=project.gid, actor=Actor.AGENT)
        return _one(request, view.section_out(section), 201)

    async def project_tasks(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        project = view.project(request.path_params["gid"])
        found = _completed_since(_query(request), self._world.project_tasks(project.gid))
        self._world.saw(state.record_ref(project.gid), Operation.SEARCH)
        return self._listed(request, [view.task_out(t) for t in found])

    async def project_memberships(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        project = view.project(request.path_params["gid"])
        wanted = _query(request).text("user")
        members = project.members
        if wanted is not None:
            members = [m for m in members if m == view.user(wanted, field="user", status=400).gid]
        self._world.saw(state.record_ref(project.gid), Operation.SEARCH)
        found = [view.membership_out(project, m) for m in members]
        return self._listed(request, sorted(found, key=lambda m: m.gid))

    async def _members(self, request: Request, caller: wire.AsanaUser, *, adding: bool) -> Response:
        view = self._view(caller)
        project = view.project(request.path_params["gid"])
        named = wire.members_in(wire.envelope(await request.body()))
        users = [view.user(n, field="members", status=400).gid for n in named]
        if adding:
            members = [*project.members, *(u for u in dict.fromkeys(users) if u not in project.members)]
        else:
            members = [m for m in project.members if m not in users]
        changed = project.model_copy(update={"members": members})
        self._world.put_record(changed, parent=state.PROJECTS, actor=Actor.AGENT, operation=Operation.UPDATE)
        return _one(request, self._view(caller).project_out(changed))

    async def add_members(self, request: Request, caller: wire.AsanaUser) -> Response:
        return await self._members(request, caller, adding=True)

    async def remove_members(self, request: Request, caller: wire.AsanaUser) -> Response:
        return await self._members(request, caller, adding=False)

    async def custom_field_settings(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        project = view.project(request.path_params["gid"])
        self._world.saw(state.record_ref(project.gid), Operation.SEARCH)
        found = [view.setting_out(project, _held(self._world.custom_field(f), f)) for f in project.custom_fields]
        return self._listed(request, sorted(found, key=lambda s: s.gid))

    async def add_custom_field_setting(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        project = view.project(request.path_params["gid"])
        gid = wire.one_gid(wire.envelope(await request.body()), "custom_field")
        if not self._world.home().premium:
            raise wire.premium(wire.FIELDS_ARE_PREMIUM)
        field = view.custom_field(gid, status=400)
        if field.gid in project.custom_fields:
            raise wire.bad(f"custom_field: Custom field {field.gid} is already applied to this project")
        changed = project.model_copy(update={"custom_fields": [*project.custom_fields, field.gid]})
        self._world.put_record(changed, parent=state.PROJECTS, actor=Actor.AGENT, operation=Operation.UPDATE)
        return _one(request, view.setting_out(changed, field))

    async def remove_custom_field_setting(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        project = view.project(request.path_params["gid"])
        field = view.custom_field(wire.one_gid(wire.envelope(await request.body()), "custom_field"), status=400)
        if field.gid not in project.custom_fields:
            raise wire.bad(f"custom_field: Custom field {field.gid} is not on this project")
        changed = project.model_copy(update={"custom_fields": [f for f in project.custom_fields if f != field.gid]})
        self._world.put_record(changed, parent=state.PROJECTS, actor=Actor.AGENT, operation=Operation.UPDATE)
        return _answer(wire.empty())

    # ------------------------------------------------------------------ sections

    async def section(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        section = view.section(request.path_params["gid"])
        self._world.saw(state.record_ref(section.gid), Operation.READ)
        return _one(request, view.section_out(section))

    async def section_tasks(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        section = view.section(request.path_params["gid"])
        found = [t for t in self._world.tasks() if any(m.section == section.gid for m in t.memberships)]
        self._world.saw(state.record_ref(section.project), Operation.SEARCH)
        return self._listed(request, [view.task_out(t) for t in _completed_since(_query(request), found)])

    async def add_task_to_section(self, request: Request, caller: wire.AsanaUser) -> Response:
        """Move the task to the section within the section's project; a task not in that project joins it."""
        view = self._view(caller)
        section = view.section(request.path_params["gid"])
        task = view.task(wire.one_gid(wire.envelope(await request.body()), "task"), status=400)
        placed = wire.AsanaMembership(project=section.project, section=section.gid)
        if any(m.project == section.project for m in task.memberships):
            memberships = [placed if m.project == section.project else m for m in task.memberships]
        else:
            memberships = [*task.memberships, placed]
        moved = task.model_copy(update={"memberships": memberships, "modified_at": self._now()})
        self._world.put_task(moved, operation=Operation.UPDATE, actor=Actor.AGENT)
        return _answer(wire.empty())

    # ------------------------------------------------------------------ custom fields, tags

    async def workspace_custom_fields(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        workspace = view.workspace(request.path_params["gid"])
        self._world.saw(state.record_ref(workspace.gid), Operation.SEARCH)
        found = [view.field_out(f) for f in self._world.custom_fields() if f.workspace == workspace.gid]
        return self._listed(request, found)

    async def custom_field(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        field = view.custom_field(request.path_params["gid"])
        self._world.saw(state.record_ref(field.gid), Operation.READ)
        return _one(request, view.field_out(field))

    def _tags(self, request: Request, view: View, workspace: str | None) -> Response:
        self._world.saw(state.record_ref(workspace or state.WORKSPACE_GID), Operation.SEARCH)
        found = [view.tag_out(t) for t in self._world.tags() if workspace is None or t.workspace == workspace]
        return self._listed(request, found)

    async def tags(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        workspace = _query(request).text("workspace")
        return self._tags(request, view, view.workspace(workspace, status=400).gid if workspace is not None else None)

    async def workspace_tags(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        return self._tags(request, view, view.workspace(request.path_params["gid"]).gid)

    async def _create_tag(self, request: Request, caller: wire.AsanaUser, *, workspace: str | None) -> Response:
        view = self._view(caller)
        sent = wire.tag_create(wire.envelope(await request.body()))
        named = workspace or sent.workspace
        if named is None:
            raise wire.bad("workspace: Missing input")
        tag = wire.AsanaTag(
            gid=self._world.next_gid(),
            name=sent.name,
            color=sent.color,
            workspace=view.workspace(named, status=400).gid,
            created_at=self._now(),
        )
        self._world.put_record(tag, parent=state.TAGS, actor=Actor.AGENT)
        return _one(request, view.tag_out(tag), 201)

    async def create_tag(self, request: Request, caller: wire.AsanaUser) -> Response:
        return await self._create_tag(request, caller, workspace=None)

    async def create_workspace_tag(self, request: Request, caller: wire.AsanaUser) -> Response:
        workspace = self._view(caller).workspace(request.path_params["gid"]).gid
        return await self._create_tag(request, caller, workspace=workspace)

    async def tag(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        tag = view.tag(request.path_params["gid"])
        self._world.saw(state.record_ref(tag.gid), Operation.READ)
        return _one(request, view.tag_out(tag))

    async def tag_tasks(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        tag = view.tag(request.path_params["gid"])
        found = [t for t in self._world.tasks() if tag.gid in t.tags and view.sees(t)]
        self._world.saw(state.record_ref(tag.gid), Operation.SEARCH)
        return self._listed(request, [view.task_out(t) for t in found])

    async def task_tags(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        task = view.task(request.path_params["gid"])
        self._world.saw(state.task_ref(task.gid), Operation.READ)
        return self._listed(request, [view.tag_out(_held(self._world.tag(t), t)) for t in task.tags])

    async def _tagged(self, request: Request, caller: wire.AsanaUser, *, adding: bool) -> Response:
        view = self._view(caller)
        task = view.task(request.path_params["gid"])
        tag = view.tag(wire.one_gid(wire.envelope(await request.body()), "tag"), status=400)
        tags = [*task.tags, tag.gid] if adding and tag.gid not in task.tags else task.tags
        if not adding:
            tags = [t for t in task.tags if t != tag.gid]
        changed = task.model_copy(update={"tags": tags, "modified_at": self._now()})
        self._world.put_task(changed, operation=Operation.UPDATE, actor=Actor.AGENT)
        return _answer(wire.empty())

    async def add_tag(self, request: Request, caller: wire.AsanaUser) -> Response:
        return await self._tagged(request, caller, adding=True)

    async def remove_tag(self, request: Request, caller: wire.AsanaUser) -> Response:
        return await self._tagged(request, caller, adding=False)

    # ------------------------------------------------------------------ tasks

    async def list_tasks(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        query = _query(request)
        query.refuse(["user_task_list"])
        project, section, tag = query.text("project"), query.text("section"), query.text("tag")
        assignee, workspace = query.text("assignee"), query.text("workspace")
        chosen = [
            project is not None,
            section is not None,
            tag is not None,
            assignee is not None and workspace is not None,
        ]
        if sum(chosen) != 1 or (assignee is None) != (workspace is None):
            raise wire.bad(_NEEDS_FILTER)
        if project is not None:
            found = self._world.project_tasks(view.project(project, status=400).gid)
            seen = project
        elif section is not None:
            wanted = view.section(section, status=400)
            found = [t for t in self._world.tasks() if any(m.section == wanted.gid for m in t.memberships)]
            seen = wanted.project
        elif tag is not None:
            labelled = view.tag(tag, status=400)
            found = [t for t in self._world.tasks() if labelled.gid in t.tags]
            seen = labelled.gid
        else:
            assert assignee is not None and workspace is not None
            ws = view.workspace(workspace, status=400)
            user = view.user(assignee, field="assignee", status=400)
            found = [t for t in self._world.tasks() if t.assignee == user.gid and t.workspace == ws.gid]
            seen = ws.gid
        found = [t for t in _completed_since(query, found) if view.sees(t)]
        since = query.moment("modified_since")
        if since is not None:
            found = [t for t in found if _at(t.modified_at) >= since]
        self._world.saw(state.record_ref(seen), Operation.SEARCH)
        return self._listed(request, [view.task_out(t) for t in found])

    def _values(self, view: View, task: wire.AsanaTask, sent: dict[str, JsonValue]) -> list[wire.AsanaFieldValue]:
        """The task's custom field values with what was sent written over them; each sent field must be one the
        task carries through its projects."""
        if sent and not self._world.home().premium:
            raise wire.premium(wire.FIELDS_ARE_PREMIUM)
        carried = self._world.fields_of(task)
        users = view.user_ids()
        written = {v.field: v for v in task.custom_fields}
        for gid, value in sent.items():
            field = view.custom_field(gid, field="custom_fields", status=400)
            if field.gid not in carried:
                raise wire.bad(f"custom_fields: Custom field {field.gid} is not on given task")
            written[field.gid] = wire.custom_field_value(field, value, users)
        return list(written.values())

    def _placed(self, view: View, sent: wire.TaskCreate) -> list[wire.AsanaMembership]:
        """Where a new task lands: each named project's named section, or its first."""
        named = [wire.MembershipIn(project=p) for p in dict.fromkeys(sent.projects)] or sent.memberships
        placed: list[wire.AsanaMembership] = []
        for membership in named:
            field = "projects" if sent.projects else "memberships.project"
            project = view.project(membership.project, field=field, status=400)
            if membership.section is not None:
                section = view.section(membership.section, field="memberships.section", status=400)
                if section.project != project.gid:
                    raise wire.bad("memberships.section: Must be a section of the membership's project")
                placed.append(wire.AsanaMembership(project=project.gid, section=section.gid))
                continue
            sections = self._world.sections(project.gid)
            if not sections:
                raise LookupError(f"asana project {project.gid} has no section for a new task to land in")
            placed.append(wire.AsanaMembership(project=project.gid, section=sections[0].gid))
        return placed

    async def _create_task(self, request: Request, caller: wire.AsanaUser, *, parent: str | None) -> Response:
        view = self._view(caller)
        sent = wire.task_create(wire.envelope(await request.body()), parent=parent)
        memberships = self._placed(view, sent)
        under = view.task(sent.parent, field="parent", status=400) if sent.parent is not None else None
        projects = [_held(self._world.project(m.project), m.project) for m in memberships]
        if sent.workspace is not None:
            workspace = view.workspace(sent.workspace, status=400).gid
            if any(p.workspace != workspace for p in projects):
                raise wire.bad("projects: Must be in the same workspace as the task")
        elif projects:
            workspace = projects[0].workspace
        else:
            assert under is not None
            workspace = under.workspace
        assignee = view.user(sent.assignee, field="assignee", status=400) if sent.assignee is not None else None
        tags = [view.tag(t, field="tags", status=400).gid for t in dict.fromkeys(sent.tags)]
        now = self._now()
        task = wire.AsanaTask(
            gid=self._world.next_gid(),
            name=sent.name,
            notes=sent.notes,
            completed=sent.completed,
            completed_at=now if sent.completed else None,
            due_on=_due_on(sent.due_on, sent.due_at),
            due_at=sent.due_at,
            assignee=assignee.gid if assignee is not None else None,
            created_by=caller.gid,
            workspace=workspace,
            parent=under.gid if under is not None else None,
            memberships=memberships,
            tags=tags,
            created_at=now,
            modified_at=now,
        )
        task = task.model_copy(update={"custom_fields": self._values(view, task, sent.custom_fields)})
        self._world.put_task(task, operation=Operation.CREATE, actor=Actor.AGENT)
        return _one(request, self._view(caller).task_out(task), 201)

    async def create_task(self, request: Request, caller: wire.AsanaUser) -> Response:
        return await self._create_task(request, caller, parent=None)

    async def create_subtask(self, request: Request, caller: wire.AsanaUser) -> Response:
        parent = self._view(caller).task(request.path_params["gid"]).gid
        return await self._create_task(request, caller, parent=parent)

    async def subtasks(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        task = view.task(request.path_params["gid"])
        self._world.saw(state.task_ref(task.gid), Operation.READ)
        return self._listed(request, [view.task_out(t) for t in self._world.subtasks(task.gid)])

    async def get_task(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        task = view.task(request.path_params["gid"])
        self._world.saw(state.task_ref(task.gid), Operation.READ)
        return _one(request, view.task_out(task))

    async def update_task(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        task = view.task(request.path_params["gid"])
        sent = wire.task_update(wire.envelope(await request.body()))
        given = sent.model_fields_set
        now = self._now()
        update: dict[str, object] = {"modified_at": now}
        if "name" in given:
            update["name"] = sent.name
        if "notes" in given:
            update["notes"] = sent.notes
        if "completed" in given:
            update["completed"] = sent.completed
            update["completed_at"] = (task.completed_at if task.completed else now) if sent.completed else None
        if "due_on" in given:
            update["due_on"] = sent.due_on
            update["due_at"] = None
        if "due_at" in given:
            update["due_at"] = sent.due_at
            update["due_on"] = _due_on(None, sent.due_at)
        if "assignee" in given:
            user = view.user(sent.assignee, field="assignee", status=400) if sent.assignee is not None else None
            update["assignee"] = user.gid if user is not None else None
        if "custom_fields" in given:
            update["custom_fields"] = self._values(view, task, sent.custom_fields)
        changed = task.model_copy(update=update)
        self._world.put_task(changed, operation=Operation.UPDATE, actor=Actor.AGENT)
        return _one(request, self._view(caller).task_out(changed))

    async def set_parent(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        task = view.task(request.path_params["gid"])
        sent = wire.parent_in(wire.envelope(await request.body()))
        if sent.parent is not None:
            parent = view.task(sent.parent, field="parent", status=400)
            ancestor: wire.AsanaTask | None = parent
            while ancestor is not None:
                if ancestor.gid == task.gid:
                    raise wire.bad("parent: A task cannot be a subtask of itself or of its own subtask")
                ancestor = self._world.task(ancestor.parent) if ancestor.parent is not None else None
        changed = task.model_copy(update={"parent": sent.parent, "modified_at": self._now()})
        self._world.put_task(changed, operation=Operation.UPDATE, actor=Actor.AGENT)
        return _one(request, self._view(caller).task_out(changed))

    async def delete_task(self, request: Request, caller: wire.AsanaUser) -> Response:
        task = self._view(caller).task(request.path_params["gid"])
        self._world.delete_task(task, actor=Actor.AGENT)
        return _answer(wire.empty())

    # ------------------------------------------------------------------ stories

    async def create_story(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        task = view.task(request.path_params["gid"])
        sent = wire.story_create(wire.envelope(await request.body()))
        story = wire.AsanaStory(
            gid=self._world.next_gid(), text=sent.text, task=task.gid, created_by=caller.gid, created_at=self._now()
        )
        self._world.put_story(story, actor=Actor.AGENT)
        return _one(request, view.story_out(story), 201)

    async def stories(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        task = view.task(request.path_params["gid"])
        self._world.saw(state.task_ref(task.gid), Operation.READ)
        return self._listed(request, [view.story_out(s) for s in self._world.stories(task.gid)])

    # ------------------------------------------------------------------ search, typeahead

    async def search(self, request: Request, caller: wire.AsanaUser) -> Response:
        """Advanced search: premium only, at most `limit` tasks, newest change first, and no `next_page`."""
        view = self._view(caller)
        workspace = view.workspace(request.path_params["gid"])
        if not self._world.home().premium:
            raise wire.premium(wire.SEARCH_IS_PREMIUM)
        query = _query(request)
        for name in query.names():
            if name in _SEARCH_UNSUPPORTED or name.startswith(_CUSTOM_FIELD_SEARCH):
                raise wire.unsupported(name)
            if name not in _SEARCH:
                raise wire.bad(f"{name}: Unrecognized parameter")
        limit = query.count("limit") or wire.SEARCH_DEFAULT
        sort_by = query.text("sort_by") or "modified_at"
        if sort_by not in _SORTS:
            raise wire.unsupported(f"sort_by={sort_by}")
        ascending = bool(query.boolean("sort_ascending"))
        found = [t for t in self._world.tasks() if t.workspace == workspace.gid and view.sees(t)]
        completed = query.boolean("completed")
        if completed is not None:
            found = [t for t in found if t.completed == completed]
        subtask = query.boolean("is_subtask")
        if subtask is not None:
            found = [t for t in found if (t.parent is not None) == subtask]
        for name, keep in (("assignee.any", True), ("assignee.not", False)):
            named = query.text(name)
            if named is not None:
                users = {view.user(a.strip(), field=name, status=400).gid for a in named.split(",")}
                found = [t for t in found if (t.assignee in users) == keep]
        for name, keep in (("projects.any", True), ("projects.not", False)):
            gids = query.gids(name)
            if gids is not None:
                wanted = {view.project(p, field=name, status=400).gid for p in gids}
                found = [t for t in found if any(m.project in wanted for m in t.memberships) == keep]
        sections = query.gids("sections.any")
        if sections is not None:
            found = [t for t in found if any(m.section in sections for m in t.memberships)]
        tags = query.gids("tags.any")
        if tags is not None:
            found = [t for t in found if any(g in tags for g in t.tags)]
        text = (query.text("text") or "").strip().lower()
        if text:
            found = [t for t in found if text in t.name.lower() or text in t.notes.lower()]
        found.sort(
            key=lambda t: (t.modified_at if sort_by == "modified_at" else t.created_at, t.gid), reverse=not ascending
        )
        self._world.saw(state.record_ref(workspace.gid), Operation.SEARCH)
        return _answer(wire.unpaged([view.task_out(t) for t in found[:limit]], wire.field_tree(query)))

    async def typeahead(self, request: Request, caller: wire.AsanaUser) -> Response:
        view = self._view(caller)
        workspace = view.workspace(request.path_params["gid"])
        query = _query(request)
        resource_type = query.text("resource_type")
        if resource_type is None:
            raise wire.bad("resource_type: Missing input")
        if resource_type not in _TYPEAHEAD:
            raise wire.unsupported(f"resource_type={resource_type}")
        count = query.count("count") or wire.TYPEAHEAD_DEFAULT
        words = (query.text("query") or "").strip().lower()
        found: list[wire.Representation]
        match resource_type:
            case "task":  # enum-lint: exempt Asana's typeahead resource_type, its wire vocabulary
                found = [
                    view.task_out(t)
                    for t in self._world.tasks()
                    if t.workspace == workspace.gid and words in t.name.lower() and view.sees(t)
                ]
            case "user":  # enum-lint: exempt Asana's typeahead resource_type, its wire vocabulary
                found = [
                    view.user_out(u) for u in self._world.users() if words in u.name.lower() or words in u.email.lower()
                ]
            case "project":  # enum-lint: exempt Asana's typeahead resource_type, its wire vocabulary
                found = [
                    view.project_out(p)
                    for p in self._world.projects()
                    if p.workspace == workspace.gid and words in p.name.lower() and view.visible(p)
                ]
            case _:  # "tag", the last of Asana's typeahead resource types this serves
                found = [
                    view.tag_out(t)
                    for t in self._world.tags()
                    if t.workspace == workspace.gid and words in t.name.lower()
                ]
        self._world.saw(state.record_ref(workspace.gid), Operation.SEARCH)
        return _answer(wire.unpaged(found[:count], wire.field_tree(query)))

    async def webhooks(self, request: Request, caller: wire.AsanaUser) -> Response:
        """Asana's webhooks: the `X-Hook-Secret` handshake and event delivery are not served. Said, not ignored."""
        raise wire.unsupported("webhooks")


def build_app(store: Store, clock: Clock) -> Starlette:
    api = AsanaApi(store, clock)
    g = api.guarded

    async def no_route(request: Request, exc: Exception) -> Response:
        if isinstance(exc, HTTPException) and exc.status_code == 405:
            return _answer(wire.failed("Method not allowed"), 405)
        return _answer(wire.failed("No matching route for request"), 404)

    return Starlette(
        routes=[
            Route("/-/oauth_token", api.oauth_token, methods=["POST"]),
            Route("/users/me", g(api.me), methods=["GET"]),
            Route("/users", g(api.users), methods=["GET"]),
            Route("/users/{gid}", g(api.user), methods=["GET"]),
            Route("/users/{gid}/teams", g(api.user_teams), methods=["GET"]),
            Route("/workspaces", g(api.workspaces), methods=["GET"]),
            Route("/workspaces/{gid}", g(api.workspace), methods=["GET"]),
            Route("/workspaces/{gid}/users", g(api.workspace_users), methods=["GET"]),
            Route("/workspaces/{gid}/teams", g(api.workspace_teams), methods=["GET"]),
            Route("/workspaces/{gid}/projects", g(api.workspace_projects), methods=["GET"]),
            Route("/workspaces/{gid}/projects", g(api.create_workspace_project), methods=["POST"]),
            Route("/workspaces/{gid}/custom_fields", g(api.workspace_custom_fields), methods=["GET"]),
            Route("/workspaces/{gid}/tags", g(api.workspace_tags), methods=["GET"]),
            Route("/workspaces/{gid}/tags", g(api.create_workspace_tag), methods=["POST"]),
            Route("/workspaces/{gid}/tasks/search", g(api.search), methods=["GET"]),
            Route("/workspaces/{gid}/typeahead", g(api.typeahead), methods=["GET"]),
            Route("/teams/{gid}", g(api.team), methods=["GET"]),
            Route("/teams/{gid}/users", g(api.team_users), methods=["GET"]),
            Route("/teams/{gid}/projects", g(api.team_projects), methods=["GET"]),
            Route("/teams/{gid}/projects", g(api.create_team_project), methods=["POST"]),
            Route("/projects", g(api.projects), methods=["GET"]),
            Route("/projects", g(api.create_project), methods=["POST"]),
            Route("/projects/{gid}", g(api.project), methods=["GET"]),
            Route("/projects/{gid}/sections", g(api.project_sections), methods=["GET"]),
            Route("/projects/{gid}/sections", g(api.create_section), methods=["POST"]),
            Route("/projects/{gid}/tasks", g(api.project_tasks), methods=["GET"]),
            Route("/projects/{gid}/project_memberships", g(api.project_memberships), methods=["GET"]),
            Route("/projects/{gid}/addMembers", g(api.add_members), methods=["POST"]),
            Route("/projects/{gid}/removeMembers", g(api.remove_members), methods=["POST"]),
            Route("/projects/{gid}/custom_field_settings", g(api.custom_field_settings), methods=["GET"]),
            Route("/projects/{gid}/addCustomFieldSetting", g(api.add_custom_field_setting), methods=["POST"]),
            Route("/projects/{gid}/removeCustomFieldSetting", g(api.remove_custom_field_setting), methods=["POST"]),
            Route("/sections/{gid}", g(api.section), methods=["GET"]),
            Route("/sections/{gid}/tasks", g(api.section_tasks), methods=["GET"]),
            Route("/sections/{gid}/addTask", g(api.add_task_to_section), methods=["POST"]),
            Route("/custom_fields/{gid}", g(api.custom_field), methods=["GET"]),
            Route("/tags", g(api.tags), methods=["GET"]),
            Route("/tags", g(api.create_tag), methods=["POST"]),
            Route("/tags/{gid}", g(api.tag), methods=["GET"]),
            Route("/tags/{gid}/tasks", g(api.tag_tasks), methods=["GET"]),
            Route("/tasks", g(api.list_tasks), methods=["GET"]),
            Route("/tasks", g(api.create_task), methods=["POST"]),
            Route("/tasks/{gid}", g(api.get_task), methods=["GET"]),
            Route("/tasks/{gid}", g(api.update_task), methods=["PUT"]),
            Route("/tasks/{gid}", g(api.delete_task), methods=["DELETE"]),
            Route("/tasks/{gid}/subtasks", g(api.subtasks), methods=["GET"]),
            Route("/tasks/{gid}/subtasks", g(api.create_subtask), methods=["POST"]),
            Route("/tasks/{gid}/setParent", g(api.set_parent), methods=["POST"]),
            Route("/tasks/{gid}/tags", g(api.task_tags), methods=["GET"]),
            Route("/tasks/{gid}/addTag", g(api.add_tag), methods=["POST"]),
            Route("/tasks/{gid}/removeTag", g(api.remove_tag), methods=["POST"]),
            Route("/tasks/{gid}/stories", g(api.stories), methods=["GET"]),
            Route("/tasks/{gid}/stories", g(api.create_story), methods=["POST"]),
            Route("/webhooks", g(api.webhooks), methods=["GET", "POST"]),
        ],
        exception_handlers={404: no_route, 405: no_route},
    )
