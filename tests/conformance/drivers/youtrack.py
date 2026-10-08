"""YouTrack through its REST API (`https://<instance>.youtrack.cloud/api/...`), as a client holding permanent tokens.

Every account the world seeds (the agent's `agent-bot` and each person, whose login is their key) holds a permanent
token of its own (`perm:…`), seeded through the provider's own seed (`tokens`); any other token acts as the agent,
since Minutehand checks no credential. An issue is named by its database id (`2-17`, what the world
records), and shows its readable id (`LAUNCH-1`) as its key. Every answer is narrowed by `fields=` and every
collection is paged by `$skip`/`$top`, as YouTrack documents
(https://www.jetbrains.com/help/youtrack/devportal/api-fields-syntax.html,
https://www.jetbrains.com/help/youtrack/devportal/api-concept-pagination.html).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, StrictInt

from minutehand.adapters.control.wire import Claims, CreateWorld, WorldView
from minutehand.domain.scenario import Seed
from tests.conformance.contract import (
    Api,
    CommentSeen,
    DeclaredId,
    Driver,
    FaultCase,
    IdKind,
    PermissionCase,
    PersonSeen,
    Progress,
    Session,
    Tickets,
    TicketSeen,
    ok,
)

PROVIDER = "youtrack"
AGENT_LOGIN = "agent-bot"
"""The agent's own account, which the provider seeds beside the people."""
PAGE = 100
"""What this client asks for per page when it reads a whole collection."""
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

STATE = "State"
ASSIGNEE = "Assignee"
STATE_NAMES = {
    Progress.OPEN: "Open",
    Progress.IN_PROGRESS: "In Progress",
    Progress.DONE: "Fixed",
    Progress.CANCELLED: "Won't fix",
}
"""The value of YouTrack's default State bundle a client writes for each progress."""
CANCELLED_STATES = frozenset({"won't fix", "duplicate", "can't reproduce", "incomplete", "obsolete"})
"""The resolved values of YouTrack's default State bundles that close an issue without doing it."""
IN_PROGRESS_STATE = "in progress"
RELATES = "Relates"
"""YouTrack's undirected default link type: its one slot is named by the type's own id."""


def host(tag: str) -> str:
    return f"https://{tag}.youtrack.cloud"


def token(tag: str, login: str) -> str:
    """The permanent token that acts as `login` in the instance `tag` names."""
    return f"perm:{login}.{tag}"


def tag_of(world: WorldView) -> str:
    """The instance a world was opened as: the host label it claims (`Manifest.world_keys`)."""
    return world.claims.keys[0]


def millis(at: int) -> datetime:
    """YouTrack's timestamp, epoch milliseconds, as an aware UTC moment."""
    return EPOCH + timedelta(milliseconds=at)


# ---------------------------------------------------------------------------------------------- YouTrack's answers


class Read(BaseModel):
    """A YouTrack entity as this client reads it: the attributes it named in `fields=`, `$type` beside them."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class User(Read):
    id: str
    login: str | None = None
    fullName: str | None = None
    email: str | None = None
    banned: bool = False
    guest: bool = False


class Who(Read):
    """A user named inside another entity: what the `fields=` asked of them."""

    login: str | None = None
    email: str | None = None


class Activity(Read):
    timestamp: StrictInt
    author: Who


class Named(Read):
    """A bundle element or a user as a custom field's value."""

    id: str | None = None
    name: str | None = None
    login: str | None = None
    email: str | None = None
    isResolved: bool | None = None


class IssueField(Read):
    name: str
    value: Named | list[Named] | StrictInt | float | str | None = None


class Tag(Read):
    id: str
    name: str


class Comment(Read):
    text: str
    author: Who | None = None


class Linked(Read):
    id: str


class Link(Read):
    issues: list[Linked] = []


class Project(Read):
    id: str
    name: str
    shortName: str


class Issue(Read):
    id: str
    idReadable: str | None = None
    summary: str | None = None
    description: str | None = None
    created: StrictInt | None = None
    project: Project | None = None
    customFields: list[IssueField] = []
    tags: list[Tag] = []
    comments: list[Comment] = []
    links: list[Link] = []


class LinkType(Read):
    id: str
    name: str
    directed: bool


class Me(Read):
    login: str


def parsed[T: Read](model: type[T], response: httpx.Response) -> T:
    return model.model_validate_json(response.content)


def listed[T: Read](model: type[T], response: httpx.Response) -> list[T]:
    found = json.loads(response.content)
    if not isinstance(found, list):
        raise AssertionError(f"YouTrack answered a collection with {response.text[:200]}")
    return [model.model_validate(item) for item in found]


ISSUE_FIELDS = (
    "id,idReadable,summary,description,created,project(id,name,shortName),"
    "customFields(name,value(id,name,login,email,isResolved)),tags(id,name),comments(text,author(email)),"
    "links(issues(id))"
)
ACTIVITY_CATEGORIES = (
    "IssueCreatedCategory,CustomFieldCategory,CommentsCategory,SummaryCategory,DescriptionCategory,TagsCategory,"
    "LinksCategory"
)
"""Every change an issue's activity stream records, by YouTrack's category ids
(https://www.jetbrains.com/help/youtrack/devportal/resource-api-activities.html)."""
USER_FIELDS = "id,login,fullName,email,banned,guest"


# ---------------------------------------------------------------------------------------------- the session


class YouTrackSession(Tickets):
    def __init__(self, api: Api, tag: str, credential: str) -> None:
        self._tag = tag
        self._credential = credential
        self.secrets = (credential,)
        self._http = api.http(
            {"Authorization": f"Bearer {credential}", "Accept": "application/json"}, base_url=host(tag)
        )
        self._api = api

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ helpers

    def _get(self, what: str, path: str, **query: str | int) -> httpx.Response:
        return ok(what, self._http.get(path, params=query))

    def _post(self, what: str, path: str, body: object, **query: str | int) -> httpx.Response:
        return ok(what, self._http.post(path, params=query, json=body))

    def _pages(self, what: str, path: str, page_size: int, **query: str | int) -> list[httpx.Response]:
        """Every page of a collection at `page_size`, `$skip` moving on until a page comes back short."""
        pages: list[httpx.Response] = []
        skip = 0
        while True:
            answered = self._get(what, path, **query, **{"$skip": skip, "$top": page_size})
            count = len(json.loads(answered.content))
            if count == 0 and pages:
                return pages
            pages.append(answered)
            if count < page_size:
                return pages
            skip += count

    def _every[T: Read](self, model: type[T], what: str, path: str, **query: str | int) -> list[T]:
        return [item for page in self._pages(what, path, PAGE, **query) for item in listed(model, page)]

    def _project(self, name: str) -> Project:
        projects = self._every(Project, "list projects", "/api/admin/projects", fields="id,name,shortName")
        found = next((p for p in projects if p.name == name), None)
        if found is None:
            raise AssertionError(f"YouTrack lists no project named {name!r}: {[p.name for p in projects]}")
        return found

    def _tags(self) -> list[Tag]:
        return self._every(Tag, "list tags", "/api/tags", fields="id,name")

    # ------------------------------------------------------------------ accounts

    def whoami(self) -> str:
        return parsed(Me, self._get("read me", "/api/users/me", fields="login")).login

    def people(self) -> list[PersonSeen]:
        return [
            PersonSeen(
                id=u.id,
                name=u.fullName or "",
                email=u.email,
                active=not u.banned,
                guest=u.guest,
                login=u.login,
            )
            for u in self._every(User, "list users", "/api/users", fields=USER_FIELDS)
        ]

    def people_pages(self, page_size: int) -> list[list[str]]:
        pages = self._pages("list users", "/api/users", page_size, fields="id")
        return [[u.id for u in listed(User, page)] for page in pages]

    def stranger(self, *, credentialed: bool) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token(self._tag, 'nobody')}"} if credentialed else {}
        with self._api.http(headers, base_url=host(self._tag)) as http:
            return http.get("/api/users/me", params={"fields": "login"})

    def observe(self) -> str:
        issues = self._pages(
            "list issues",
            "/api/issues",
            PAGE,
            fields="id,idReadable,summary,description,created,updated,resolved,"
            "customFields(name,value(name,login)),tags(name),comments(text,created,author(login))",
        )
        users = self._pages("list users", "/api/users", PAGE, fields=USER_FIELDS)
        return "\n".join(r.text for r in [*issues, *users])

    def change(self, label: str) -> None:
        self.create_ticket("Launch", label, "")

    # ------------------------------------------------------------------ tickets

    def ticket_ids(self) -> dict[str, str]:
        issues = self._every(Issue, "list issues", "/api/issues", fields="id,summary")
        return {i.summary or "": i.id for i in issues}

    def read_ticket(self, ticket: str) -> TicketSeen:
        issue = parsed(Issue, self._get("read issue", f"/api/issues/{ticket}", fields=ISSUE_FIELDS))
        state = self._field(issue, STATE)
        assignee = self._field(issue, ASSIGNEE)
        if not isinstance(state, Named) or state.name is None:
            raise AssertionError(f"issue {ticket} answered no State value: {issue.customFields}")
        if assignee is not None and not isinstance(assignee, Named):
            raise AssertionError(f"issue {ticket} answered an Assignee that is no user: {assignee!r}")
        if issue.created is None or issue.summary is None:
            raise AssertionError(f"issue {ticket} answered no created or summary")
        return TicketSeen(
            id=issue.id,
            title=issue.summary,
            body=issue.description or "",
            progress=_progress(state),
            assignee_email=None if assignee is None else assignee.email,
            assignee_id=None if assignee is None else assignee.id,
            labels=frozenset(t.name for t in issue.tags),
            comments=tuple(CommentSeen(c.author.email if c.author else None, c.text) for c in issue.comments),
            key=issue.idReadable,
            created=millis(issue.created),
            links=tuple(linked.id for slot in issue.links for linked in slot.issues),
            project=None if issue.project is None else issue.project.name,
            changed_by_email=self._latest_author(ticket),
        )

    def _latest_author(self, ticket: str) -> str | None:
        """Who the issue's activity stream says made its latest change, newest first (`reverse=true`)."""
        latest = listed(
            Activity,
            self._get(
                "read activities",
                f"/api/issues/{ticket}/activities",
                categories=ACTIVITY_CATEGORIES,
                reverse="true",
                fields="timestamp,author(email)",
                **{"$top": 1},
            ),
        )
        if not latest:
            raise AssertionError(f"issue {ticket} answered no activity, not even its creation")
        return latest[0].author.email

    @staticmethod
    def _field(issue: Issue, name: str) -> Named | list[Named] | int | float | str | None:
        found = next((f for f in issue.customFields if f.name == name), None)
        if found is None:
            raise AssertionError(f"issue {issue.id} carries no {name} field")
        return found.value

    def create_ticket(self, project: str, title: str, body: str) -> str:
        home = self._project(project)
        made = self._post(
            "create issue",
            "/api/issues",
            {"project": {"id": home.id}, "summary": title, "description": body},
            fields="id",
        )
        return parsed(Issue, made).id

    def set_progress(self, ticket: str, progress: Progress) -> None:
        field = {"name": STATE, "$type": "StateIssueCustomField", "value": {"name": STATE_NAMES[progress]}}
        self._post("set state", f"/api/issues/{ticket}", {"customFields": [field]}, fields="id")

    def assign(self, ticket: str, person: str | None) -> None:
        value = None if person is None else {"id": person}
        field = {"name": ASSIGNEE, "$type": "SingleUserIssueCustomField", "value": value}
        self._post("assign", f"/api/issues/{ticket}", {"customFields": [field]}, fields="id")

    def comment(self, ticket: str, text: str) -> None:
        self._post("comment", f"/api/issues/{ticket}/comments", {"text": text}, fields="id")

    def link(self, ticket: str, other: str) -> None:
        types = listed(LinkType, self._get("list link types", "/api/issueLinkTypes", fields="id,name,directed"))
        relates = next((t for t in types if t.name == RELATES and not t.directed), None)
        if relates is None:
            raise AssertionError(f"YouTrack lists no undirected {RELATES} link type: {types}")
        self._post("link", f"/api/issues/{ticket}/links/{relates.id}/issues", {"id": other}, fields="id")

    def relabel(self, ticket: str, labels: list[str]) -> None:
        held = parsed(Issue, self._get("read tags", f"/api/issues/{ticket}", fields="id,tags(id,name)")).tags
        known = {t.name: t for t in self._tags()}
        for name in labels:
            if name in {t.name for t in held}:
                continue
            tag = known[name] if name in known else None
            if tag is None:
                tag = parsed(Tag, self._post("create tag", "/api/tags", {"name": name}, fields="id,name"))
            self._post("tag", f"/api/issues/{ticket}/tags", {"id": tag.id}, fields="id")
        for tag in held:
            if tag.name not in labels:
                ok("untag", self._http.delete(f"/api/issues/{ticket}/tags/{tag.id}"))

    def delete_ticket(self, ticket: str) -> None:
        ok("delete issue", self._http.delete(f"/api/issues/{ticket}"))

    def search_tickets(self, text: str) -> list[str]:
        found = self._every(Issue, "search issues", "/api/issues", query=f"summary: {{{text}}}", fields="id")
        return [i.id for i in found]

    def ticket_pages(self, project: str, page_size: int) -> list[list[str]]:
        home = self._project(project)
        pages = self._pages("list issues", "/api/issues", page_size, query=f"project: {home.shortName}", fields="id")
        return [[i.id for i in listed(Issue, page)] for page in pages]

    def first_key(self, project: str) -> str:
        """The readable id of the project's earliest issue, as YouTrack's search sorts them by creation."""
        found = listed(
            Issue,
            self._get(
                "search issues",
                "/api/issues",
                query=f"project: {{{project}}} sort by: created asc",
                fields="id,idReadable",
                **{"$top": 1},
            ),
        )
        if not found or found[0].idReadable is None:
            raise AssertionError(f"YouTrack answered no issue with a readable id in {project}")
        return found[0].idReadable

    # ------------------------------------------------------------------ what a permission governs

    def holds(self, permission: str, project: str) -> bool:
        """Whether Hub's permissions cache lists this account holding the permission in the project. Minutehand
        enforces no permission, so what one governs is what the API reports, not a 403."""
        answered = ok(
            "read permissions",
            self._http.get("/hub/api/rest/permissions/cache", params={"fields": "permission/key,global,projects/key"}),
        )
        for entry in answered.json():
            if entry["permission"]["key"] == permission:
                return bool(entry["global"]) or project in [p["key"] for p in entry["projects"]]
        return False


def _progress(state: Named) -> Progress:
    name = (state.name or "").lower()
    if state.isResolved:
        return Progress.CANCELLED if name in CANCELLED_STATES else Progress.DONE
    return Progress.IN_PROGRESS if name == IN_PROGRESS_STATE else Progress.OPEN


# ---------------------------------------------------------------------------------------------- faults


def _faulted(status: int, lasts: timedelta) -> FaultCase:
    def call(api: Api, world: WorldView) -> httpx.Response:
        tag = tag_of(world)
        # No YouTrack client library is installed: httpx, through the proxy and its CA bundle, as `Api.http` makes it.
        with api.http({"Authorization": f"Bearer {token(tag, AGENT_LOGIN)}"}, base_url=host(tag)) as http:
            return http.get("/api/issues", params={"fields": "id", "$top": 1})

    def trigger(api: Api, world: WorldView) -> BaseException | None:
        try:
            call(api, world).raise_for_status()
        except httpx.HTTPStatusError as raised:
            return raised
        return None

    def then(api: Api, world: WorldView) -> None:
        ok("list issues after the fault", call(api, world))

    return FaultCase(
        name=f"youtrack_{status}",
        fragment={"faults": [{"method": "GET", "path": "/issues", "status": status, "lasts": _iso(lasts)}]},
        trigger=trigger,
        typed=None,
        status=status,
        holds={429: "Too Many Requests", 503: "Service Unavailable"}[status],
        expires_after=lasts,
        then=then,
    )


def _iso(span: timedelta) -> str:
    return f"PT{int(span.total_seconds())}S"


# ---------------------------------------------------------------------------------------------- the driver


def _seed_body(found: object) -> dict[str, object]:
    decoded = json.loads(found) if isinstance(found, str) else found
    if not isinstance(decoded, dict):
        raise ValueError(f"a youtrack provider seed is an object, not {found!r}")
    return {str(k): v for k, v in decoded.items()}


class YouTrackDriver(Driver):
    provider: ClassVar[str] = PROVIDER
    session: ClassVar[type[Session]] = YouTrackSession
    absent: ClassVar[Mapping[str, str]] = {
        "accounts.title": "YouTrack's User entity has no job title: login, full name, email, avatar, banned, guest "
        "and online are all it answers.",
        "accounts.bot": "YouTrack's User entity has no bot or app flag: an integration acts through a permanent token "
        "or a Hub service belonging to an ordinary user account.",
        "accounts.guest": "YouTrack's only guest is the single anonymous 'guest' account unauthenticated visitors act "
        "as; nobody is invited as a guest from outside, so no person's account can be one.",
    }
    id_formats: ClassVar[Mapping[IdKind, re.Pattern[str]]] = {
        # A user is named by its database id, `<type>-<number>` (`1-<n>`): youtrack/state.py's entity table and
        # YouTrack's User entity (https://www.jetbrains.com/help/youtrack/devportal/api-entity-User.html).
        IdKind.PERSON: re.compile(r"\d+-\d+"),
        # An issue is named by its database id (`2-<n>`), its readable id (`LAUNCH-1`) being `idReadable`:
        # youtrack/state.py and CLAIMS.md #6, and YouTrack's Issue entity
        # (https://www.jetbrains.com/help/youtrack/devportal/api-entity-Issue.html).
        IdKind.TICKET: re.compile(r"\d+-\d+"),
    }
    page_floor: ClassVar[Mapping[str, int]] = {
        # `$top` takes any positive number of entities (CLAIMS.md #26;
        # https://www.jetbrains.com/help/youtrack/devportal/api-concept-pagination.html).
        "people": 1,
        "tickets": 1,
    }
    # CLAIMS.md #1: a call YouTrack cannot authenticate is a 401 `Unauthorized`.

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise NotImplementedError(
                "the youtrack seed gives each scenario person the login of their Person.key and has no field that "
                f"gives a person another login, so {dict(logins)} cannot be seeded"
            )
        people = seed["people"]
        if not isinstance(people, list):
            raise ValueError(f"the seed's people are a list, not {people!r}")
        keys = [str(p["key"]) for p in people if isinstance(p, dict)]
        tokens = [{"token": token(tag, login), "login": login} for login in [AGENT_LOGIN, *keys]]
        seeds = seed["provider_seeds"] if "provider_seeds" in seed else []
        if not isinstance(seeds, list):
            raise ValueError(f"the seed's provider_seeds are a list, not {seeds!r}")
        others = [s for s in seeds if not (isinstance(s, dict) and s["provider"] == PROVIDER)]
        mine = next((s for s in seeds if isinstance(s, dict) and s["provider"] == PROVIDER), None)
        body = {} if mine is None else _seed_body(mine["body"])
        held = body["tokens"] if "tokens" in body else []
        if not isinstance(held, list):
            raise ValueError(f"the youtrack seed's tokens are a list, not {held!r}")
        body["tokens"] = [*held, *tokens]
        chosen = {**seed, "provider_seeds": [*others, {"provider": PROVIDER, "body": body}]}
        return CreateWorld(
            seed=Seed.model_validate(chosen),
            claims=Claims(tokens=[t["token"] for t in tokens], keys=[tag]),
        )

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        return token(tag_of(world), person or AGENT_LOGIN)

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        credential = self.credential_of(world, person)
        assert credential is not None
        return YouTrackSession(api, tag_of(world), credential)

    def faults(self) -> list[FaultCase]:
        # The seed's one fault kind (`YouTrackSeed.faults`): calls to a path refused with a status while it lasts,
        # with YouTrack's error body and a `Retry-After` for its remainder.
        return [_faulted(429, timedelta(minutes=1)), _faulted(503, timedelta(minutes=5))]

    def declared_ids(self) -> list[DeclaredId]:
        # A seeded project may name its short name (`YouTrackSeed.projects[].short_name`), which prefixes every
        # issue's readable id in it (CLAIMS.md #29).
        def read(session: Session) -> str:
            if not isinstance(session, YouTrackSession):
                raise TypeError(f"a youtrack session, not {type(session).__name__}")
            return session.first_key("Launch")

        return [
            DeclaredId(
                name="project short name",
                seed={
                    "provider_seeds": [
                        {"provider": PROVIDER, "body": {"projects": [{"name": "Launch", "short_name": "VENUE"}]}}
                    ]
                },
                read=read,
                expected="VENUE-1",
            )
        ]

    def permission(self) -> PermissionCase | None:
        # Read Issue, held per project (`jetbrains.youtrack.readIssue`), as Hub's permissions cache reports it.
        def allowed(session: Session) -> bool:
            if not isinstance(session, YouTrackSession):
                raise TypeError(f"a youtrack session, not {type(session).__name__}")
            return session.holds("jetbrains.youtrack.readIssue", "LAUNCH")

        return PermissionCase(
            person="sofia", permission="jetbrains.youtrack.readIssue", project="LAUNCH", allowed=allowed
        )


DRIVER = YouTrackDriver()
