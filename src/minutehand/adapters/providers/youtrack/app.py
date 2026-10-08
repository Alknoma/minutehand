"""The YouTrack REST API and the Hub routes beside it, as an ASGI app over the run's store and clock.

Every YouTrack route answers at `/api/...` (YouTrack Cloud on `*.youtrack.cloud`) and at `/youtrack/api/...` (the
`*.myjetbrains.com` instances); Hub answers at `/hub/api/rest/...` on the same host. Every YouTrack answer is
narrowed by `fields=`, every collection is paged by `$skip`/`$top`, and every refusal is YouTrack's own
`{"error": …, "error_description": …}` with its status code.

Each request acts as the user its token names, or as the agent's account when it names none the instance knows
(`access.Access`): Minutehand never refuses a call for its credential or for a permission. A fault the scenario
seeded answers in place of the route while it lasts. An operation of YouTrack's API among the claimed resources that
the fake does not serve is a 501 naming it (`surface.UNSERVED`).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from minutehand.adapters import answering
from minutehand.adapters.providers.youtrack import fields, state, wire
from minutehand.adapters.providers.youtrack.access import Access, fault_for
from minutehand.adapters.providers.youtrack.activities import Feed, categories
from minutehand.adapters.providers.youtrack.hub import hub_routes
from minutehand.adapters.providers.youtrack.present import Presenter
from minutehand.adapters.providers.youtrack.query import ME, Command, CommandWord, Keyword, parse_command, parse_search
from minutehand.adapters.providers.youtrack.search import Matcher
from minutehand.adapters.providers.youtrack.seed import new_project
from minutehand.adapters.providers.youtrack.state import YouTrackWorld
from minutehand.adapters.providers.youtrack.surface import UNSERVED
from minutehand.domain.world import Actor, Operation
from minutehand.ports.clock import Clock
from minutehand.ports.store import Store

PREFIXES = ("/api", "/youtrack/api")
HUB = "/hub/api/rest"
JSON = "application/json;charset=UTF-8"
_ENTITY_ID = re.compile(r"\d+-\d+")
"""YouTrack's database id. A short name or a readable id in an `{"id": …}` slot is refused before any lookup."""


def param(request: Request, name: str) -> str | None:
    return request.query_params[name] if name in request.query_params else None


def header(request: Request, name: str) -> str | None:
    return request.headers[name] if name in request.headers else None


class Call:
    """One request as a handler sees it: who made it, its body, and the moment it arrived."""

    def __init__(self, request: Request, raw: bytes, caller: wire.StoredUser, now: int) -> None:
        self.request = request
        self.raw = raw
        self.caller = caller
        self.now = now

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
        self, handler: Handler, *, fault_path: Callable[[Request], str]
    ) -> Callable[[Request], Awaitable[Response]]:
        async def answer(request: Request) -> Response:
            try:
                now = self.now()
                fault = fault_for(self.world, request.method, fault_path(request), now)
                if fault is not None:
                    answering.injected()
                    raise fault
                caller = self.access.caller(header(request, "authorization"))
                status, payload = handler(Call(request, await request.body(), caller, now))
            except wire.Refusal as refusal:
                headers = {} if refusal.retry_after is None else {"Retry-After": str(refusal.retry_after)}
                return Response(wire.error_body(refusal), status_code=refusal.status, media_type=JSON, headers=headers)
            return Response(payload, status_code=status, media_type=JSON)

        return answer

    def presenter(self) -> Presenter:
        return Presenter(self.world)

    def answer(self, call: Call, answer: wire.Answer | Sequence[wire.Answer]) -> Answered:
        return 200, wire.render(answer, wire.parse_fields(call.param("fields")))

    def page[T](self, call: Call, items: list[T], *, recorded: bool = False) -> list[T]:
        start, limit = wire.page_bounds(call.param("$skip"), call.param("$top"), recorded=recorded)
        return items[start : start + limit]

    # ------------------------------------------------------------------ lookups

    def issue(self, call: Call) -> wire.StoredIssue:
        reference = call.path("issue")
        found = self.world.find_issue(reference)
        if found is None:
            raise wire.not_found(reference)
        return found

    def home(self, issue: wire.StoredIssue) -> wire.StoredProject:
        project = self.world.project(issue.project)
        if project is None:
            raise LookupError(f"{issue.idReadable} names project {issue.project}, which does not exist")
        return project

    def project(self, call: Call) -> wire.StoredProject:
        reference = call.path("project")
        found = self.world.project(reference) or self.world.project_named(reference)
        if found is None:
            raise wire.not_found(reference)
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
            raise _undocumented(f"a body without {what}")
        if not _ENTITY_ID.fullmatch(reference.id):
            raise wire.invalid_entity_id(reference.id)
        return reference.id

    # ------------------------------------------------------------------ users

    def me(self, call: Call) -> Answered:
        self.world.saw(state.user_ref(call.caller.id), Operation.READ)
        return self.answer(call, self.presenter().me(call.caller))

    def users(self, call: Call) -> Answered:
        """Every user, paged. YouTrack's description of `GET /users` takes no `query`, so a filter is refused by
        name rather than guessed; Hub's `/users?query=` is the documented search."""
        if (call.param("query") or "").strip():
            raise NotImplementedError("GET /users takes no query parameter in YouTrack's API description")
        present = self.presenter()
        page: list[wire.Answer] = [present.user(u) for u in self.page(call, self.world.users())]
        self.world.saw(state.instance_ref(), Operation.SEARCH)
        return self.answer(call, page)

    def user(self, call: Call) -> Answered:
        reference = call.path("user")
        if reference == ME:
            return self.me(call)
        found = self.world.user(reference) if _ENTITY_ID.fullmatch(reference) else self.world.user_by_login(reference)
        if found is None:
            raise wire.not_found(reference)
        self.world.saw(state.user_ref(found.id), Operation.READ)
        return self.answer(call, self.presenter().user(found))

    # ------------------------------------------------------------------ projects

    def projects(self, call: Call) -> Answered:
        visible = self.world.projects()
        present = self.presenter()
        page: list[wire.Answer] = [present.project(p) for p in self.page(call, visible)]
        self.world.saw(state.instance_ref(), Operation.SEARCH)
        return self.answer(call, page)

    def project_read(self, call: Call) -> Answered:
        project = self.project(call)
        self.world.saw(state.project_ref(project.id), Operation.READ)
        return self.answer(call, self.presenter().project(project))

    def project_create(self, call: Call) -> Answered:
        template = call.param("template")
        if template not in fields.TEMPLATES:
            raise NotImplementedError(
                f"POST /admin/projects?template={template}: the reference lists scrum and kanban; a custom project "
                "template is not served"
            )
        body = wire.read_body(wire.ProjectCreateIn, call.raw)
        name = body.name or ""
        key = body.shortName or ""
        if not name.strip():
            raise _undocumented("a project without a name")
        if not key.strip():
            raise _undocumented("a project without a shortName")
        if key.isdigit():
            raise wire.numeric_short_name()
        leader = self.world.user(self.entity_id(body.leader, "Project leader"))
        if leader is None:
            raise wire.not_found(body.leader.id if body.leader is not None and body.leader.id else "")
        projects = self.world.projects()
        if any(p.shortName.lower() == key.lower() for p in projects):
            raise _undocumented(f"a project whose shortName {key} another project holds")
        ids = fields.Ids(projects)
        number = max((state.ordinal(p.id)[1] for p in projects), default=-1) + 1
        made = new_project(
            number,
            name,
            key,
            leader=leader.id,
            created_by=call.caller.id,
            team=list(dict.fromkeys([leader.id, call.caller.id])),
            project_fields=[self._template_field(ids, f) for f in fields.TEMPLATES[template]],
            description=body.description or "",
            created_through_api=True,
        )
        self.world.write_project(made, actor=Actor.AGENT)
        return self.answer(call, self.presenter().project(made))

    def _template_field(self, ids: fields.Ids, field: fields.TemplateField) -> wire.StoredProjectField:
        """The template's field on a new project, drawing on the instance's field of that name, defined when the
        instance has none (as a template brings its fields with it)."""
        definition = self.world.definition_named(field.name)
        if definition is None:
            definition = wire.StoredFieldDefinition(id=self.world.next_id(58), name=field.name, fieldType=field.type)
            self.world.write_definition(definition, actor=Actor.AGENT)
        return fields.project_field(
            ids,
            definition,
            values=field.values,
            can_be_empty=field.can_be_empty,
            empty_text=field.empty_text,
            default=field.default,
        )

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
        project = self.project(call)
        body = wire.read_body(wire.ProjectFieldIn, call.raw)
        definition = self.world.definition(self.entity_id(body.field, "field"))
        if definition is None:
            raise wire.not_found(body.field.id if body.field is not None and body.field.id else "")
        expected = wire.PROJECT_FIELD_TYPES[definition.fieldType]
        if body.type_ != expected:
            raise _undocumented(f"attaching {definition.name} as {body.type_}, not as {expected}")
        if any(f.field == definition.id for f in project.fields):
            raise _undocumented(f"attaching {definition.name}, which {project.shortName} already carries")
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
            raise _undocumented(f"attaching the bundled field {definition.name} without naming its bundle")
        for project in self.world.projects():
            for field in project.fields:
                if field.bundle == bundle.id and field.field == definition.id:
                    return field.values
        raise wire.not_found(bundle.id)

    def project_team(self, call: Call) -> Answered:
        project = self.project(call)
        self.world.saw(state.project_ref(project.id), Operation.READ)
        return self.answer(call, self.presenter().team_group(project))

    def project_team_users(self, call: Call) -> Answered:
        project = self.project(call)
        present = self.presenter()
        page: list[wire.Answer] = [present.user(u) for u in self.page(call, self._team(project))]
        self.world.saw(state.project_ref(project.id), Operation.READ)
        return self.answer(call, page)

    def _team(self, project: wire.StoredProject) -> list[wire.StoredUser]:
        return [u for u in (self.world.user(m) for m in project.team) if u is not None]

    def project_team_add(self, call: Call) -> Answered:
        """A user joins the project's team directly, named by database id (`POST .../team/ownUsers`), as YouTrack
        2026.1 and later take it; the Assignee bundle grows with the team."""
        project = self.project(call)
        body = wire.read_body(wire.EntityIn, call.raw)
        member = self.world.user(self.entity_id(body, "id"))
        if member is None:
            raise wire.not_found(body.id or "")
        if member.id in project.team:
            raise _undocumented(f"adding {member.login} to the team of {project.shortName}, which already holds them")
        self.world.write_project(project.model_copy(update={"team": [*project.team, member.id]}), actor=Actor.AGENT)
        return self.answer(call, self.presenter().user(member))

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
        name = body.name or ""
        if not name.strip():
            raise _undocumented("a custom field without a name")
        if body.fieldType is None or body.fieldType.id is None:
            raise _undocumented("a custom field without a fieldType")
        try:
            kind = wire.FieldType(body.fieldType.id)
        except ValueError as error:
            raise wire.not_found(body.fieldType.id) from error
        if self.world.definition_named(name) is not None:
            raise _undocumented(f"a custom field named {name}, a name another holds")
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
        made = [present.bundle(p, f) for p, f in self._bundles(_bundle_kind(call))]
        page: list[wire.Answer] = [b for b in self.page(call, made) if b is not None]
        return self.answer(call, page)

    def _bundle(self, call: Call) -> tuple[wire.StoredProject, wire.StoredProjectField]:
        reference = call.path("bundle")
        found = next(((p, f) for p, f in self._bundles(_bundle_kind(call)) if f.bundle == reference), None)
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
        body = wire.read_body(wire.EntityIn, call.raw)
        name = body.name or ""
        if not name.strip():
            raise _undocumented("a body without a name")
        if any(v.name.lower() == name.lower() for v in field.values):
            raise _undocumented(f"a bundle value named {name}, which the bundle already holds")
        ids = fields.Ids(self.world.projects())
        added = wire.StoredBundleValue(id=ids.take("62"), name=name, ordinal=len(field.values))
        for project in self.world.projects():
            if not any(f.bundle == field.bundle for f in project.fields):
                continue
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
        shown = set(call.request.query_params.getlist("customFields"))
        page: list[wire.Answer] = [_showing(present.issue(i), shown) for i in self.page(call, matched, recorded=True)]
        if len(scope) == 1:
            self.world.saw(state.project_ref(scope[0].id), Operation.SEARCH)
        else:
            self.world.saw(state.instance_ref(), Operation.SEARCH)
        return self.answer(call, page)

    def _matching(self, call: Call, query: str | None) -> tuple[list[wire.StoredIssue], list[wire.StoredProject]]:
        matcher = Matcher(self.world, call.caller, self._clock.now(), query or "")
        parsed = parse_search(query, matcher.field_names())
        readable = self.world.projects()
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
            bad = start if start is not None and not start.lstrip("-").isdigit() else end
            raise wire.Refusal(
                400, "Bad Request", f"Incorrect timestamp {bad}", developer_message=f"Incorrect timestamp {bad}"
            ) from error  # as recorded: data/observed/issue_activities_bad_start.http
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
            raise _undocumented(f"a create naming project {project_id}, which does not exist")
        if body.summary is None or not body.summary.strip():
            raise _undocumented("an issue without a summary")
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
        written = self._with_fields(project, issue, body.customFields, creating=True)
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
                raise wire.field_required(self.definition_of(field).name)

    def _tags_named(self, tags: list[wire.EntityIn]) -> list[str]:
        named: list[str] = []
        for reference in tags:
            tag_id = self.entity_id(reference, "tag")
            if self.world.tag(tag_id) is None:
                raise _undocumented(f"a body naming tag {tag_id}, which does not exist")
            if tag_id not in named:
                named.append(tag_id)
        return named

    def update(self, call: Call) -> Answered:
        issue = self.issue(call)
        body = wire.read_body(wire.IssueUpdateIn, call.raw)
        project = self.home(issue)
        changed = issue
        if "summary" in body.model_fields_set:
            if body.summary is None or not body.summary.strip():
                raise _undocumented("an issue without a summary")
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
        issue = self.issue(call)
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
        self,
        project: wire.StoredProject,
        issue: wire.StoredIssue,
        writes: list[wire.CustomFieldIn],
        *,
        creating: bool = False,
    ) -> wire.StoredIssue:
        """The issue with a `customFields` block applied, every write checked before any is kept."""
        values = dict(issue.values)
        for write in writes:
            field = next((f for f in project.fields if write.id is not None and f.id == write.id), None)
            if field is None and write.name is not None:
                field = self.world.project_field(project, write.name)
            if field is None:
                raise _undocumented(f"a write to the field {write.name or write.id}, which the project does not carry")
            value = self._coerced(project, field, write.value, creating=creating)
            if value is None:
                values.pop(field.id, None)
            else:
                values[field.id] = value
        return issue.model_copy(update={"values": values})

    def _coerced(
        self,
        project: wire.StoredProject,
        field: wire.StoredProjectField,
        value: wire.FieldValueIn | None,
        *,
        creating: bool = False,
    ) -> wire.FieldValue | None:
        """What a write to the field stores; None clears it. A value the field will not take is refused."""
        if value is None:
            if not field.canBeEmpty:
                raise _undocumented(f"clearing {self.definition_of(field).name}, which cannot be empty")
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
                raise _undocumented(
                    f"the value {value.name or value.id} for {self.definition_of(field).name}, which its bundle has not got"
                )
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
            if creating and user is not None and user.id not in project.team:
                raise _undocumented(
                    f"creating an issue assigned to {user.login}, who is off {project.shortName}'s team"
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
        issue = self.issue(call)
        self.world.delete_issue(issue, by=call.caller.id, at=call.now, actor=Actor.AGENT)
        return 200, b""

    def comment(self, call: Call) -> Answered:
        issue = self.issue(call)
        body = wire.read_body(wire.CommentIn, call.raw)
        if body.text is None or not body.text.strip():
            raise _undocumented("a comment without text")
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
        name = body.name or ""
        if not name.strip():
            raise _undocumented("a body without a name")
        if self.world.tag_named(name) is not None:
            raise _undocumented(f"a tag named {name}, a name another tag holds")
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
        issue = self.issue(call)
        body = wire.read_body(wire.TagIn, call.raw)
        tag_id = self.entity_id(wire.EntityIn(id=body.id), "id")
        tag = self.world.tag(tag_id)
        if tag is None:
            raise _undocumented(f"a body naming tag {tag_id}, which does not exist")
        if tag.id not in issue.tags:
            self._written(self.home(issue), issue, issue.model_copy(update={"tags": [*issue.tags, tag.id]}), call)
        return self.answer(call, self.presenter().tag(tag))

    def issue_tag_remove(self, call: Call) -> Answered:
        issue = self.issue(call)
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
        issue = self.issue(call)
        link_type, outward = self._slot(call.path("link"))
        body = wire.read_body(wire.EntityIn, call.raw)
        other_id = self.entity_id(body, "id")
        other = self.world.issue(other_id)
        if other is None:
            raise wire.not_found(other_id)
        if other.id == issue.id:
            raise _undocumented("linking an issue to itself")
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
        issue = self.issue(call)
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
            raise _undocumented("a command naming no issues")
        targets: list[wire.StoredIssue] = []
        for named in body.issues:
            reference = named.id or named.idReadable
            found = self.world.find_issue(reference) if reference else None
            if found is None:
                raise wire.not_found(reference or "")
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
            commands=[wire.ParsedCommandOut() for _ in commands],
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
                    raise _undocumented(f"a command naming the tag {command.value}, which does not exist")
                kept = [t for t in issue.tags if t != tag.id]
                issue = issue.model_copy(update={"tags": [*kept, tag.id] if command.word is CommandWord.TAG else kept})
                continue
            name = state.ASSIGNEE_FIELD if command.word is CommandWord.FOR else command.field or ""
            field = self.world.project_field(project, name)
            if field is None:
                raise _undocumented(f"the command {name} {command.value}")
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


def _showing(issue: wire.IssueOut, names: set[str]) -> wire.IssueOut:
    """The issue with only the custom fields `GET /issues?customFields=` names (by name, the parameter repeated for
    more than one), as the issues resource documents it; every field when it names none."""
    if not names:
        return issue
    return issue.model_copy(update={"customFields": [f for f in issue.customFields if f.name in names]})


def _bundle_kind(call: Call) -> str:
    """The bundle kind a `/admin/customFieldSettings/bundles/<kind>/...` path names."""
    return call.request.url.path.split("/bundles/", 1)[1].split("/", 1)[0]


def _differs(before: wire.StoredIssue, after: wire.StoredIssue) -> bool:
    keep = {"updated", "updater", "resolved"}
    return before.model_dump(exclude=keep) != after.model_dump(exclude=keep)


_BUNDLE_KINDS = ("state", "enum", "version", "user")
"""The bundle kinds the fake keeps: the bundles of the single-valued field types it holds."""

TABLE: tuple[tuple[str, str, str], ...] = (
    ("/users/me", "GET", "me"),
    ("/users", "GET", "users"),
    ("/users/{user}", "GET", "user"),
    ("/admin/projects", "GET", "projects"),
    ("/admin/projects", "POST", "project_create"),
    ("/admin/projects/{project}", "GET", "project_read"),
    ("/admin/projects/{project}/customFields", "GET", "project_fields"),
    ("/admin/projects/{project}/customFields", "POST", "project_field_attach"),
    ("/admin/projects/{project}/customFields/{field}", "GET", "project_field_read"),
    ("/admin/projects/{project}/team", "GET", "project_team"),
    ("/admin/projects/{project}/team/users", "GET", "project_team_users"),
    ("/admin/projects/{project}/team/ownUsers", "POST", "project_team_add"),
    ("/admin/customFieldSettings/customFields", "GET", "definitions"),
    ("/admin/customFieldSettings/customFields", "POST", "definition_create"),
    ("/admin/customFieldSettings/customFields/{field}", "GET", "definition_read"),
    *(
        (path, method, handler)
        for kind in _BUNDLE_KINDS
        for path, method, handler in (
            (f"/admin/customFieldSettings/bundles/{kind}", "GET", "bundles"),
            (f"/admin/customFieldSettings/bundles/{kind}/{{bundle}}", "GET", "bundle_read"),
            (f"/admin/customFieldSettings/bundles/{kind}/{{bundle}}/values", "GET", "bundle_values"),
            (f"/admin/customFieldSettings/bundles/{kind}/{{bundle}}/values", "POST", "bundle_value_add"),
        )
        if kind != "user" or not path.endswith("/values")
    ),
    ("/issues", "GET", "search"),
    ("/issues", "POST", "create"),
    ("/issuesGetter/count", "POST", "count"),
    ("/issues/{issue}", "GET", "issue_read"),
    ("/issues/{issue}", "POST", "update"),
    ("/issues/{issue}", "DELETE", "delete"),
    ("/issues/{issue}/customFields", "GET", "issue_fields"),
    ("/issues/{issue}/customFields/{field}", "GET", "issue_field_read"),
    ("/issues/{issue}/customFields/{field}", "POST", "field_write"),
    ("/issues/{issue}/comments", "GET", "comments"),
    ("/issues/{issue}/comments", "POST", "comment"),
    ("/issues/{issue}/comments/{comment}", "GET", "comment_read"),
    ("/issues/{issue}/activities", "GET", "activities"),
    ("/issues/{issue}/tags", "GET", "issue_tags"),
    ("/issues/{issue}/tags", "POST", "issue_tag_add"),
    ("/issues/{issue}/tags/{tag}", "DELETE", "issue_tag_remove"),
    ("/issues/{issue}/links", "GET", "links"),
    ("/issues/{issue}/links/{link}", "GET", "link_slot"),
    ("/issues/{issue}/links/{link}/issues", "GET", "link_slot_issues"),
    ("/issues/{issue}/links/{link}/issues", "POST", "link_add"),
    ("/issues/{issue}/links/{link}/issues/{target}", "DELETE", "link_remove"),
    ("/issueLinkTypes", "GET", "link_types"),
    ("/tags", "GET", "tags"),
    ("/tags", "POST", "tag_create"),
    ("/tags/{tag}", "GET", "tag_read"),
    ("/commands", "POST", "command"),
)
"""Every YouTrack operation served: its path (YouTrack's template, parameters named for the handler), method and
the `YouTrackApi` method that answers it. Every other operation of the claimed resources is in `surface.UNSERVED`."""


def build_app(store: Store, clock: Clock) -> Starlette:
    api = YouTrackApi(store, clock)
    routes: list[Route] = []
    for prefix in PREFIXES:

        def youtrack_path(request: Request, prefix: str = prefix) -> str:
            return request.url.path[len(prefix) :]

        served = [(path, method, getattr(api, name)) for path, method, name in TABLE]
        for path, methods in _by_path(served).items():
            answers = {m: api.endpoint(h, fault_path=youtrack_path) for m, h in methods.items()}
            routes.append(Route(prefix + path, _dispatch(answers), methods=list(methods)))
        for method, path in UNSERVED:
            routes.append(Route(prefix + path, _unserved(method, path), methods=[method]))
    for path, methods in hub_routes(api):
        answers = {m: api.endpoint(h, fault_path=lambda r: r.url.path) for m, h in methods.items()}
        routes.append(Route(HUB + path, _dispatch(answers), methods=list(methods)))

    async def refused(request: Request, error: Exception) -> Response:
        status = error.status_code if isinstance(error, HTTPException) else 500
        path = request.url.path
        if status == 404 and any(path == p or path.startswith(p + "/") for p in (*PREFIXES, HUB)):
            raise NotImplementedError(f"{request.method} {path} is outside the resources this fake serves")
        if status == 405:
            return Response(
                wire.error_body(wire.Refusal(405, "Method Not Allowed", "HTTP 405 Method Not Allowed")),
                status_code=405,
                media_type=JSON,
            )
        return Response(
            wire.error_body(wire.Refusal(404, "Not Found", "HTTP 404 Not Found")), status_code=404, media_type=JSON
        )

    return Starlette(routes=routes, exception_handlers={404: refused, 405: refused})


def _unserved(method: str, path: str) -> Callable[[Request], Awaitable[Response]]:
    async def route(request: Request) -> Response:
        raise NotImplementedError(f"{method} {path} is an operation of YouTrack's API that this fake does not serve")

    return route


def _by_path(table: Sequence[tuple[str, str, Handler]]) -> dict[str, dict[str, Handler]]:
    by_path: dict[str, dict[str, Handler]] = {}
    for path, method, handler in table:
        by_path.setdefault(path, {})[method] = handler
    return by_path


def _dispatch(answers: dict[str, Callable[[Request], Awaitable[Response]]]) -> Callable[[Request], Awaitable[Response]]:
    async def route(request: Request) -> Response:
        return await answers["GET" if request.method == "HEAD" else request.method](request)

    return route


def _undocumented(what: str) -> NotImplementedError:
    """A request whose answer neither YouTrack's documentation nor a recording of the real service gives: refused
    by name, never answered with an error this fake would have to invent."""
    return NotImplementedError(f"{what}: what YouTrack answers is neither documented nor recorded")
