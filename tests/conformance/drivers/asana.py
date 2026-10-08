"""Asana, through its REST API at `https://app.asana.com/api/1.0` (https://developers.asana.com/reference).

Every caller holds a personal access token sent as `Authorization: Bearer`: the agent's, and one per seeded person
(`AsanaSeed.tokens`, each naming its person); any other token acts as the agent, since Minutehand does not enforce
credentials. Every write is wrapped in `{"data": ...}`, every answer is `{"data": ...}` (with `next_page` on a paged collection), and a list
is compact until `opt_fields` names what to bring.

A task's state is where Asana's board puts it: the section it sits in (`To do`, `Done`, `Cancelled`, and an
`In progress` section made when a task is first moved there) together with its `completed` box. A link is a
dependency (`addDependencies`); labels are tags; comments are stories of subtype `comment_added`; who made a
task's latest change is the author of its latest story, the task's activity feed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import asana  # pyright: ignore[reportMissingTypeStubs]
import httpx
import urllib3
from asana.rest import ApiException  # pyright: ignore[reportMissingTypeStubs]
from pydantic import BaseModel, ConfigDict

from minutehand.adapters.control.wire import CreateWorld, WorldView
from tests.conformance.contract import (
    Api,
    CommentSeen,
    Driver,
    FaultCase,
    IdKind,
    PersonSeen,
    Progress,
    Session,
    Tickets,
    TicketSeen,
    ok,
)

PROVIDER = "asana"
BASE = "https://app.asana.com/api/1.0"
STAMP = "%Y-%m-%dT%H:%M:%S.%fZ"
"""Asana's timestamps: ISO 8601 in UTC with milliseconds, e.g. `2012-02-22T02:06:58.147Z`
(https://developers.asana.com/docs/input-output-options)."""

TO_DO = "To do"
IN_PROGRESS = "In progress"
DONE = "Done"
CANCELLED = "Cancelled"
SECTION_OF: Mapping[Progress, str] = {
    Progress.OPEN: TO_DO,
    Progress.IN_PROGRESS: IN_PROGRESS,
    Progress.DONE: DONE,
    Progress.CANCELLED: CANCELLED,
}
COMMENT = "comment_added"
"""A story's `resource_subtype` when a person wrote it (https://developers.asana.com/reference/getstoriesfortask)."""

PAGE = 100
"""The largest `limit` Asana takes on a paged list (https://developers.asana.com/docs/pagination)."""


# ---------------------------------------------------------------------------------------------- Asana's answers


class _Answer(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Ref(_Answer):
    gid: str


class Named(_Answer):
    gid: str
    name: str


class User(_Answer):
    gid: str
    name: str
    email: str | None = None


class Membership(_Answer):
    project: Named | None = None
    section: Named | None = None


class Tag(_Answer):
    gid: str
    name: str


class Task(_Answer):
    gid: str
    name: str
    notes: str
    completed: bool
    created_at: str
    assignee: User | None
    memberships: list[Membership]
    tags: list[Tag]
    dependencies: list[Ref] = []
    dependents: list[Ref] = []


class Story(_Answer):
    gid: str
    resource_subtype: str
    text: str = ""
    created_by: User | None = None


class NextPage(_Answer):
    offset: str


class Page(_Answer):
    data: list[dict[str, object]]
    next_page: NextPage | None = None


class One(_Answer):
    data: dict[str, object]


def _parsed(value: str) -> datetime:
    """An Asana timestamp, strictly in Asana's own format, as an aware UTC datetime."""
    return datetime.strptime(value, STAMP).replace(tzinfo=UTC)


def _wrapped(fields: Mapping[str, object]) -> bytes:
    return json.dumps({"data": fields}).encode()


# ---------------------------------------------------------------------------------------------- the session


class AsanaSession(Tickets):
    def __init__(self, api: Api, token: str) -> None:
        self.http = api.http(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"},
            base_url=BASE,
        )
        self.secrets = (token,)
        self._workspace: str | None = None

    def close(self) -> None:
        self.http.close()

    # -------------------------------------------------------------------- reads and writes as Asana takes them

    def _get(self, what: str, path: str, params: Mapping[str, str | int] | None = None) -> httpx.Response:
        return ok(what, self.http.get(path, params=dict(params or {})))

    def _one(self, what: str, path: str, params: Mapping[str, str | int] | None = None) -> dict[str, object]:
        return One.model_validate_json(self._get(what, path, params).content).data

    def _send(self, what: str, method: str, path: str, fields: Mapping[str, object]) -> httpx.Response:
        return ok(
            what,
            self.http.request(method, path, content=_wrapped(fields), headers={"Content-Type": "application/json"}),
        )

    def _pages(self, what: str, path: str, params: Mapping[str, str | int], size: int) -> Iterator[Page]:
        """Each page of a paged collection at `size`, following `next_page.offset` to the last."""
        offset: str | None = None
        while True:
            sent: dict[str, str | int] = {**params, "limit": size}
            if offset is not None:
                sent["offset"] = offset
            page = Page.model_validate_json(self._get(what, path, sent).content)
            yield page
            if page.next_page is None:
                return
            offset = page.next_page.offset

    def _all(self, what: str, path: str, params: Mapping[str, str | int]) -> list[dict[str, object]]:
        return [item for page in self._pages(what, path, params, PAGE) for item in page.data]

    def workspace(self) -> str:
        """The workspace the token's user belongs to (https://developers.asana.com/reference/getworkspaces)."""
        if self._workspace is None:
            found = [Named.model_validate(w) for w in self._all("list workspaces", "/workspaces", {})]
            if not found:
                raise AssertionError("Asana answered no workspace for this token")
            self._workspace = found[0].gid
        return self._workspace

    def project(self, name: str) -> str:
        found = [
            Named.model_validate(p)
            for p in self._all("list projects", "/projects", {"workspace": self.workspace(), "opt_fields": "name"})
        ]
        named = [p.gid for p in found if p.name == name]
        if len(named) != 1:
            raise LookupError(f"Asana lists {len(named)} projects named {name!r}")
        return named[0]

    def _task(self, ticket: str) -> Task:
        fields = (
            "name,notes,completed,created_at,assignee.name,assignee.email,memberships.project.name,"
            "memberships.section.name,tags.name,dependencies,dependents"
        )
        return Task.model_validate(self._one("read a task", f"/tasks/{ticket}", {"opt_fields": fields}))

    def _stories(self, ticket: str) -> list[Story]:
        return [
            Story.model_validate(s)
            for s in self._all(
                "read a task's stories",
                f"/tasks/{ticket}/stories",
                {"opt_fields": "resource_subtype,text,created_by.name,created_by.email"},
            )
        ]

    def _sections(self, project: str) -> dict[str, str]:
        return {
            n.name: n.gid
            for n in (
                Named.model_validate(s)
                for s in self._all("list a project's sections", f"/projects/{project}/sections", {"opt_fields": "name"})
            )
        }

    def _tags(self) -> dict[str, str]:
        return {
            t.name: t.gid
            for t in (
                Tag.model_validate(t)
                for t in self._all("list tags", f"/workspaces/{self.workspace()}/tags", {"opt_fields": "name"})
            )
        }

    # -------------------------------------------------------------------- accounts

    def whoami(self) -> str:
        me = User.model_validate(self._one("read me", "/users/me", {"opt_fields": "name,email"}))
        return me.email if me.email is not None else me.name

    def people(self) -> list[PersonSeen]:
        """Asana's user listing (https://developers.asana.com/reference/getusers); it says nothing of a guest or
        a removed member, which only `/workspace_memberships` carries."""
        found = self._all("list users", "/users", {"workspace": self.workspace(), "opt_fields": "name,email"})
        return [PersonSeen(id=u.gid, name=u.name, email=u.email) for u in map(User.model_validate, found)]

    def people_pages(self, page_size: int) -> list[list[str]]:
        return [
            [Ref.model_validate(u).gid for u in page.data]
            for page in self._pages("list users", "/users", {"workspace": self.workspace()}, page_size)
        ]

    def unknown_credential(self) -> httpx.Response:
        return self.http.get("/users/me", headers={"Authorization": "Bearer 2/0000000000000000/0000000000000000:00"})

    def observe(self) -> str:
        launch = self.project("Launch")
        bodies = [
            self._get("read me", "/users/me", {"opt_fields": "name,email"}).text,
            self._get(
                "list the project's tasks",
                f"/projects/{launch}/tasks",
                {"opt_fields": "name,notes,completed,completed_at,assignee.email,created_at,modified_at,tags.name"},
            ).text,
        ]
        return "\n".join(bodies)

    def change(self, label: str) -> None:
        self.create_ticket("Launch", label, "")

    # -------------------------------------------------------------------- tickets

    def ticket_ids(self) -> dict[str, str]:
        found: dict[str, str] = {}
        projects = self._all("list projects", "/projects", {"workspace": self.workspace(), "opt_fields": "name"})
        for project in map(Named.model_validate, projects):
            tasks = self._all("list a project's tasks", f"/projects/{project.gid}/tasks", {"opt_fields": "name"})
            for task in map(Named.model_validate, tasks):
                found[task.name] = task.gid
        return found

    def read_ticket(self, ticket: str) -> TicketSeen:
        task = self._task(ticket)
        stories = self._stories(ticket)
        place = task.memberships[0] if task.memberships else None
        section = place.section.name if place is not None and place.section is not None else None
        if section == CANCELLED:
            progress = Progress.CANCELLED
        elif task.completed or section == DONE:
            progress = Progress.DONE
        elif section == IN_PROGRESS:
            progress = Progress.IN_PROGRESS
        else:
            progress = Progress.OPEN
        latest = stories[-1].created_by if stories else None
        return TicketSeen(
            id=task.gid,
            title=task.name,
            body=task.notes,
            progress=progress,
            assignee_email=task.assignee.email if task.assignee is not None else None,
            assignee_id=task.assignee.gid if task.assignee is not None else None,
            labels=frozenset(t.name for t in task.tags),
            comments=tuple(
                CommentSeen(author_email=s.created_by.email if s.created_by is not None else None, text=s.text)
                for s in stories
                if s.resource_subtype == COMMENT
            ),
            key=None,
            created=_parsed(task.created_at),
            links=tuple(dict.fromkeys(r.gid for r in [*task.dependencies, *task.dependents])),
            project=place.project.name if place is not None and place.project is not None else None,
            changed_by_email=latest.email if latest is not None else None,
        )

    def create_ticket(self, project: str, title: str, body: str) -> str:
        made = self._send(
            "create a task", "POST", "/tasks", {"name": title, "notes": body, "projects": [self.project(project)]}
        )
        return Ref.model_validate(One.model_validate_json(made.content).data).gid

    def set_progress(self, ticket: str, progress: Progress) -> None:
        """Into the section that says it on the task's board (made, for `In progress`, where the board has none),
        and the `completed` box ticked unless it is open or in progress."""
        task = self._task(ticket)
        place = task.memberships[0] if task.memberships else None
        if place is not None and place.project is not None:
            sections = self._sections(place.project.gid)
            name = SECTION_OF[progress]
            section = sections[name] if name in sections else None
            if section is None:
                made = self._send("create a section", "POST", f"/projects/{place.project.gid}/sections", {"name": name})
                section = Ref.model_validate(One.model_validate_json(made.content).data).gid
            self._send("move a task to a section", "POST", f"/sections/{section}/addTask", {"task": ticket})
        completed = progress in (Progress.DONE, Progress.CANCELLED)
        self._send("complete or reopen a task", "PUT", f"/tasks/{ticket}", {"completed": completed})

    def assign(self, ticket: str, person: str | None) -> None:
        self._send("assign a task", "PUT", f"/tasks/{ticket}", {"assignee": person})

    def comment(self, ticket: str, text: str) -> None:
        self._send("comment on a task", "POST", f"/tasks/{ticket}/stories", {"text": text})

    def link(self, ticket: str, other: str) -> None:
        """`other` becomes a dependency of `ticket` (https://developers.asana.com/reference/adddependenciesfortask)."""
        self._send("link two tasks", "POST", f"/tasks/{ticket}/addDependencies", {"dependencies": [other]})

    def relabel(self, ticket: str, labels: list[str]) -> None:
        held = {t.name: t.gid for t in self._task(ticket).tags}
        known = self._tags()
        for name in labels:
            if name in held:
                continue
            gid = known[name] if name in known else None
            if gid is None:
                made = self._send("create a tag", "POST", f"/workspaces/{self.workspace()}/tags", {"name": name})
                gid = Ref.model_validate(One.model_validate_json(made.content).data).gid
            self._send("tag a task", "POST", f"/tasks/{ticket}/addTag", {"tag": gid})
        for name, gid in held.items():
            if name not in labels:
                self._send("untag a task", "POST", f"/tasks/{ticket}/removeTag", {"tag": gid})

    def delete_ticket(self, ticket: str) -> None:
        ok("delete a task", self.http.delete(f"/tasks/{ticket}"))

    def search_tickets(self, text: str) -> list[str]:
        """Asana's advanced search (https://developers.asana.com/reference/searchtasksforworkspace)."""
        found = self._get(
            "search tasks",
            f"/workspaces/{self.workspace()}/tasks/search",
            {"text": text, "limit": PAGE, "opt_fields": "name"},
        )
        return [Ref.model_validate(t).gid for t in Page.model_validate_json(found.content).data]

    def ticket_pages(self, project: str, page_size: int) -> list[list[str]]:
        return [
            [Ref.model_validate(t).gid for t in page.data]
            for page in self._pages("list a project's tasks", f"/projects/{self.project(project)}/tasks", {}, page_size)
        ]


# ---------------------------------------------------------------------------------------------- faults


def _sdk(api: Api, token: str) -> asana.ApiClient:  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    """Asana's own Python client, through the proxy and trusting its CA. It is told not to sleep out a 429's
    `Retry-After` itself, so the refusal reaches the caller."""
    configuration = asana.Configuration()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    configuration.access_token = token  # pyright: ignore[reportUnknownMemberType]
    configuration.proxy = api.proxy  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
    configuration.ssl_ca_cert = api.bundle  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
    configuration.retry_strategy = urllib3.Retry(total=0, respect_retry_after_header=False, raise_on_status=False)  # pyright: ignore[reportUnknownMemberType]
    return asana.ApiClient(configuration)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def _read_me(api: Api, world: WorldView) -> None:
    client = _sdk(api, DRIVER.credential_of(world, None) or "")  # pyright: ignore[reportUnknownVariableType]
    asana.UsersApi(client).get_user("me", {})  # pyright: ignore[reportUnknownMemberType]


def _raised(api: Api, world: WorldView) -> BaseException | None:
    try:
        _read_me(api, world)
    except ApiException as raised:  # pyright: ignore[reportUnknownVariableType]
        return raised  # pyright: ignore[reportUnknownVariableType]
    return None


# ---------------------------------------------------------------------------------------------- the driver


def _token(tag: str, person: str | None) -> str:
    return f"pat-{tag}" if person is None else f"pat-{tag}-{person}"


class AsanaDriver(Driver):
    provider: ClassVar[str] = PROVIDER
    session: ClassVar[type[Session]] = AsanaSession
    absent: ClassVar[Mapping[str, str]] = {
        "accounts.title": "An Asana user resource carries a gid, name, email, photo and workspaces; it has no job title.",
        "accounts.bot": "Asana's user resource has no field saying an account is a bot or an app; integrations act "
        "as ordinary users or service accounts the API does not mark.",
        "accounts.no_email": "Every Asana account signs in by its email address, so no Asana user exists without one.",
        "accounts.vendor_login": "Asana has no login apart from the account's email address; there is no username.",
    }
    id_formats: ClassVar[Mapping[IdKind, re.Pattern[str]]] = {
        # Asana's gids are strings of digits: "gid: Globally unique identifier of the resource, as a string"
        # (https://developers.asana.com/docs/object-hierarchy), e.g. "12345"; CLAIMS.md: "a tag is named by its gid",
        # and the provider's own `wire.is_gid` holds every gid to digits.
        IdKind.PERSON: re.compile(r"^[0-9]+$"),
        # A task is a resource like any other and its gid the same string of digits
        # (https://developers.asana.com/reference/gettask).
        IdKind.TICKET: re.compile(r"^[0-9]+$"),
    }
    page_floor: ClassVar[Mapping[str, int]] = {
        # "limit: Results per page. The number of objects to return per page. The value must be between 1 and 100."
        # (https://developers.asana.com/docs/pagination)
        "people": 1,
        "tickets": 1,
    }
    unknown_refusal: ClassVar[tuple[int, str]] = (401, "Not Authorized")
    """https://developers.asana.com/docs/errors: 401 Unauthorized, "Not Authorized" (also CLAIMS.md)."""

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise NotImplementedError("Asana has no login apart from an account's email address")
        people = seed["people"] if "people" in seed else []
        assert isinstance(people, list)
        keys: list[str] = []
        for person in people:  # pyright: ignore[reportUnknownVariableType]
            assert isinstance(person, dict)
            key = person["key"]  # pyright: ignore[reportUnknownVariableType]
            assert isinstance(key, str)
            keys.append(key)
        tokens = [_token(tag, None), *(_token(tag, k) for k in keys)]
        mine: list[dict[str, object]] = [{"token": tokens[0]}, *({"token": _token(tag, k), "person": k} for k in keys)]
        given = seed["provider_seeds"] if "provider_seeds" in seed else []
        assert isinstance(given, list)
        seeds: list[object] = []
        merged = False
        for entry in given:  # pyright: ignore[reportUnknownVariableType]
            assert isinstance(entry, dict)
            if entry["provider"] != PROVIDER:
                seeds.append(entry)
                continue
            raw = entry["body"]  # pyright: ignore[reportUnknownVariableType]
            body = json.loads(raw) if isinstance(raw, str) else dict(raw)  # pyright: ignore[reportUnknownArgumentType]
            assert isinstance(body, dict)
            held = body["tokens"] if "tokens" in body else []  # pyright: ignore[reportUnknownVariableType]
            assert isinstance(held, list)
            seeds.append({"provider": PROVIDER, "body": {**body, "tokens": [*held, *mine]}})
            merged = True
        if not merged:
            seeds.append({"provider": PROVIDER, "body": {"tokens": mine}})
        return CreateWorld.model_validate({"seed": {**seed, "provider_seeds": seeds}, "claims": {"tokens": tokens}})

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> AsanaSession:
        token = self.credential_of(world, person)
        assert token is not None
        return AsanaSession(api, token)

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        agent = world.claims.tokens[0]
        token = agent if person is None else f"{agent}-{person}"
        if token not in world.claims.tokens:
            raise LookupError(f"the world claims no Asana token for {person!r}")
        return token

    def faults(self) -> list[FaultCase]:
        """`AsanaSeed.rate_limits`, the one fault kind `provider-faults` takes that lapses: every call is 429
        with `Retry-After` for its stretch (https://developers.asana.com/docs/rate-limits)."""

        def then(api: Api, world: WorldView) -> None:
            _read_me(api, world)

        return [
            FaultCase(
                name="rate_limits",
                fragment={"rate_limits": [{"after": "PT0S", "lasts": "PT1M"}]},
                trigger=_raised,
                typed=ApiException,  # pyright: ignore[reportUnknownArgumentType]
                status=429,
                holds="You have made too many requests recently",
                expires_after=timedelta(minutes=1),
                then=then,
            )
        ]


DRIVER = AsanaDriver()
