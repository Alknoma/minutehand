"""The YouTrack REST API, as an ASGI app over the run's store and clock.

Every route answers at `/api/...` (YouTrack Cloud on `*.youtrack.cloud`) and at
`/youtrack/api/...` (the `*.myjetbrains.com` instances). Every answer is narrowed
by `fields=`, every collection is paged by `$skip`/`$top`, and every refusal is
YouTrack's own `{"error": …, "error_description": …}` with its status code.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.providers.youtrack import state, wire
from minutehand.adapters.providers.youtrack.query import (
    ME,
    UNASSIGNED,
    Attribute,
    Command,
    CommandWord,
    Search,
    parse_command,
    parse_search,
)
from minutehand.adapters.providers.youtrack.state import YouTrackWorld
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

PREFIXES = ("/api", "/youtrack/api")
_JSON = "application/json;charset=UTF-8"
_ENTITY_ID = re.compile(r"\d+-\d+")
"""YouTrack's database id. A short name or a readable id in an `{"id": …}` slot is refused before any lookup."""

Handler = Callable[[Request, bytes], tuple[int, bytes]]


def _param(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def _header(request: Request, name: str) -> str | None:
    return request.headers[name] if name in request.headers else None


class YouTrackApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = YouTrackWorld(store)
        self._clock = clock

    def endpoint(self, handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def answer(request: Request) -> Response:
            try:
                self._authenticate(request)
                status, payload = handler(request, await request.body())
            except wire.Refusal as refusal:
                return Response(wire.error_body(refusal), status_code=refusal.status, media_type=_JSON)
            return Response(payload, status_code=status, media_type=_JSON)

        return answer

    def _authenticate(self, request: Request) -> None:
        """Any bearer token is accepted: a permanent token (`perm:…`) or any other. Its absence is a 401."""
        authorization = _header(request, "authorization") or ""
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise wire.Refusal(401, "Unauthorized", "Not authorized, try to login first")

    def _now(self) -> int:
        return state.millis(self._clock.now())

    # ------------------------------------------------------------------ presenting

    def _stored_user(self, user: str) -> wire.StoredUser:
        found = self._world.user(user)
        if found is None:
            raise LookupError(f"user {user} is named by an entity and does not exist")
        return found

    def _user_out(self, user: wire.StoredUser) -> wire.UserOut:
        return wire.UserOut(
            id=user.id,
            login=user.login,
            fullName=user.fullName,
            name=user.fullName,
            email=user.email,
            ringId=user.ringId,
        )

    def _definition(self, definition: str, name: str, field_type: str) -> wire.CustomFieldOut:
        return wire.CustomFieldOut(id=definition, name=name, fieldType=wire.FieldTypeOut(id=field_type))

    def _state_value_out(self, value: wire.StoredState) -> wire.StateValueOut:
        return wire.StateValueOut(id=value.id, name=value.name, isResolved=value.isResolved, ordinal=value.ordinal)

    def _state_field_out(self, project: wire.StoredProject) -> wire.StateProjectFieldOut:
        return wire.StateProjectFieldOut(
            id=project.stateField,
            field=self._definition(project.stateFieldDefinition, "State", "state[1]"),
            bundle=wire.StateBundleOut(
                id=project.stateBundle,
                values=[self._state_value_out(s) for s in project.states],
            ),
        )

    def _team(self, project: wire.StoredProject) -> list[wire.UserOut]:
        return [self._user_out(self._stored_user(u)) for u in project.team]

    def _assignee_field_out(self, project: wire.StoredProject) -> wire.UserProjectFieldOut:
        return wire.UserProjectFieldOut(
            id=project.assigneeField,
            field=self._definition(project.assigneeFieldDefinition, "Assignee", "user[1]"),
            bundle=wire.UserBundleOut(id=project.assigneeBundle, aggregatedUsers=self._team(project)),
        )

    def _project_fields_out(self, project: wire.StoredProject) -> list[wire.ProjectFieldOut]:
        return [self._state_field_out(project), self._assignee_field_out(project)]

    def _project_out(self, project: wire.StoredProject) -> wire.ProjectOut:
        team = self._team(project)
        return wire.ProjectOut(
            id=project.id,
            name=project.name,
            shortName=project.shortName,
            description=project.description,
            leader=self._user_out(self._stored_user(project.leader)),
            team=wire.UserGroupOut(id=project.teamGroup, name=f"{project.name} Team", usersCount=len(team), users=team),
            customFields=self._project_fields_out(project),
        )

    def _issue_fields_out(self, project: wire.StoredProject, issue: wire.StoredIssue) -> list[wire.IssueFieldOut]:
        assignee = self._stored_user(issue.assignee) if issue.assignee is not None else None
        return [
            wire.StateIssueFieldOut(
                id=project.stateField,
                name="State",
                value=self._state_value_out(self._world.state_of(project, issue)),
                projectCustomField=self._state_field_out(project),
            ),
            wire.UserIssueFieldOut(
                id=project.assigneeField,
                name="Assignee",
                value=self._user_out(assignee) if assignee is not None else None,
                projectCustomField=self._assignee_field_out(project),
            ),
        ]

    def _comment_out(self, comment: wire.StoredComment) -> wire.CommentOut:
        return wire.CommentOut(
            id=comment.id,
            text=comment.text,
            textPreview=comment.text,
            author=self._user_out(self._stored_user(comment.author)),
            created=comment.created,
            updated=comment.updated,
        )

    def _home(self, issue: wire.StoredIssue) -> wire.StoredProject:
        project = self._world.project(issue.project)
        if project is None:
            raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
        return project

    def _issue_out(self, issue: wire.StoredIssue) -> wire.IssueOut:
        project = self._home(issue)
        comments = [self._comment_out(c) for c in self._world.comments(issue.id)]
        return wire.IssueOut(
            id=issue.id,
            idReadable=issue.idReadable,
            numberInProject=issue.numberInProject,
            summary=issue.summary,
            description=issue.description,
            project=self._project_out(project),
            reporter=self._user_out(self._stored_user(issue.reporter)),
            updater=self._user_out(self._stored_user(issue.updater)),
            created=issue.created,
            updated=issue.updated,
            resolved=issue.resolved,
            customFields=self._issue_fields_out(project, issue),
            comments=comments,
            commentsCount=len(comments),
        )

    # ------------------------------------------------------------------ lookups

    def _issue(self, request: Request) -> wire.StoredIssue:
        reference = request.path_params["issue"]
        found = self._world.find_issue(reference)
        if found is None:
            raise wire.not_found(reference)
        return found

    def _project(self, request: Request) -> wire.StoredProject:
        reference = request.path_params["project"]
        found = self._world.project(reference) or self._world.project_named(reference)
        if found is None:
            raise wire.not_found(reference)
        return found

    def _page[T](self, request: Request, items: list[T]) -> list[T]:
        start, limit = wire.page_bounds(_param(request, "$skip"), _param(request, "$top"))
        return items[start:] if limit is None else items[start : start + limit]

    def _answer(self, request: Request, answer: wire.Answer | Sequence[wire.Answer]) -> tuple[int, bytes]:
        return 200, wire.render(answer, wire.parse_fields(_param(request, "fields")))

    # ------------------------------------------------------------------ users

    def me(self, request: Request, _: bytes) -> tuple[int, bytes]:
        me = self._world.me()
        self._world.saw(state.user_ref(me.id), Operation.READ)
        return self._answer(
            request,
            wire.MeOut(
                id=me.id,
                login=me.login,
                fullName=me.fullName,
                name=me.fullName,
                email=me.email,
                ringId=me.ringId,
            ),
        )

    def users(self, request: Request, _: bytes) -> tuple[int, bytes]:
        wanted = (_param(request, "query") or "").strip().lower()
        found = [
            u
            for u in self._world.users()
            if not wanted
            or wanted in u.login.lower()
            or wanted in u.fullName.lower()
            or (u.email is not None and wanted in u.email.lower())
        ]
        page: list[wire.Answer] = [self._user_out(u) for u in self._page(request, found)]
        self._world.saw(state.instance_ref(), Operation.SEARCH)
        return self._answer(request, page)

    # ------------------------------------------------------------------ projects

    def projects(self, request: Request, _: bytes) -> tuple[int, bytes]:
        page: list[wire.Answer] = [self._project_out(p) for p in self._page(request, self._world.projects())]
        self._world.saw(state.instance_ref(), Operation.SEARCH)
        return self._answer(request, page)

    def project(self, request: Request, _: bytes) -> tuple[int, bytes]:
        project = self._project(request)
        self._world.saw(state.project_ref(project.id), Operation.READ)
        return self._answer(request, self._project_out(project))

    def project_fields(self, request: Request, _: bytes) -> tuple[int, bytes]:
        project = self._project(request)
        page: list[wire.Answer] = list(self._page(request, self._project_fields_out(project)))
        self._world.saw(state.project_ref(project.id), Operation.READ)
        return self._answer(request, page)

    # ------------------------------------------------------------------ issues: reads

    def search(self, request: Request, _: bytes) -> tuple[int, bytes]:
        search = parse_search(_param(request, "query"))
        matched = self._matching(search)
        page: list[wire.Answer] = [self._issue_out(i) for i in self._page(request, matched)]
        scope = next((c for c in search.clauses if c.attribute is Attribute.PROJECT and len(c.values) == 1), None)
        if scope is not None:
            named = self._world.project_named(scope.values[0])
            assert named is not None
            self._world.saw(state.project_ref(named.id), Operation.SEARCH)
        else:
            self._world.saw(state.instance_ref(), Operation.SEARCH)
        return self._answer(request, page)

    def _matching(self, search: Search) -> list[wire.StoredIssue]:
        """The issues a query matches, oldest first. A value no field in scope has is refused, never answered empty."""
        projects = self._world.projects()
        scope = projects
        wanted_projects: list[set[str]] = []
        wanted_states: list[set[str]] = []
        wanted_assignees: list[set[str | None]] = []
        wanted_ids: list[set[str]] = []
        for clause in search.clauses:
            if clause.attribute is Attribute.PROJECT:
                ids: set[str] = set()
                for value in clause.values:
                    named = self._world.project_named(value)
                    if named is None:
                        raise wire.invalid_query(value, "project")
                    ids.add(named.id)
                wanted_projects.append(ids)
                scope = [p for p in scope if p.id in ids]
        for clause in search.clauses:
            if clause.attribute is Attribute.STATE:
                names = {s.name.lower() for p in scope for s in p.states}
                for value in clause.values:
                    if value.lower() not in names:
                        raise wire.invalid_query(value, "State")
                wanted_states.append({v.lower() for v in clause.values})
            elif clause.attribute is Attribute.ASSIGNEE:
                people: set[str | None] = set()
                for value in clause.values:
                    if value.lower() == ME:
                        people.add(self._world.me().id)
                    elif value.lower() == UNASSIGNED:
                        people.add(None)
                    else:
                        found = self._world.user_by_login(value)
                        if found is None:
                            raise wire.invalid_query(value, "Assignee")
                        people.add(found.id)
                wanted_assignees.append(people)
            elif clause.attribute is Attribute.ISSUE_ID:
                wanted_ids.append({v.lower() for v in clause.values})
        words = [w.lower() for w in search.words]

        matched: list[wire.StoredIssue] = []
        for project in projects:
            if any(project.id not in ids for ids in wanted_projects):
                continue
            for issue in self._world.issues(project.id):
                current = self._world.state_of(project, issue)
                text = " ".join([issue.summary, issue.description or "", issue.idReadable]).lower()
                if (
                    all(current.name.lower() in names for names in wanted_states)
                    and all(issue.assignee in people for people in wanted_assignees)
                    and all(issue.idReadable.lower() in ids or issue.id in ids for ids in wanted_ids)
                    and (search.resolved is None or current.isResolved is search.resolved)
                    and all(word in text for word in words)
                ):
                    matched.append(issue)
        return sorted(matched, key=lambda i: (i.created, state_order(i.id)))

    def issue(self, request: Request, _: bytes) -> tuple[int, bytes]:
        issue = self._issue(request)
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return self._answer(request, self._issue_out(issue))

    def issue_fields(self, request: Request, _: bytes) -> tuple[int, bytes]:
        issue = self._issue(request)
        page: list[wire.Answer] = list(self._page(request, self._issue_fields_out(self._home(issue), issue)))
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return self._answer(request, page)

    def comments(self, request: Request, _: bytes) -> tuple[int, bytes]:
        issue = self._issue(request)
        page: list[wire.Answer] = [self._comment_out(c) for c in self._page(request, self._world.comments(issue.id))]
        self._world.saw(state.issue_ref(issue.id), Operation.READ)
        return self._answer(request, page)

    # ------------------------------------------------------------------ issues: writes

    def create(self, request: Request, raw: bytes) -> tuple[int, bytes]:
        body = wire.read_body(wire.IssueCreateIn, raw)
        if body.project is None or body.project.id is None:
            raise wire.bad_request("project is required")
        if not _ENTITY_ID.fullmatch(body.project.id):
            raise wire.bad_request(f"Invalid structure of entity id: {body.project.id}")
        project = self._world.project(body.project.id)
        if project is None:
            raise wire.not_found(body.project.id)
        if body.summary is None or not body.summary.strip():
            raise wire.bad_request("summary is required")
        me, now = self._world.me(), self._now()
        number = self._world.next_number(project.id)
        issue = wire.StoredIssue(
            id=self._world.next_id(2),
            idReadable=f"{project.shortName}-{number}",
            numberInProject=number,
            project=project.id,
            summary=body.summary,
            description=body.description,
            reporter=me.id,
            updater=me.id,
            created=now,
            updated=now,
            state=project.states[0].id,
        )
        issue = self._with_fields(project, issue, body.customFields, by=me.id, at=now)
        self._world.create_issue(issue, actor=Actor.AGENT)
        return self._answer(request, self._issue_out(issue))

    def update(self, request: Request, raw: bytes) -> tuple[int, bytes]:
        issue = self._issue(request)
        body = wire.read_body(wire.IssueUpdateIn, raw)
        project = self._home(issue)
        me, now = self._world.me(), self._now()
        changed = issue
        if "summary" in body.model_fields_set:
            if body.summary is None or not body.summary.strip():
                raise wire.bad_request("summary is required")
            changed = changed.model_copy(update={"summary": body.summary})
        if "description" in body.model_fields_set:
            changed = changed.model_copy(update={"description": body.description})
        changed = self._with_fields(project, changed, body.customFields, by=me.id, at=now)
        if _differs(issue, changed):
            changed = changed.model_copy(update={"updated": now, "updater": me.id})
            self._world.update_issue(changed, actor=Actor.AGENT)
        return self._answer(request, self._issue_out(changed))

    def delete(self, request: Request, _: bytes) -> tuple[int, bytes]:
        issue = self._issue(request)
        self._world.delete_issue(issue, actor=Actor.AGENT)
        return 200, b""

    def comment(self, request: Request, raw: bytes) -> tuple[int, bytes]:
        issue = self._issue(request)
        body = wire.read_body(wire.CommentIn, raw)
        if body.text is None or not body.text.strip():
            raise wire.bad_request("text is required")
        written = self._comment(issue, body.text)
        return self._answer(request, self._comment_out(written))

    def _comment(self, issue: wire.StoredIssue, text: str) -> wire.StoredComment:
        comment = wire.StoredComment(
            id=self._world.next_id(4),
            issue=issue.id,
            text=text,
            author=self._world.me().id,
            created=self._now(),
        )
        self._world.write_comment(comment, actor=Actor.AGENT)
        return comment

    def _with_fields(
        self,
        project: wire.StoredProject,
        issue: wire.StoredIssue,
        writes: list[wire.CustomFieldIn],
        *,
        by: str,
        at: int,
    ) -> wire.StoredIssue:
        """The issue with a `customFields` block applied, every write checked before any is kept."""
        for write in writes:
            name: str = (write.name or "").strip().lower()
            if write.id == project.stateField or name == "state":
                if write.value is None:
                    raise wire.value_not_allowed()
                issue = self._world.moved(issue, project, self._state_value(project, write.value), by=by, at=at)
            elif write.id == project.assigneeField or name == "assignee":
                if write.value is None:
                    issue = issue.model_copy(update={"assignee": None})
                else:
                    issue = issue.model_copy(update={"assignee": self._assignable(project, write.value).id})
            else:
                raise wire.Refusal(404, "Not Found", f"Entity with name {write.name or write.id} not found")
        return issue

    def _state_value(self, project: wire.StoredProject, value: wire.EntityIn) -> wire.StoredState:
        found = next(
            (
                s
                for s in project.states
                if (value.id is not None and s.id == value.id)
                or (value.name is not None and s.name.lower() == value.name.strip().lower())
            ),
            None,
        )
        if found is None:
            raise wire.value_not_allowed()
        return found

    def _assignable(self, project: wire.StoredProject, value: wire.EntityIn) -> wire.StoredUser:
        """A user the Assignee field takes: one who exists and is on the project's team."""
        found = (
            self._world.user(value.id)
            if value.id is not None
            else self._world.user_by_login(value.login)
            if value.login is not None
            else None
        )
        if found is None or found.id not in project.team:
            raise wire.value_not_allowed()
        return found

    # ------------------------------------------------------------------ commands

    def command(self, request: Request, raw: bytes) -> tuple[int, bytes]:
        body = wire.read_body(wire.CommandIn, raw)
        commands = parse_command(body.query)
        if not body.issues:
            raise wire.bad_request("issues are required")
        me, now = self._world.me(), self._now()
        targets: list[wire.StoredIssue] = []
        for named in body.issues:
            reference = named.id or named.idReadable
            found = self._world.find_issue(reference) if reference else None
            if found is None:
                raise wire.not_found(reference or "")
            targets.append(found)
        planned = [(issue, self._commanded(self._home(issue), issue, commands, me=me, at=now)) for issue in targets]
        for before, after in planned:
            if _differs(before, after):
                self._world.update_issue(after.model_copy(update={"updated": now, "updater": me.id}), actor=Actor.AGENT)
            if body.comment is not None and body.comment.strip():
                self._comment(before, body.comment)
        answer = wire.CommandListOut(
            query=body.query or "",
            issues=[wire.IssueRefOut(id=i.id, idReadable=i.idReadable) for i in targets],
            commands=[wire.ParsedCommandOut(description=f"{c.word.value} {c.value}") for c in commands],
            comment=body.comment,
            silent=body.silent,
        )
        return self._answer(request, answer)

    def _commanded(
        self,
        project: wire.StoredProject,
        issue: wire.StoredIssue,
        commands: list[Command],
        *,
        me: wire.StoredUser,
        at: int,
    ) -> wire.StoredIssue:
        for command in commands:
            if command.word is CommandWord.STATE:
                issue = self._world.moved(
                    issue,
                    project,
                    self._state_value(project, wire.EntityIn(name=command.value)),
                    by=me.id,
                    at=at,
                )
            else:
                login = me.login if command.value.lower() == ME else command.value
                issue = issue.model_copy(update={"assignee": self._assignable(project, wire.EntityIn(login=login)).id})
        return issue


def _differs(before: wire.StoredIssue, after: wire.StoredIssue) -> bool:
    keep = {"updated", "updater"}
    return before.model_dump(exclude=keep) != after.model_dump(exclude=keep)


def state_order(entity_id: str) -> int:
    return int(entity_id.partition("-")[2])


def build_app(store: Store, clock: Clock) -> Starlette:
    api = YouTrackApi(store, clock)
    table: list[tuple[str, str, Handler]] = [
        ("/users/me", "GET", api.me),
        ("/users", "GET", api.users),
        ("/admin/projects", "GET", api.projects),
        ("/admin/projects/{project}", "GET", api.project),
        ("/admin/projects/{project}/customFields", "GET", api.project_fields),
        ("/issues", "GET", api.search),
        ("/issues", "POST", api.create),
        ("/issues/{issue}", "GET", api.issue),
        ("/issues/{issue}", "POST", api.update),
        ("/issues/{issue}", "DELETE", api.delete),
        ("/issues/{issue}/comments", "GET", api.comments),
        ("/issues/{issue}/comments", "POST", api.comment),
        ("/issues/{issue}/customFields", "GET", api.issue_fields),
        ("/commands", "POST", api.command),
    ]
    by_path: dict[str, dict[str, Handler]] = {}
    for path, method, handler in table:
        by_path.setdefault(path, {})[method] = handler
    routes: list[Route] = []
    for prefix in PREFIXES:
        for path, methods in by_path.items():
            routes.append(Route(prefix + path, _dispatch(api, methods), methods=list(methods)))

    async def refused(request: Request, error: Exception) -> Response:
        status = error.status_code if isinstance(error, HTTPException) else 500
        reason = "Not Found" if status == 404 else "Method Not Allowed" if status == 405 else "Server Error"
        return Response(
            wire.error_body(wire.Refusal(status, reason, f"{request.method} {request.url.path} is not served")),
            status_code=status,
            media_type=_JSON,
        )

    return Starlette(routes=routes, exception_handlers={404: refused, 405: refused})


def _dispatch(api: YouTrackApi, methods: dict[str, Handler]) -> Callable[[Request], Awaitable[Response]]:
    answers = {method: api.endpoint(handler) for method, handler in methods.items()}

    async def route(request: Request) -> Response:
        return await answers["GET" if request.method == "HEAD" else request.method](request)

    return route
