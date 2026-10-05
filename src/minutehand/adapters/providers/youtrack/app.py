"""The YouTrack REST API and the Hub routes beside it, as an ASGI app over the run's store and clock.

Every YouTrack route answers at `/api/...` (YouTrack Cloud on `*.youtrack.cloud`) and at `/youtrack/api/...` (the
`*.myjetbrains.com` instances); Hub answers at `/hub/api/rest/...` on the same host. Every YouTrack answer is
narrowed by `fields=`, every collection is paged by `$skip`/`$top`, and every refusal is a `wire.Refusal` raised out
of the app, which the guard (`adapters.answering`) renders as YouTrack's own `{"error": …, "error_description": …}`
with its status code. A path no route has is an operation the fake does not implement (501); a method a routed path
does not take is YouTrack's own 405.

Each request acts as the user its token names (`access.Access`), and is refused 403 where that user lacks the
permission. A fault the scenario seeded answers in place of the route while it lasts.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters.answering import unrouted
from minutehand.adapters.providers.youtrack import fields, state, wire
from minutehand.adapters.providers.youtrack.access import Access, fault_for
from minutehand.adapters.providers.youtrack.activities import Feed, categories
from minutehand.adapters.providers.youtrack.hub import hub_routes
from minutehand.adapters.providers.youtrack.present import Presenter
from minutehand.adapters.providers.youtrack.query import ME, Command, CommandWord, Keyword, parse_command, parse_search
from minutehand.adapters.providers.youtrack.search import Matcher
from minutehand.adapters.providers.youtrack.seed import new_project
from minutehand.adapters.providers.youtrack.state import YouTrackWorld
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

PREFIXES = ("/api", "/youtrack/api")
HUB = "/hub/api/rest"
_ENTITY_ID = re.compile(r"\d+-\d+")
"""YouTrack's database id. A short name or a readable id in an `{"id": …}` slot is refused before any lookup."""
_SHORT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_STOCK_TEMPLATES = ("scrum", "kanban")
"""What `POST /admin/projects?template=` takes: YouTrack's own templates, none of which carries a Due Date, so a
project made from one carries the default template's fields here."""


def param(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def header(request: Request, name: str) -> str | None:
    return request.headers[name] if name in request.headers else None


class Call:
    """One request as a handler sees it: who made it, its body, and the moment it arrived."""

    def __init__(self, request: Request, raw: bytes, caller: wire.StoredUser | None, now: int) -> None:
        self.request = request
        self.raw = raw
        self._caller = caller
        self.now = now

    @property
    def caller(self) -> wire.StoredUser:
        if self._caller is None:
            raise wire.unauthorized()
        return self._caller

    def path(self, name: str) -> str:
        return str(self.request.path_params[name])

    def param(self, name: str) -> str | None:
        return param(self.request, name)

    def header(self, name: str) -> str | None:
        return header(self.request, name)


Answered = tuple[int, bytes]
Handler = Callable[[Call], Answered]


class YouTrackApi:
    def __init__(self, store: Store, clock: Clock) -> None:
        self.world = YouTrackWorld(store)
        self.access = Access(self.world)
        self._clock = clock

    def now(self) -> int:
        return state.millis(self._clock.now())

    def endpoint(
        self, handler: Handler, *, fault_path: Callable[[Request], str], open_route: bool = False
    ) -> Callable[[Request], Awaitable[Response]]:
        async def answer(request: Request) -> Response:
            now = self.now()
            fault = fault_for(self.world, request.method, fault_path(request), now)
            if fault is not None:
                raise fault
            caller = None if open_route else self.access.caller(header(request, "authorization"), now)
            status, payload = handler(Call(request, await request.body(), caller, now))
            return Response(payload, status_code=status, media_type=wire.JSON)

        return answer

    def presenter(self) -> Presenter:
        return Presenter(self.world)

    def answer(self, call: Call, answer: wire.Answer | Sequence[wire.Answer]) -> Answered:
        return 200, wire.render(answer, wire.parse_fields(call.param("fields")))

    def page[T](self, call: Call, items: list[T]) -> list[T]:
        start, limit = wire.page_bounds(call.param("$skip"), call.param("$top"))
        return items[start:] if limit is None else items[start : start + limit]

    # ------------------------------------------------------------------ lookups

    def issue(self, call: Call, *, permission: wire.Permission = wire.Permission.READ_ISSUE) -> wire.StoredIssue:
        reference = call.path("issue")
        found = self.world.find_issue(reference)
        if found is None:
            raise wire.not_found(reference)
        home = self.home(found)
        self.access.require(call.caller, wire.Permission.READ_ISSUE, home)
        self.access.require(call.caller, permission, home)
        return found

    def home(self, issue: wire.StoredIssue) -> wire.StoredProject:
        project = self.world.project(issue.project)
        if project is None:
            raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
        return project

    def project(self, call: Call, *, permission: wire.Permission = wire.Permission.READ_PROJECT) -> wire.StoredProject:
        reference = call.path("project")
        found = self.world.project(reference) or self.world.project_named(reference)
        if found is None:
            raise wire.not_found(reference)
        self.access.require(call.caller, wire.Permission.READ_PROJECT, found)
        self.access.require(call.caller, permission, found)
        return found

    def project_field(self, call: Call, project: wire.StoredProject) -> wire.StoredProjectField:
        reference = call.path("field")
        found = next((f for f in project.fields if f.id == reference), None)
        if found is None:
            raise wire.not_found(reference)
        return found

    def definition_of(self, field: wire.StoredProjectField) -> wire.StoredFieldDefinition:
        definition = self.world.definition(field.field)
        if definition is None:
            raise LookupError(f"project field {field.id} names field {field.field}, which the instance has not got")
        return definition

    def entity_id(self, reference: wire.EntityIn | None, what: str) -> str:
        """The database id a body's `{"id": …}` names, refused by its shape before anything is looked up."""
        if reference is None or reference.id is None:
            raise wire.bad_request(f"{what} is required")
        if not _ENTITY_ID.fullmatch(reference.id):
            raise wire.invalid_entity_id(reference.id)
        return reference.id

    # ------------------------------------------------------------------ users

    def me(self, call: Call) -> Answered:
        self.world.saw(state.user_ref(call.caller.id), Operation.READ)
        return self.answer(call, self.presenter().me(call.caller))

    def users(self, call: Call) -> Answered:
        wanted = (call.param("query") or "").strip().lower()
        found = [
            u
            for u in self.world.users()
            if not wanted
            or wanted in u.login.lower()
            or wanted in u.fullName.lower()
            or (u.shown_email is not None and wanted in u.shown_email.lower())
        ]
        present = self.presenter()
        page: list[wire.Answer] = [present.user(u) for u in self.page(call, found)]
        self.world.saw(state.instance_ref(), Operation.SEARCH)
        return self.answer(call, page)

    def user(self, call: Call) -> Answered:
        reference = call.path("user")
        if reference == ME:
            return self.me(call)
        if not _ENTITY_ID.fullmatch(reference):
            raise wire.invalid_entity_id(reference)
        found = self.world.user(reference)
        if found is None:
            raise wire.not_found(reference)
        self.world.saw(state.user_ref(found.id), Operation.READ)
        return self.answer(call, self.presenter().user(found))

    # ------------------------------------------------------------------ projects

    def projects(self, call: Call) -> Answered:
        visible = [p for p in self.world.projects() if self.access.holds(call.caller, wire.Permission.READ_PROJECT, p)]
        present = self.presenter()
        page: list[wire.Answer] = [present.project(p) for p in self.page(call, visible)]
        self.world.saw(state.instance_ref(), Operation.SEARCH)
        return self.answer(call, page)

    def project_read(self, call: Call) -> Answered:
        project = self.project(call)
        self.world.saw(state.project_ref(project.id), Operation.READ)
        return self.answer(call, self.presenter().project(project))

    def project_create(self, call: Call) -> Answered:
        self.access.require(call.caller, wire.Permission.CREATE_PROJECT, None)
        template = call.param("template")
        if template is not None and template not in _STOCK_TEMPLATES:
            raise wire.bad_request(
                f"Unknown project template: {template}. Possible values: {', '.join(_STOCK_TEMPLATES)}"
            )
        body = wire.read_body(wire.ProjectCreateIn, call.raw)
        name = (body.name or "").strip()
        key = (body.shortName or "").strip()
        if not name:
            raise wire.bad_request("Project name is required")
        if not key:
            raise wire.bad_request("Project shortName is required")
        if not _SHORT_NAME.fullmatch(key):
            raise wire.bad_request(
                f"Invalid project shortName: {key}. Only letters, digits and underscores are allowed, starting with "
                "a letter"
            )
        leader = self.world.user(self.entity_id(body.leader, "Project leader"))
        if leader is None:
            raise wire.not_found(body.leader.id if body.leader is not None and body.leader.id else "")
        projects = self.world.projects()
        if any(p.shortName.lower() == key.lower() for p in projects):
            raise wire.bad_request(f"Project with shortName {key} already exists")
        ids = fields.Ids(projects)
        number = max((state.ordinal(p.id)[1] for p in projects), default=-1) + 1
        made = new_project(
            number,
            name,
            key,
            leader=leader.id,
            created_by=call.caller.id,
            team=[leader.id],
            project_fields=[fields.standard_field(ids, d, template=True) for d in fields.TEMPLATE_SET],
            description=body.description or "",
            created_through_api=True,
        )
        self.world.write_project(made, actor=Actor.AGENT)
        return self.answer(call, self.presenter().project(made))

    def project_fields(self, call: Call) -> Answered:
        project = self.project(call)
        present = self.presenter()
        page: list[wire.Answer] = [present.project_field(project, f) for f in self.page(call, project.fields)]
        self.world.saw(state.project_ref(project.id), Operation.READ)
        return self.answer(call, page)

    def project_field_read(self, call: Call) -> Answered:
        project = self.project(call)
        field = self.project_field(call, project)
        self.world.saw(state.project_ref(project.id), Operation.READ)
        return self.answer(call, self.presenter().project_field(project, field))

    def project_field_attach(self, call: Call) -> Answered:
        project = self.project(call, permission=wire.Permission.UPDATE_PROJECT)
        body = wire.read_body(wire.ProjectFieldIn, call.raw)
        definition = self.world.definition(self.entity_id(body.field, "field"))
        if definition is None:
            raise wire.not_found(body.field.id if body.field is not None and body.field.id else "")
        expected = wire.PROJECT_FIELD_TYPES[definition.fieldType]
        if body.type_ != expected:
            raise wire.bad_request(
                f"Unknown entity type: {body.type_}. A {definition.fieldType.value} field is attached as {expected}"
            )
        if any(f.field == definition.id for f in project.fields):
            raise wire.bad_request(f"Custom field {definition.name} is already present in project {project.shortName}")
        ids = fields.Ids(self.world.projects())
        values: list[fields.Value] = []
        if definition.fieldType in wire.BUNDLED:
            shared = self._bundle_values(body.bundle, definition)
            values = [fields.Value(name=v.name, resolved=v.isResolved, outcome=v.outcome) for v in shared]
        attached = fields.project_field(
            ids,
            definition,
            values=values,
            can_be_empty=True if body.canBeEmpty is None else body.canBeEmpty,
            empty_text=body.emptyFieldText or "No value",
            default=None,
        )
        changed = project.model_copy(update={"fields": [*project.fields, attached]})
        self.world.write_project(changed, actor=Actor.AGENT)
        return self.answer(call, self.presenter().project_field(changed, attached))

    def _bundle_values(
        self, bundle: wire.EntityIn | None, definition: wire.StoredFieldDefinition
    ) -> list[wire.StoredBundleValue]:
        """A bundled field is attached with the values of a bundle the instance has: the request names it."""
        if bundle is None or bundle.id is None:
            raise wire.bad_request(
                f"Custom field {definition.name} is of type {definition.fieldType.value} and cannot be added to a "
                "project without its bundle"
            )
        for project in self.world.projects():
            for field in project.fields:
                if field.bundle == bundle.id and field.field == definition.id:
                    return field.values
        raise wire.not_found(bundle.id)

    def project_team(self, call: Call) -> Answered:
        project = self.project(call)
        self.world.saw(state.project_ref(project.id), Operation.READ)
        return self.answer(call, self.presenter().team_group(project))

    def project_team_add(self, call: Call) -> Answered:
        """405: YouTrack does not serve this route (measured on a live instance); Hub owns a project's team."""
        raise wire.Refusal(405, "Method Not Allowed", "Method Not Allowed")

    # ------------------------------------------------------------------ the instance's fields and bundles

    def definitions(self, call: Call) -> Answered:
        present = self.presenter()
        page: list[wire.Answer] = [present.definition(d) for d in self.page(call, self.world.definitions())]
        self.world.saw(state.instance_ref(), Operation.SEARCH)
        return self.answer(call, page)

    def definition_read(self, call: Call) -> Answered:
        reference = call.path("field")
        found = self.world.definition(reference)
        if found is None:
            raise wire.not_found(reference)
        return self.answer(call, self.presenter().definition(found))

    def definition_create(self, call: Call) -> Answered:
        body = wire.read_body(wire.FieldDefinitionIn, call.raw)
        name = (body.name or "").strip()
        if not name:
            raise wire.bad_request("Custom field name is required")
        if body.fieldType is None or body.fieldType.id is None:
            raise wire.bad_request("fieldType is required")
        try:
            kind = wire.FieldType(body.fieldType.id)
        except ValueError as error:
            raise wire.not_found(body.fieldType.id) from error
        if self.world.definition_named(name) is not None:
            raise wire.bad_request(f"Custom field with name {name} already exists")
        made = wire.StoredFieldDefinition(id=self.world.next_id(58), name=name, fieldType=kind)
        self.world.write_definition(made, actor=Actor.AGENT)
        return self.answer(call, self.presenter().definition(made))

    def _bundles(self, kind: str) -> list[tuple[wire.StoredProject, wire.StoredProjectField]]:
        types = {
            "state": wire.FieldType.STATE,
            "enum": wire.FieldType.ENUM,
            "version": wire.FieldType.VERSION,
            "user": wire.FieldType.USER,
        }
        if kind not in types:
            raise wire.not_found(kind)
        seen: set[str] = set()
        found: list[tuple[wire.StoredProject, wire.StoredProjectField]] = []
        for project in self.world.projects():
            for field in project.fields:
                if field.bundle is None or field.bundle in seen:
                    continue
                if self.definition_of(field).fieldType is types[kind]:
                    seen.add(field.bundle)
                    found.append((project, field))
        return found

    def bundles(self, call: Call) -> Answered:
        present = self.presenter()
        made = [present.bundle(p, f) for p, f in self._bundles(call.path("kind"))]
        page: list[wire.Answer] = [b for b in self.page(call, made) if b is not None]
        return self.answer(call, page)

    def _bundle(self, call: Call) -> tuple[wire.StoredProject, wire.StoredProjectField]:
        reference = call.path("bundle")
        found = next(((p, f) for p, f in self._bundles(call.path("kind")) if f.bundle == reference), None)
        if found is None:
            raise wire.not_found(reference)
        return found

    def bundle_read(self, call: Call) -> Answered:
        project, field = self._bundle(call)
        bundle = self.presenter().bundle(project, field)
        if bundle is None:
            raise wire.not_found(call.path("bundle"))
        return self.answer(call, bundle)

    def bundle_values(self, call: Call) -> Answered:
        _, field = self._bundle(call)
        present = self.presenter()
        kind = self.definition_of(field).fieldType
        page: list[wire.Answer] = [present.bundle_value(kind, v) for v in self.page(call, field.values)]
        return self.answer(call, page)

    def bundle_value_add(self, call: Call) -> Answered:
        """A new value in a bundle: every project field drawing on that bundle gains it."""
        _, field = self._bundle(call)
        kind = self.definition_of(field).fieldType
        if kind not in wire.BUNDLED:
            raise wire.Refusal(405, "Method Not Allowed", "Method Not Allowed")
        body = wire.read_body(wire.EntityIn, call.raw)
        name = (body.name or "").strip()
        if not name:
            raise wire.bad_request("name is required")
        if any(v.name.lower() == name.lower() for v in field.values):
            raise wire.bad_request(f"Value {name} already exists in the bundle")
        ids = fields.Ids(self.world.projects())
        added = wire.StoredBundleValue(id=ids.take("62"), name=name, ordinal=len(field.values))
        for project in self.world.projects():
            if not any(f.bundle == field.bundle for f in project.fields):
                continue
            self.access.require(call.caller, wire.Permission.UPDATE_PROJECT, project)
            grown = [
                f.model_copy(update={"values": [*f.values, added]}) if f.bundle == field.bundle else f
                for f in project.fields
            ]
            self.world.write_project(project.model_copy(update={"fields": grown}), actor=Actor.AGENT)
        return self.answer(call, self.presenter().bundle_value(kind, added))

    # ------------------------------------------------------------------ issues: reads

    def search(self, call: Call) -> Answered:
        matched, scope = self._matching(call, call.param("query"))
        present = self.presenter()
        page: list[wire.Answer] = [present.issue(i) for i in self.page(call, matched)]
        if len(scope) == 1:
            self.world.saw(state.project_ref(scope[0].id), Operation.SEARCH)
        else:
            self.world.saw(state.instance_ref(), Operation.SEARCH)
        return self.answer(call, page)

    def _matching(self, call: Call, query: str | None) -> tuple[list[wire.StoredIssue], list[wire.StoredProject]]:
        matcher = Matcher(self.world, call.caller, self._clock.now(), query or "")
        parsed = parse_search(query, matcher.field_names())
        readable = self.access.readable(call.caller)
        matched = matcher.matching(parsed, readable)
        named = {
            item.text.lower()
            for c in parsed.alternatives
            for clause in c.clauses
            if clause.keyword is Keyword.PROJECT
            for item in clause.items
        }
        scope = [p for p in readable if p.shortName.lower() in named or p.name.lower() in named] or readable
        return matched, scope

    def count(self, call: Call) -> Answered:
        body = wire.read_body(wire.CountIn, call.raw)
        matched, _ = self._matching(call, body.query)
        self.world.saw(state.instance_ref(), Operation.SEARCH)
        unknown = self.world.settings().countUnknown
        return self.answer(call, wire.CountOut(count=-1 if unknown else len(matched)))

    def issue_read(self, call: Call) -> Answered:
        issue = self.issue(call)
        self.world.saw(state.issue_ref(issue.id), Operation.READ)
        return self.answer(call, self.presenter().issue(issue))

    def issue_fields(self, call: Call) -> Answered:
        issue = self.issue(call)
        present = self.presenter()
        project = self.home(issue)
        page: list[wire.Answer] = list(self.page(call, present.issue_fields(project, issue)))
        self.world.saw(state.issue_ref(issue.id), Operation.READ)
        return self.answer(call, page)

    def issue_field_read(self, call: Call) -> Answered:
        issue = self.issue(call)
        project = self.home(issue)
        field = self.project_field(call, project)
        self.world.saw(state.issue_ref(issue.id), Operation.READ)
        return self.answer(call, self.presenter().issue_field(project, issue, field))

    def comments(self, call: Call) -> Answered:
        issue = self.issue(call)
        present = self.presenter()
        page: list[wire.Answer] = [present.comment(c, issue) for c in self.page(call, self.world.comments(issue.id))]
        self.world.saw(state.issue_ref(issue.id), Operation.READ)
        return self.answer(call, page)

    def comment_read(self, call: Call) -> Answered:
        issue = self.issue(call)
        reference = call.path("comment")
        found = next((c for c in self.world.comments(issue.id) if c.id == reference), None)
        if found is None:
            raise wire.not_found(reference)
        self.world.saw(state.issue_ref(issue.id), Operation.READ)
        return self.answer(call, self.presenter().comment(found, issue))

    def activities(self, call: Call) -> Answered:
        issue = self.issue(call)
        wanted = categories(call.param("categories"))
        found = Feed(self.world, self.presenter()).of(issue, wanted)
        author = call.param("author")
        if author is not None:
            found = [a for a in found if author in (a.author.id, a.author.login)]
        start, end = call.param("start"), call.param("end")
        try:
            if start is not None:
                found = [a for a in found if a.timestamp >= int(start)]
            if end is not None:
                found = [a for a in found if a.timestamp <= int(end)]
        except ValueError as error:
            raise wire.bad_request("start and end take epoch milliseconds") from error
        if (call.param("reverse") or "").lower() == "true":
            found.reverse()
        self.world.saw(state.issue_ref(issue.id), Operation.READ)
        return self.answer(call, list(self.page(call, found)))

    # ------------------------------------------------------------------ issues: writes

    def create(self, call: Call) -> Answered:
        body = wire.read_body(wire.IssueCreateIn, call.raw)
        project_id = self.entity_id(body.project, "project")
        project = self.world.project(project_id)
        if project is None:
            raise wire.not_found(project_id)
        self.access.require(call.caller, wire.Permission.CREATE_ISSUE, project)
        if body.summary is None or not body.summary.strip():
            raise wire.bad_request("summary is required")
        number = self.world.next_number(project.id)
        issue = wire.StoredIssue(
            id=self.world.next_id(2),
            idReadable=f"{project.shortName}-{number}",
            numberInProject=number,
            project=project.id,
            summary=body.summary,
            description=body.description,
            reporter=call.caller.id,
            updater=call.caller.id,
            created=call.now,
            updated=call.now,
            values={f.id: f.defaultValue for f in project.fields if f.defaultValue is not None},
        )
        written = self._with_fields(project, issue, body.customFields)
        written = written.model_copy(update={"tags": self._tags_named(body.tags)})
        self._require_filled(project, written)
        written = written.model_copy(
            update={"resolved": call.now if self.world.is_resolved(project, written) else None}
        )
        self.world.create_issue(written, actor=Actor.AGENT)
        return self.answer(call, self.presenter().issue(written))

    def _require_filled(self, project: wire.StoredProject, issue: wire.StoredIssue) -> None:
        for field in project.fields:
            if not field.canBeEmpty and field.id not in issue.values:
                raise wire.bad_request(f"{self.definition_of(field).name} is required")

    def _tags_named(self, tags: list[wire.EntityIn]) -> list[str]:
        named: list[str] = []
        for reference in tags:
            tag_id = self.entity_id(reference, "tag")
            if self.world.tag(tag_id) is None:
                raise wire.not_found(tag_id)
            if tag_id not in named:
                named.append(tag_id)
        return named

    def update(self, call: Call) -> Answered:
        issue = self.issue(call, permission=wire.Permission.UPDATE_ISSUE)
        body = wire.read_body(wire.IssueUpdateIn, call.raw)
        project = self.home(issue)
        changed = issue
        if "summary" in body.model_fields_set:
            if body.summary is None or not body.summary.strip():
                raise wire.bad_request("summary is required")
            changed = changed.model_copy(update={"summary": body.summary})
        if "description" in body.model_fields_set:
            changed = changed.model_copy(update={"description": body.description})
        changed = self._with_fields(project, changed, body.customFields)
        if "tags" in body.model_fields_set:
            changed = changed.model_copy(update={"tags": self._tags_named(body.tags)})
        changed = self._written(project, issue, changed, call)
        return self.answer(call, self.presenter().issue(changed))

    def _written(
        self, project: wire.StoredProject, before: wire.StoredIssue, after: wire.StoredIssue, call: Call
    ) -> wire.StoredIssue:
        """Keep the change, stamped as the caller's, when it changed anything; answer the issue as it stands."""
        if not _differs(before, after):
            return before
        settled = self.world.settled(after, project, was=before, by=call.caller.id, at=call.now)
        self.world.update_issue(settled, actor=Actor.AGENT)
        return settled

    def field_write(self, call: Call) -> Answered:
        issue = self.issue(call, permission=wire.Permission.UPDATE_ISSUE)
        project = self.home(issue)
        field = self.project_field(call, project)
        body = wire.read_body(wire.FieldValueWriteIn, call.raw)
        value = self._coerced(project, field, body.value)
        values = {k: v for k, v in issue.values.items() if k != field.id}
        if value is not None:
            values[field.id] = value
        changed = self._written(project, issue, issue.model_copy(update={"values": values}), call)
        return self.answer(call, self.presenter().issue_field(project, changed, field))

    def _with_fields(
        self, project: wire.StoredProject, issue: wire.StoredIssue, writes: list[wire.CustomFieldIn]
    ) -> wire.StoredIssue:
        """The issue with a `customFields` block applied, every write checked before any is kept."""
        values = dict(issue.values)
        for write in writes:
            field = next((f for f in project.fields if write.id is not None and f.id == write.id), None)
            if field is None and write.name is not None:
                field = self.world.project_field(project, write.name)
            if field is None:
                raise wire.Refusal(404, "Not Found", f"Entity with name {write.name or write.id} not found")
            value = self._coerced(project, field, write.value)
            if value is None:
                values.pop(field.id, None)
            else:
                values[field.id] = value
        return issue.model_copy(update={"values": values})

    def _coerced(
        self, project: wire.StoredProject, field: wire.StoredProjectField, value: wire.FieldValueIn | None
    ) -> wire.FieldValue | None:
        """What a write to the field stores; None clears it. A value the field will not take is refused."""
        if value is None:
            if not field.canBeEmpty:
                raise wire.value_not_allowed()
            return None
        if isinstance(value, bool):
            raise wire.value_not_allowed()
        kind = self.definition_of(field).fieldType
        if kind in wire.BUNDLED:
            if not isinstance(value, wire.ValueIn) or (value.id is None and value.name is None):
                raise wire.value_not_allowed()
            found = next(
                (
                    v
                    for v in field.values
                    if (value.id is not None and v.id == value.id)
                    or (value.name is not None and v.name.lower() == value.name.strip().lower())
                ),
                None,
            )
            if found is None:
                raise wire.value_not_allowed()
            return found.id
        if kind is wire.FieldType.USER:
            if not isinstance(value, wire.ValueIn):
                raise wire.value_not_allowed()
            user = (
                self.world.user(value.id)
                if value.id is not None
                else self.world.user_by_login(value.login)
                if value.login is not None
                else None
            )
            if user is None or user.banned or user.id not in project.team:
                raise wire.value_not_allowed()
            return user.id
        if kind in wire.DATED:
            if not isinstance(value, int):
                raise wire.value_not_allowed()
            return value
        if kind is wire.FieldType.PERIOD:
            if not isinstance(value, wire.ValueIn):
                raise wire.value_not_allowed()
            minutes = value.minutes
            if minutes is None and value.presentation is not None:
                minutes = fields.period_minutes(value.presentation)
            if minutes is None or minutes < 0:
                raise wire.value_not_allowed()
            return minutes
        if kind is wire.FieldType.FLOAT and isinstance(value, int | float):
            return float(value)
        if kind is wire.FieldType.INTEGER and isinstance(value, int):
            return value
        if kind is wire.FieldType.STRING and isinstance(value, str):
            return value
        raise wire.value_not_allowed()

    def delete(self, call: Call) -> Answered:
        issue = self.issue(call, permission=wire.Permission.DELETE_ISSUE)
        self.world.delete_issue(issue, by=call.caller.id, at=call.now, actor=Actor.AGENT)
        return 200, b""

    def comment(self, call: Call) -> Answered:
        issue = self.issue(call, permission=wire.Permission.UPDATE_ISSUE)
        body = wire.read_body(wire.CommentIn, call.raw)
        if body.text is None or not body.text.strip():
            raise wire.bad_request("text is required")
        written = self._comment(issue, body.text, call.caller, call.now)
        return self.answer(call, self.presenter().comment(written, issue))

    def _comment(self, issue: wire.StoredIssue, text: str, author: wire.StoredUser, at: int) -> wire.StoredComment:
        comment = wire.StoredComment(id=self.world.next_id(4), issue=issue.id, text=text, author=author.id, created=at)
        self.world.write_comment(comment, actor=Actor.AGENT)
        return comment

    # ------------------------------------------------------------------ tags

    def tags(self, call: Call) -> Answered:
        present = self.presenter()
        page: list[wire.Answer] = [present.tag(t) for t in self.page(call, self.world.tags())]
        self.world.saw(state.instance_ref(), Operation.SEARCH)
        return self.answer(call, page)

    def tag_read(self, call: Call) -> Answered:
        reference = call.path("tag")
        found = self.world.tag(reference)
        if found is None:
            raise wire.not_found(reference)
        return self.answer(call, self.presenter().tag(found))

    def tag_create(self, call: Call) -> Answered:
        body = wire.read_body(wire.TagIn, call.raw)
        name = (body.name or "").strip()
        if not name:
            raise wire.bad_request("name is required")
        if self.world.tag_named(name) is not None:
            raise wire.bad_request(f"Tag {name} already exists")
        tag = wire.StoredTag(id=self.world.next_id(6), name=name, owner=call.caller.id)
        self.world.write_tag(tag, actor=Actor.AGENT)
        return self.answer(call, self.presenter().tag(tag))

    def issue_tags(self, call: Call) -> Answered:
        issue = self.issue(call)
        present = self.presenter()
        tags = [t for t in (self.world.tag(i) for i in issue.tags) if t is not None]
        page: list[wire.Answer] = [present.tag(t) for t in self.page(call, tags)]
        self.world.saw(state.issue_ref(issue.id), Operation.READ)
        return self.answer(call, page)

    def issue_tag_add(self, call: Call) -> Answered:
        """An issue carries an EXISTING tag, named by its id: a name in that slot tags nothing and makes nothing."""
        issue = self.issue(call, permission=wire.Permission.UPDATE_ISSUE)
        body = wire.read_body(wire.TagIn, call.raw)
        tag_id = self.entity_id(wire.EntityIn(id=body.id), "id")
        tag = self.world.tag(tag_id)
        if tag is None:
            raise wire.not_found(tag_id)
        if tag.id not in issue.tags:
            self._written(self.home(issue), issue, issue.model_copy(update={"tags": [*issue.tags, tag.id]}), call)
        return self.answer(call, self.presenter().tag(tag))

    def issue_tag_remove(self, call: Call) -> Answered:
        issue = self.issue(call, permission=wire.Permission.UPDATE_ISSUE)
        tag = self.world.tag(call.path("tag"))
        if tag is None:
            raise wire.not_found(call.path("tag"))
        if tag.id in issue.tags:
            remaining = [t for t in issue.tags if t != tag.id]
            self._written(self.home(issue), issue, issue.model_copy(update={"tags": remaining}), call)
        return 200, b""

    # ------------------------------------------------------------------ links

    def link_types(self, call: Call) -> Answered:
        present = self.presenter()
        page: list[wire.Answer] = [present.link_type(t) for t in self.page(call, self.world.link_types())]
        return self.answer(call, page)

    def links(self, call: Call) -> Answered:
        issue = self.issue(call)
        self.world.saw(state.issue_ref(issue.id), Operation.READ)
        page: list[wire.Answer] = list(self.page(call, self.presenter().links(issue)))
        return self.answer(call, page)

    def link_post_on_collection(self, call: Call) -> Answered:
        """There is no POST on the links collection: a link is added to one slot's issues."""
        raise wire.Refusal(405, "Method Not Allowed", "Method Not Allowed")

    def _slot(self, slot: str) -> tuple[wire.StoredLinkType, bool]:
        """A link slot id as its type and whether this issue is the link's source. A directed type is addressed
        `<type>s` or `<type>t`; an undirected one by its own id, and a marker on it names nothing."""
        types = {t.id: t for t in self.world.link_types()}
        if slot in types:
            if types[slot].directed:
                raise wire.not_found(slot)
            return types[slot], True
        marker, base = slot[-1:], slot[:-1]
        if marker not in ("s", "t") or base not in types or not types[base].directed:
            raise wire.not_found(slot)
        return types[base], marker == "s"

    def link_slot(self, call: Call) -> Answered:
        issue = self.issue(call)
        found = next((s for s in self.presenter().links(issue) if s.id == call.path("link")), None)
        if found is None:
            raise wire.not_found(call.path("link"))
        return self.answer(call, found)

    def link_slot_issues(self, call: Call) -> Answered:
        issue = self.issue(call)
        found = next((s for s in self.presenter().links(issue) if s.id == call.path("link")), None)
        if found is None:
            raise wire.not_found(call.path("link"))
        self.world.saw(state.issue_ref(issue.id), Operation.READ)
        return 200, wire.render(list(self.page(call, found.issues)), wire.parse_fields(call.param("fields")))

    def link_add(self, call: Call) -> Answered:
        issue = self.issue(call, permission=wire.Permission.UPDATE_ISSUE)
        link_type, outward = self._slot(call.path("link"))
        body = wire.read_body(wire.EntityIn, call.raw)
        other_id = self.entity_id(body, "id")
        other = self.world.issue(other_id)
        if other is None:
            raise wire.not_found(other_id)
        if other.id == issue.id:
            raise wire.bad_request("An issue cannot be linked to itself")
        source, target = (issue, other) if outward else (other, issue)
        held = self.world.links()
        if not any(e.source == source.id and e.target == target.id and e.linkType == link_type.id for e in held):
            self.world.write_link(
                wire.StoredLink(
                    source=source.id, linkType=link_type.id, target=target.id, created=call.now, author=call.caller.id
                ),
                actor=Actor.AGENT,
            )
        return 200, b""

    def link_remove(self, call: Call) -> Answered:
        issue = self.issue(call, permission=wire.Permission.UPDATE_ISSUE)
        link_type, outward = self._slot(call.path("link"))
        other = call.path("target")
        source, target = (issue.id, other) if outward else (other, issue.id)
        found = next(
            (
                e
                for e in self.world.links()
                if e.linkType == link_type.id
                and (
                    (e.source, e.target) == (source, target)
                    or (not link_type.directed and (e.source, e.target) == (target, source))
                )
            ),
            None,
        )
        if found is None:
            raise wire.not_found(other)
        self.world.remove_link(found, by=call.caller.id, at=call.now, actor=Actor.AGENT)
        return 200, b""

    # ------------------------------------------------------------------ commands

    def command(self, call: Call) -> Answered:
        body = wire.read_body(wire.CommandIn, call.raw)
        if not body.issues:
            parse_command(body.query, [d.name for d in self.world.definitions()])
            raise wire.bad_request("issues are required")
        targets: list[wire.StoredIssue] = []
        for named in body.issues:
            reference = named.id or named.idReadable
            found = self.world.find_issue(reference) if reference else None
            if found is None:
                raise wire.not_found(reference or "")
            self.access.require(call.caller, wire.Permission.UPDATE_ISSUE, self.home(found))
            targets.append(found)
        names = [d.name for d in self.world.definitions()]
        commands = parse_command(body.query, names)
        planned = [(issue, self._commanded(self.home(issue), issue, commands, call)) for issue in targets]
        for before, after in planned:
            self._written(self.home(before), before, after, call)
            if body.comment is not None and body.comment.strip():
                self._comment(before, body.comment, call.caller, call.now)
        answer = wire.CommandListOut(
            query=body.query or "",
            issues=[wire.IssueRefOut(id=i.id, idReadable=i.idReadable) for i in targets],
            commands=[wire.ParsedCommandOut(description=f"{c.field or c.word.value} {c.value}") for c in commands],
            comment=body.comment,
            silent=body.silent,
        )
        return self.answer(call, answer)

    def _commanded(
        self, project: wire.StoredProject, issue: wire.StoredIssue, commands: list[Command], call: Call
    ) -> wire.StoredIssue:
        for command in commands:
            if command.word is CommandWord.TAG or command.word is CommandWord.UNTAG:
                tag = self.world.tag_named(command.value)
                if tag is None:
                    raise wire.bad_request(f"Unknown tag: {command.value}")
                kept = [t for t in issue.tags if t != tag.id]
                issue = issue.model_copy(update={"tags": [*kept, tag.id] if command.word is CommandWord.TAG else kept})
                continue
            name = state.ASSIGNEE_FIELD if command.word is CommandWord.FOR else command.field or ""
            field = self.world.project_field(project, name)
            if field is None:
                raise wire.bad_request(f"Unknown command: {name} {command.value}")
            kind = self.definition_of(field).fieldType
            text = command.value
            written: wire.FieldValueIn
            if kind is wire.FieldType.USER:
                written = wire.ValueIn(login=call.caller.login if text.lower() == ME else text)
            elif kind in wire.BUNDLED:
                written = wire.ValueIn(name=text)
            elif kind is wire.FieldType.PERIOD:
                written = wire.ValueIn(presentation=text)
            else:
                written = text
            value = self._coerced(project, field, written)
            values = {k: v for k, v in issue.values.items() if k != field.id}
            if value is not None:
                values[field.id] = value
            issue = issue.model_copy(update={"values": values})
        return issue


def _differs(before: wire.StoredIssue, after: wire.StoredIssue) -> bool:
    keep = {"updated", "updater", "resolved"}
    return before.model_dump(exclude=keep) != after.model_dump(exclude=keep)


def build_app(store: Store, clock: Clock) -> Starlette:
    api = YouTrackApi(store, clock)
    table: list[tuple[str, str, Handler]] = [
        ("/users/me", "GET", api.me),
        ("/users", "GET", api.users),
        ("/users/{user}", "GET", api.user),
        ("/admin/projects", "GET", api.projects),
        ("/admin/projects", "POST", api.project_create),
        ("/admin/projects/{project}", "GET", api.project_read),
        ("/admin/projects/{project}/customFields", "GET", api.project_fields),
        ("/admin/projects/{project}/customFields", "POST", api.project_field_attach),
        ("/admin/projects/{project}/customFields/{field}", "GET", api.project_field_read),
        ("/admin/projects/{project}/team", "GET", api.project_team),
        ("/admin/projects/{project}/team/users", "POST", api.project_team_add),
        ("/admin/customFieldSettings/customFields", "GET", api.definitions),
        ("/admin/customFieldSettings/customFields", "POST", api.definition_create),
        ("/admin/customFieldSettings/customFields/{field}", "GET", api.definition_read),
        ("/admin/customFieldSettings/bundles/{kind}", "GET", api.bundles),
        ("/admin/customFieldSettings/bundles/{kind}/{bundle}", "GET", api.bundle_read),
        ("/admin/customFieldSettings/bundles/{kind}/{bundle}/values", "GET", api.bundle_values),
        ("/admin/customFieldSettings/bundles/{kind}/{bundle}/values", "POST", api.bundle_value_add),
        ("/issues", "GET", api.search),
        ("/issues", "POST", api.create),
        ("/issuesGetter/count", "POST", api.count),
        ("/issues/{issue}", "GET", api.issue_read),
        ("/issues/{issue}", "POST", api.update),
        ("/issues/{issue}", "DELETE", api.delete),
        ("/issues/{issue}/customFields", "GET", api.issue_fields),
        ("/issues/{issue}/customFields/{field}", "GET", api.issue_field_read),
        ("/issues/{issue}/customFields/{field}", "POST", api.field_write),
        ("/issues/{issue}/comments", "GET", api.comments),
        ("/issues/{issue}/comments", "POST", api.comment),
        ("/issues/{issue}/comments/{comment}", "GET", api.comment_read),
        ("/issues/{issue}/activities", "GET", api.activities),
        ("/issues/{issue}/tags", "GET", api.issue_tags),
        ("/issues/{issue}/tags", "POST", api.issue_tag_add),
        ("/issues/{issue}/tags/{tag}", "DELETE", api.issue_tag_remove),
        ("/issues/{issue}/links", "GET", api.links),
        ("/issues/{issue}/links", "POST", api.link_post_on_collection),
        ("/issues/{issue}/links/{link}", "GET", api.link_slot),
        ("/issues/{issue}/links/{link}/issues", "GET", api.link_slot_issues),
        ("/issues/{issue}/links/{link}/issues", "POST", api.link_add),
        ("/issues/{issue}/links/{link}/issues/{target}", "DELETE", api.link_remove),
        ("/issueLinkTypes", "GET", api.link_types),
        ("/tags", "GET", api.tags),
        ("/tags", "POST", api.tag_create),
        ("/tags/{tag}", "GET", api.tag_read),
        ("/commands", "POST", api.command),
    ]
    routes: list[Route] = []
    served: list[str] = []
    for prefix in PREFIXES:

        def youtrack_path(request: Request, prefix: str = prefix) -> str:
            return request.url.path[len(prefix) :]

        for path, methods in _by_path(table).items():
            answers = {m: api.endpoint(h, fault_path=youtrack_path) for m, h in methods.items()}
            routes.append(Route(prefix + path, _dispatch(answers), methods=list(_EVERY_METHOD)))
            served += [f"{m} {prefix}{path}" for m in methods]
    for path, methods, open_route in hub_routes(api):
        answers = {
            m: api.endpoint(h, fault_path=lambda r: r.url.path, open_route=open_route) for m, h in methods.items()
        }
        routes.append(Route(HUB + path, _dispatch(answers), methods=list(_EVERY_METHOD)))
        served += [f"{m} {HUB}{path}" for m in methods]

    async def unserved(request: Request) -> Response:
        """A path no route of the fake has: the real YouTrack may well serve it (`/api/agiles`), so it is answered as
        an operation the fake does not implement, never as YouTrack's 404."""
        raise unrouted(request.method, request.url.path, served)

    return Starlette(routes=[*routes, Route("/{anything:path}", unserved, methods=list(_EVERY_METHOD))])


def _by_path(table: list[tuple[str, str, Handler]]) -> dict[str, dict[str, Handler]]:
    by_path: dict[str, dict[str, Handler]] = {}
    for path, method, handler in table:
        by_path.setdefault(path, {})[method] = handler
    return by_path


_EVERY_METHOD = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


def _dispatch(answers: dict[str, Callable[[Request], Awaitable[Response]]]) -> Callable[[Request], Awaitable[Response]]:
    """The path's handler for the request's method; a method the path does not take is YouTrack's own 405, as the
    real service answers one on a resource it has (`PUT /api/issues/{id}`)."""

    async def route(request: Request) -> Response:
        method = "GET" if request.method == "HEAD" else request.method
        if method not in answers:
            raise wire.Refusal(405, "Method Not Allowed", f"{request.method} {request.url.path} is not served")
        return await answers[method](request)

    return route
