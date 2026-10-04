"""The Asana REST API, as an ASGI app over the run's store and clock.

Paths are Asana's with `/api/1.0` already removed by the proxy. Every answer is
Asana's envelope: `{"data": ...}` (with `next_page` on a paginated collection), or
`{"errors": [{"message", "help"}]}` with Asana's status code. A single resource is
answered in full, a collection compact, and `opt_fields` narrows either.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

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

Handler = Callable[[Request], Awaitable[Response]]

_JSON = "application/json; charset=utf-8"
_SEARCH = ("text", "completed", "assignee.any", "projects.any", "sort_by", "sort_ascending", "limit",
           "opt_fields", "opt_pretty")
_SEARCH_UNSUPPORTED = (
    "assignee.not", "projects.not", "projects.all", "sections.any", "sections.not", "sections.all", "tags.any",
    "tags.not", "tags.all", "teams.any", "followers.any", "followers.not", "created_by.any", "created_by.not",
    "assigned_by.any", "assigned_by.not", "liked_by.not", "commented_on_by.not", "portfolios.any",
    "due_on", "due_on.before", "due_on.after", "due_at.before", "due_at.after", "start_on", "start_on.before",
    "start_on.after", "created_on", "created_on.before", "created_on.after", "created_at.before",
    "created_at.after", "completed_on", "completed_on.before", "completed_on.after", "completed_at.before",
    "completed_at.after", "modified_on", "modified_on.before", "modified_on.after", "modified_at.before",
    "modified_at.after", "is_blocking", "is_blocked", "has_attachment", "is_subtask", "resource_subtype",
)
_SORTS = ("modified_at", "created_at")
_TYPEAHEAD = ("task", "user", "project")
_NEEDS_FILTER = "Must specify exactly one of project, tag, section, user task list, or assignee + workspace"


def _answer(body: bytes, status: int = 200) -> Response:
    return Response(body, status_code=status, media_type=_JSON)


class AsanaApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._world = AsanaWorld(store)
        self._clock = clock

    def guarded(self, handler: Handler) -> Handler:
        """Authenticate, then answer; a refusal becomes Asana's error envelope and records nothing."""

        async def endpoint(request: Request) -> Response:
            authorization = request.headers["authorization"] if "authorization" in request.headers else ""
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                return _answer(wire.failed("Not Authorized"), 401)
            try:
                return await handler(request)
            except wire.Refusal as refusal:
                return _answer(wire.failed(refusal.message), refusal.status)

        return endpoint

    # ------------------------------------------------------------------ lookups

    def _workspace(self, gid: str, *, status: int = 404) -> wire.AsanaWorkspace:
        found = self._world.workspace(wire.gid_in_path("workspace", gid))
        if found is None:
            raise wire.unknown("workspace", gid, status=status)
        return found

    def _project(self, gid: str, *, field: str = "project", status: int = 404) -> wire.AsanaProject:
        found = self._world.project(wire.gid_in_path(field, gid))
        if found is None:
            raise wire.unknown(field, gid, status=status)
        return found

    def _task(self, gid: str) -> wire.AsanaTask:
        found = self._world.task(wire.gid_in_path("task", gid))
        if found is None:
            raise wire.unknown("task", gid, status=404)
        return found

    def _user(self, identifier: str, *, field: str, status: int) -> wire.AsanaUser:
        if not wire.is_user_identifier(identifier):
            raise wire.bad(f"{field}: Not a Recognized ID")
        found = self._world.resolve_user(identifier)
        if found is None:
            raise wire.unknown(field, identifier, status=status)
        return found

    def _workspace_filter(self, query: wire.Query) -> None:
        gid = query.text("workspace")
        if gid is not None:
            self._workspace(gid, status=400)

    # ------------------------------------------------------------------ representations

    def _workspace_out(self, workspace: wire.AsanaWorkspace) -> wire.WorkspaceOut:
        return wire.WorkspaceOut(gid=workspace.gid, name=workspace.name, is_organization=workspace.is_organization,
                                 email_domains=workspace.email_domains)

    def _workspace_of(self, gid: str) -> wire.WorkspaceOut:
        return self._workspace_out(_held(self._world.workspace(gid), gid))

    def _user_out(self, user: wire.AsanaUser) -> wire.UserOut:
        return wire.UserOut(gid=user.gid, name=user.name, email=user.email,
                            workspaces=[self._workspace_out(w) for w in self._world.workspaces()])

    def _user_of(self, gid: str) -> wire.UserOut:
        return self._user_out(_held(self._world.user(gid), gid))

    def _project_out(self, project: wire.AsanaProject) -> wire.ProjectOut:
        return wire.ProjectOut(
            gid=project.gid, name=project.name, notes=project.notes, archived=project.archived,
            created_at=project.created_at, workspace=self._workspace_of(project.workspace),
            permalink_url=f"https://app.asana.com/0/{project.gid}/list",
        )

    def _project_of(self, gid: str) -> wire.ProjectOut:
        return self._project_out(_held(self._world.project(gid), gid))

    def _section_out(self, section: wire.AsanaSection) -> wire.SectionOut:
        return wire.SectionOut(gid=section.gid, name=section.name, created_at=section.created_at,
                               project=self._project_of(section.project))

    def _task_out(self, task: wire.AsanaTask) -> wire.TaskOut:
        first = task.memberships[0].project if task.memberships else "0"
        return wire.TaskOut(
            gid=task.gid, name=task.name, notes=task.notes, html_notes=wire.html_notes(task.notes),
            completed=task.completed, completed_at=task.completed_at, due_on=task.due_on, due_at=task.due_at,
            created_at=task.created_at, modified_at=task.modified_at,
            assignee=self._user_of(task.assignee) if task.assignee is not None else None,
            created_by=self._user_of(task.created_by),
            memberships=[
                wire.MembershipOut(project=self._project_of(m.project),
                                   section=self._section_out(_held(self._world.section(m.section), m.section)))
                for m in task.memberships
            ],
            projects=[self._project_of(m.project) for m in task.memberships],
            workspace=self._workspace_of(task.workspace),
            permalink_url=f"https://app.asana.com/0/{first}/{task.gid}",
        )

    def _story_out(self, story: wire.AsanaStory) -> wire.StoryOut:
        task = self._world.task(story.task)
        return wire.StoryOut(
            gid=story.gid, text=story.text, html_text=wire.html_notes(story.text), created_at=story.created_at,
            created_by=self._user_of(story.created_by),
            target=wire.Compact(gid=story.task, resource_type="task", name=task.name if task is not None else ""),
        )

    def _listed[Out: wire.Representation](self, request: Request, items: list[Out]) -> Response:
        query = _query(request)
        chosen, next_page = wire.page(items, query, request.url.path)
        return _answer(wire.many(chosen, wire.field_tree(query), next_page))

    def _one(self, request: Request, item: wire.Representation, status: int = 200) -> Response:
        return _answer(wire.one(item, wire.field_tree(_query(request))), status)

    # ------------------------------------------------------------------ users, workspaces

    async def me(self, request: Request) -> Response:
        user = self._user("me", field="user", status=404)
        self._world.saw(state.record_ref(user.gid), Operation.READ)
        return self._one(request, self._user_out(user))

    async def users(self, request: Request) -> Response:
        query = _query(request)
        self._workspace_filter(query)
        query.refuse(["team"])
        self._world.saw(state.record_ref(state.WORKSPACE_GID), Operation.SEARCH)
        return self._listed(request, [self._user_out(u) for u in self._world.users()])

    async def user(self, request: Request) -> Response:
        user = self._user(request.path_params["gid"], field="user", status=404)
        self._world.saw(state.record_ref(user.gid), Operation.READ)
        return self._one(request, self._user_out(user))

    async def workspaces(self, request: Request) -> Response:
        self._world.saw(state.record_ref(state.WORKSPACE_GID), Operation.SEARCH)
        return self._listed(request, [self._workspace_out(w) for w in self._world.workspaces()])

    async def workspace(self, request: Request) -> Response:
        workspace = self._workspace(request.path_params["gid"])
        self._world.saw(state.record_ref(workspace.gid), Operation.READ)
        return self._one(request, self._workspace_out(workspace))

    async def workspace_users(self, request: Request) -> Response:
        workspace = self._workspace(request.path_params["gid"])
        self._world.saw(state.record_ref(workspace.gid), Operation.SEARCH)
        return self._listed(request, [self._user_out(u) for u in self._world.users()])

    # ------------------------------------------------------------------ projects

    def _projects(self, request: Request, workspace: str | None) -> Response:
        query = _query(request)
        query.refuse(["team"])
        archived = query.boolean("archived")
        found = [p for p in self._world.projects()
                 if (workspace is None or p.workspace == workspace) and (archived is None or p.archived == archived)]
        self._world.saw(state.record_ref(workspace or state.WORKSPACE_GID), Operation.SEARCH)
        return self._listed(request, [self._project_out(p) for p in found])

    async def workspace_projects(self, request: Request) -> Response:
        return self._projects(request, self._workspace(request.path_params["gid"]).gid)

    async def projects(self, request: Request) -> Response:
        query = _query(request)
        gid = query.text("workspace")
        return self._projects(request, self._workspace(gid, status=400).gid if gid is not None else None)

    async def project(self, request: Request) -> Response:
        project = self._project(request.path_params["gid"])
        self._world.saw(state.record_ref(project.gid), Operation.READ)
        return self._one(request, self._project_out(project))

    async def project_sections(self, request: Request) -> Response:
        project = self._project(request.path_params["gid"])
        self._world.saw(state.record_ref(project.gid), Operation.SEARCH)
        return self._listed(request, [self._section_out(s) for s in self._world.sections(project.gid)])

    async def project_tasks(self, request: Request) -> Response:
        project = self._project(request.path_params["gid"])
        found = _completed_since(_query(request), self._world.project_tasks(project.gid))
        self._world.saw(state.record_ref(project.gid), Operation.SEARCH)
        return self._listed(request, [self._task_out(t) for t in found])

    # ------------------------------------------------------------------ tasks

    async def list_tasks(self, request: Request) -> Response:
        query = _query(request)
        query.refuse(["tag", "user_task_list"])
        project, section, assignee = query.text("project"), query.text("section"), query.text("assignee")
        workspace = query.text("workspace")
        chosen = [project is not None, section is not None, assignee is not None and workspace is not None]
        if sum(chosen) != 1 or (assignee is None) != (workspace is None):
            raise wire.bad(_NEEDS_FILTER)
        if project is not None:
            found = self._world.project_tasks(self._project(project, status=400).gid)
            seen = project
        elif section is not None:
            wanted = self._world.section(wire.gid_in_path("section", section))
            if wanted is None:
                raise wire.unknown("section", section, status=400)
            found = [t for t in self._world.tasks() if any(m.section == wanted.gid for m in t.memberships)]
            seen = wanted.project
        else:
            assert assignee is not None and workspace is not None
            ws = self._workspace(workspace, status=400)
            user = self._user(assignee, field="assignee", status=400)
            found = [t for t in self._world.tasks() if t.assignee == user.gid and t.workspace == ws.gid]
            seen = ws.gid
        found = _completed_since(query, found)
        since = query.moment("modified_since")
        if since is not None:
            found = [t for t in found if _at(t.modified_at) >= since]
        self._world.saw(state.record_ref(seen), Operation.SEARCH)
        return self._listed(request, [self._task_out(t) for t in found])

    async def create_task(self, request: Request) -> Response:
        sent = wire.task_create(wire.envelope(await request.body()))
        projects = [self._project(p, field="projects", status=400) for p in dict.fromkeys(sent.projects)]
        if sent.workspace is not None:
            workspace = self._workspace(sent.workspace, status=400).gid
            if any(p.workspace != workspace for p in projects):
                raise wire.bad("projects: Must be in the same workspace as the task")
        else:
            workspace = projects[0].workspace
        assignee = self._user(sent.assignee, field="assignee", status=400) if sent.assignee is not None else None
        now = wire.stamp(self._clock.now())
        task = wire.AsanaTask(
            gid=self._world.next_gid(), name=sent.name, notes=sent.notes, completed=sent.completed,
            completed_at=now if sent.completed else None, due_on=_due_on(sent.due_on, sent.due_at),
            due_at=sent.due_at, assignee=assignee.gid if assignee is not None else None, created_by=AGENT_GID,
            workspace=workspace,
            memberships=[wire.AsanaMembership(project=p.gid, section=self._first_section(p.gid)) for p in projects],
            created_at=now, modified_at=now,
        )
        self._world.put_task(task, operation=Operation.CREATE, actor=Actor.AGENT)
        return self._one(request, self._task_out(task), 201)

    def _first_section(self, project: str) -> str:
        sections = self._world.sections(project)
        if not sections:
            raise LookupError(f"asana project {project} has no section for a new task to land in")
        return sections[0].gid

    async def get_task(self, request: Request) -> Response:
        task = self._task(request.path_params["gid"])
        self._world.saw(state.task_ref(task.gid), Operation.READ)
        return self._one(request, self._task_out(task))

    async def update_task(self, request: Request) -> Response:
        task = self._task(request.path_params["gid"])
        sent = wire.task_update(wire.envelope(await request.body()))
        given = sent.model_fields_set
        now = wire.stamp(self._clock.now())
        update: dict[str, str | bool | None] = {"modified_at": now}
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
            user = self._user(sent.assignee, field="assignee", status=400) if sent.assignee is not None else None
            update["assignee"] = user.gid if user is not None else None
        changed = task.model_copy(update=update)
        self._world.put_task(changed, operation=Operation.UPDATE, actor=Actor.AGENT)
        return self._one(request, self._task_out(changed))

    async def delete_task(self, request: Request) -> Response:
        task = self._task(request.path_params["gid"])
        self._world.delete_task(task, actor=Actor.AGENT)
        return _answer(wire.empty())

    # ------------------------------------------------------------------ stories

    async def create_story(self, request: Request) -> Response:
        task = self._task(request.path_params["gid"])
        sent = wire.story_create(wire.envelope(await request.body()))
        story = wire.AsanaStory(gid=self._world.next_gid(), text=sent.text, task=task.gid, created_by=AGENT_GID,
                                created_at=wire.stamp(self._clock.now()))
        self._world.put_story(story, actor=Actor.AGENT)
        return self._one(request, self._story_out(story), 201)

    async def stories(self, request: Request) -> Response:
        task = self._task(request.path_params["gid"])
        self._world.saw(state.task_ref(task.gid), Operation.READ)
        return self._listed(request, [self._story_out(s) for s in self._world.stories(task.gid)])

    # ------------------------------------------------------------------ search, typeahead

    async def search(self, request: Request) -> Response:
        """Advanced search: at most `limit` tasks, newest change first, and no `next_page`."""
        workspace = self._workspace(request.path_params["gid"])
        query = _query(request)
        for name in query.names():
            if name in _SEARCH_UNSUPPORTED:
                raise wire.unsupported(name)
            if name not in _SEARCH:
                raise wire.bad(f"{name}: Unrecognized parameter")
        limit = query.count("limit") or wire.SEARCH_DEFAULT
        sort_by = query.text("sort_by") or "modified_at"
        if sort_by not in _SORTS:
            raise wire.unsupported(f"sort_by={sort_by}")
        ascending = bool(query.boolean("sort_ascending"))
        found = [t for t in self._world.tasks() if t.workspace == workspace.gid]
        completed = query.boolean("completed")
        if completed is not None:
            found = [t for t in found if t.completed == completed]
        assignees = query.text("assignee.any")
        if assignees is not None:
            wanted = {self._user(a.strip(), field="assignee.any", status=400).gid for a in assignees.split(",")}
            found = [t for t in found if t.assignee in wanted]
        projects = query.text("projects.any")
        if projects is not None:
            gids = {self._project(p.strip(), field="projects.any", status=400).gid for p in projects.split(",")}
            found = [t for t in found if any(m.project in gids for m in t.memberships)]
        text = (query.text("text") or "").strip().lower()
        if text:
            found = [t for t in found if text in t.name.lower() or text in t.notes.lower()]
        found.sort(key=lambda t: (t.modified_at if sort_by == "modified_at" else t.created_at, t.gid),
                   reverse=not ascending)
        self._world.saw(state.record_ref(workspace.gid), Operation.SEARCH)
        return _answer(wire.unpaged([self._task_out(t) for t in found[:limit]], wire.field_tree(query)))

    async def typeahead(self, request: Request) -> Response:
        workspace = self._workspace(request.path_params["gid"])
        query = _query(request)
        resource_type = query.text("resource_type")
        if resource_type is None:
            raise wire.bad("resource_type: Missing input")
        if resource_type not in _TYPEAHEAD:
            raise wire.unsupported(f"resource_type={resource_type}")
        count = query.count("count") or wire.TYPEAHEAD_DEFAULT
        words = (query.text("query") or "").strip().lower()
        found: list[wire.Representation]
        if resource_type == "task":
            found = [self._task_out(t) for t in self._world.tasks()
                     if t.workspace == workspace.gid and words in t.name.lower()]
        elif resource_type == "user":
            found = [self._user_out(u) for u in self._world.users()
                     if words in u.name.lower() or words in u.email.lower()]
        else:
            found = [self._project_out(p) for p in self._world.projects()
                     if p.workspace == workspace.gid and words in p.name.lower()]
        self._world.saw(state.record_ref(workspace.gid), Operation.SEARCH)
        return _answer(wire.unpaged(found[:count], wire.field_tree(query)))


def _held[Found](found: Found | None, gid: str) -> Found:
    """Something a stored record refers to; its absence is a broken world, not a refusal."""
    if found is None:
        raise LookupError(f"asana gid {gid} is referenced and names nothing in the world")
    return found


def _query(request: Request) -> wire.Query:
    return wire.Query(list(request.query_params.multi_items()))


def _at(value: str) -> datetime:
    parsed = wire.parse_stamp(value)
    if parsed is None:
        raise ValueError(f"stored asana timestamp {value!r} is not one")
    return parsed


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


def build_app(store: Store, clock: Clock) -> Starlette:
    api = AsanaApi(store, clock)
    g = api.guarded

    async def no_route(request: Request, exc: Exception) -> Response:
        if isinstance(exc, HTTPException) and exc.status_code == 405:
            return _answer(wire.failed("Method not allowed"), 405)
        return _answer(wire.failed("No matching route for request"), 404)

    return Starlette(
        routes=[
            Route("/users/me", g(api.me), methods=["GET"]),
            Route("/users", g(api.users), methods=["GET"]),
            Route("/users/{gid}", g(api.user), methods=["GET"]),
            Route("/workspaces", g(api.workspaces), methods=["GET"]),
            Route("/workspaces/{gid}", g(api.workspace), methods=["GET"]),
            Route("/workspaces/{gid}/users", g(api.workspace_users), methods=["GET"]),
            Route("/workspaces/{gid}/projects", g(api.workspace_projects), methods=["GET"]),
            Route("/workspaces/{gid}/tasks/search", g(api.search), methods=["GET"]),
            Route("/workspaces/{gid}/typeahead", g(api.typeahead), methods=["GET"]),
            Route("/projects", g(api.projects), methods=["GET"]),
            Route("/projects/{gid}", g(api.project), methods=["GET"]),
            Route("/projects/{gid}/sections", g(api.project_sections), methods=["GET"]),
            Route("/projects/{gid}/tasks", g(api.project_tasks), methods=["GET"]),
            Route("/tasks", g(api.list_tasks), methods=["GET"]),
            Route("/tasks", g(api.create_task), methods=["POST"]),
            Route("/tasks/{gid}", g(api.get_task), methods=["GET"]),
            Route("/tasks/{gid}", g(api.update_task), methods=["PUT"]),
            Route("/tasks/{gid}", g(api.delete_task), methods=["DELETE"]),
            Route("/tasks/{gid}/stories", g(api.stories), methods=["GET"]),
            Route("/tasks/{gid}/stories", g(api.create_story), methods=["POST"]),
        ],
        exception_handlers={404: no_route, 405: no_route},
    )
