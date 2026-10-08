"""Jira Cloud through its REST API v3 at `<site>.atlassian.net`, signed in with Basic auth (an account's email and
an API token), as a client written from Atlassian's REST reference would call it.

No Jira client library is among the dev dependencies, so every call, fault triggers included, is made with httpx
through `Api.http`."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime

import httpx

from minutehand.adapters.control.wire import CreateWorld, WorldView
from tests.conformance.contract import (
    STAMP_PROJECT,
    Api,
    CommentSeen,
    DeclaredId,
    Driver,
    FaultCase,
    IdKind,
    PersonSeen,
    Progress,
    Session,
    Tickets,
    TicketSeen,
    VendorRefused,
    ok,
)

PROVIDER = "jira"
AGENT = "agent"
AGENT_EMAIL = "agent@minutehand.invalid"
V3 = "/rest/api/3"

# Jira Cloud's timestamp: "2026-09-01T09:00:00.000+0000" (REST v3 reference, every `created`/`updated` field).
_JIRA_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4}$")

# The statuses and resolution Jira Cloud's default workflows use for work that will not be done ("Won't Do"), as
# Atlassian's "What are issue statuses, priorities and resolutions?" page names them: a done-category status.
_CANCELLED = "Won't Do"


def jira_time(text: object) -> datetime:
    """A Jira timestamp, parsed strictly in Jira's own format, as an aware UTC datetime."""
    if not isinstance(text, str) or not _JIRA_TIME.match(text):
        raise AssertionError(f"not a Jira timestamp: {text!r}")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(UTC)


# ---------------------------------------------------------------------------------------------- ADF


def adf(text: str) -> dict[str, object]:
    """Plain text as an Atlassian document: a paragraph per blank-line-separated block, a hard break per line."""
    blocks: list[object] = []
    for block in text.split("\n\n"):
        nodes: list[object] = []
        for n, line in enumerate(block.split("\n")):
            if n:
                nodes.append({"type": "hardBreak"})
            if line:
                nodes.append({"type": "text", "text": line})
        blocks.append({"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": blocks}


def adf_text(document: object) -> str:
    """The words of an Atlassian document: its blocks separated by a blank line, a newline per hard break."""
    if document is None:
        return ""
    if not isinstance(document, dict) or document["type"] != "doc":
        raise AssertionError(f"not an Atlassian document: {document!r}")
    return "\n\n".join(_inline(block) for block in document["content"])


def _inline(node: object) -> str:
    if not isinstance(node, dict):
        raise AssertionError(f"not an ADF node: {node!r}")
    kind = node["type"]
    if kind == "text":  # enum-lint: exempt ADF's own node type
        return str(node["text"])
    if kind == "hardBreak":  # enum-lint: exempt ADF's own node type
        return "\n"
    children = node["content"] if "content" in node else []
    return "".join(_inline(child) for child in children)


# ---------------------------------------------------------------------------------------------- reading answers


def _obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"Jira answered {value!r} where an object belongs")
    return {str(k): v for k, v in value.items()}


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError(f"Jira answered {value!r} where a list belongs")
    return list(value)


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise AssertionError(f"Jira answered {value!r} where a string belongs")
    return value


def _email(user: object) -> str | None:
    """A user's email, which Jira leaves out when the account's profile visibility hides it."""
    if user is None:
        return None
    found = _obj(user)
    return _str(found["emailAddress"]) if "emailAddress" in found else None


def _person(user: object) -> PersonSeen:
    found = _obj(user)
    return PersonSeen(
        id=_str(found["accountId"]),
        name=_str(found["displayName"]),
        email=_email(found),
        active=bool(found["active"]),
        bot=found["accountType"] == "app",  # enum-lint: exempt Jira's accountType wire value
    )


def _jql_text(text: str) -> str:
    """A JQL string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------------------------- the session


class JiraSession(Tickets):
    def __init__(self, api: Api, site: str, email: str, token: str) -> None:
        self.site = site
        self.email = email
        self.token = token
        self.secrets = (token,)
        self._api = api
        self.http = api.http(
            {"Authorization": _basic(email, token), "Accept": "application/json"},
            base_url=f"https://{site}.atlassian.net",
        )

    def close(self) -> None:
        self.http.close()

    # -------------------------------------------------------------- accounts

    def whoami(self) -> str:
        me = _obj(ok("myself", self.http.get(f"{V3}/myself")).json())
        return _email(me) or _str(me["displayName"])

    def _users(self, start: int, most: int) -> list[object]:
        answered = self.http.get(f"{V3}/users/search", params={"startAt": start, "maxResults": most})
        return _list(ok("users/search", answered).json())

    def people(self) -> list[PersonSeen]:
        found: list[PersonSeen] = []
        start, most = 0, 100
        while True:
            page = self._users(start, most)
            if not page:
                return found
            found += [_person(u) for u in page]
            start += len(page)

    def people_pages(self, page_size: int) -> list[list[str]]:
        pages: list[list[str]] = []
        start = 0
        while True:
            page = self._users(start, page_size)
            if not page:
                return pages
            pages.append([_person(u).id for u in page])
            start += len(page)

    def stranger(self, *, credentialed: bool) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if credentialed:
            headers["Authorization"] = _basic(self.email, "nobody-seeded-this-token")
        with self._api.http(headers, base_url=f"https://{self.site}.atlassian.net") as stranger:
            return stranger.get(f"{V3}/myself")

    # -------------------------------------------------------------- projects and search

    def _projects(self) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        start = 0
        while True:
            answered = self.http.get(f"{V3}/project/search", params={"startAt": start, "maxResults": 50})
            page = _obj(ok("project/search", answered).json())
            values = [_obj(v) for v in _list(page["values"])]
            found += values
            if page["isLast"] is True or not values:
                return found
            start += len(values)

    def _project(self, name: str) -> dict[str, object]:
        answered = self.http.get(f"{V3}/project/search", params={"query": name, "maxResults": 50})
        page = _obj(ok("project/search", answered).json())
        found = [_obj(v) for v in _list(page["values"]) if _obj(v)["name"] == name]
        if len(found) != 1:
            raise LookupError(f"Jira lists {len(found)} projects named {name!r}")
        return found[0]

    def _search(self, jql: str, fields: str, most: int) -> list[list[dict[str, object]]]:
        pages: list[list[dict[str, object]]] = []
        token: str | None = None
        while True:
            params: dict[str, str | int] = {"jql": jql, "maxResults": most, "fields": fields}
            if token is not None:
                params["nextPageToken"] = token
            page = _obj(ok("search/jql", self.http.get(f"{V3}/search/jql", params=params)).json())
            pages.append([_obj(i) for i in _list(page["issues"])])
            if "nextPageToken" not in page or page["isLast"] is True:
                return pages
            token = _str(page["nextPageToken"])

    def ticket_ids(self) -> dict[str, str]:
        keys = [_str(p["key"]) for p in self._projects()]
        if not keys:
            return {}
        jql = f"project in ({', '.join(keys)}) ORDER BY created ASC"
        return {
            _str(_obj(i["fields"])["summary"]): _str(i["id"])
            for page in self._search(jql, "summary", 100)
            for i in page
        }

    def search_tickets(self, text: str) -> list[str]:
        return [_str(i["id"]) for page in self._search(f"summary ~ {_jql_text(text)}", "id", 100) for i in page]

    def ticket_pages(self, project: str, page_size: int) -> list[list[str]]:
        key = _str(self._project(project)["key"])
        jql = f"project = {key} ORDER BY created ASC"
        return [[_str(i["id"]) for i in page] for page in self._search(jql, "id", page_size) if page]

    # -------------------------------------------------------------- one ticket

    def _changelog(self, ticket: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        start = 0
        while True:
            answered = self.http.get(f"{V3}/issue/{ticket}/changelog", params={"startAt": start, "maxResults": 100})
            page = _obj(ok("changelog", answered).json())
            values = [_obj(v) for v in _list(page["values"])]
            found += values
            if page["isLast"] is True or not values:
                return found
            start += len(values)

    def _comments(self, ticket: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        start = 0
        while True:
            answered = self.http.get(f"{V3}/issue/{ticket}/comment", params={"startAt": start, "maxResults": 100})
            page = _obj(ok("comments", answered).json())
            values = [_obj(v) for v in _list(page["comments"])]
            found += values
            start += len(values)
            if not values or start >= int(str(page["total"])):
                return found

    def read_ticket(self, ticket: str) -> TicketSeen:
        issue = _obj(ok("issue", self.http.get(f"{V3}/issue/{ticket}", params={"fields": "*all"})).json())
        fields = _obj(issue["fields"])
        status = _obj(fields["status"])
        category = _str(_obj(status["statusCategory"])["key"])
        resolution = _obj(fields["resolution"])["name"] if fields["resolution"] is not None else None
        if category == "new":  # enum-lint: exempt Jira's statusCategory key
            progress = Progress.OPEN
        elif category == "indeterminate":  # enum-lint: exempt Jira's statusCategory key
            progress = Progress.IN_PROGRESS
        elif _CANCELLED in (status["name"], resolution):
            progress = Progress.CANCELLED
        else:
            progress = Progress.DONE
        links: list[str] = []
        for entry in _list(fields["issuelinks"]):
            link = _obj(entry)
            other = link["outwardIssue"] if "outwardIssue" in link else link["inwardIssue"]
            links.append(_str(_obj(other)["id"]))
        comments = self._comments(ticket)
        # Jira keeps a comment out of the changelog, so the latest change is the latest of the changelog's entries
        # (`created`, `author`) and the comments' last edits (`updated`, `updateAuthor`); a tie goes to the later.
        changes = [(jira_time(h["created"]), h["author"] if "author" in h else None) for h in self._changelog(ticket)]
        changes += [(jira_time(c["updated"]), c["updateAuthor"]) for c in comments]
        latest = max(enumerate(changes), key=lambda n: (n[1][0], n[0]), default=None)
        assignee = fields["assignee"]
        return TicketSeen(
            id=_str(issue["id"]),
            title=_str(fields["summary"]),
            body=adf_text(fields["description"]),
            progress=progress,
            assignee_email=_email(assignee),
            assignee_id=_str(_obj(assignee)["accountId"]) if assignee is not None else None,
            labels=frozenset(_str(label) for label in _list(fields["labels"])),
            comments=tuple(CommentSeen(author_email=_email(c["author"]), text=adf_text(c["body"])) for c in comments),
            key=_str(issue["key"]),
            created=jira_time(fields["created"]),
            links=tuple(links),
            project=_str(_obj(fields["project"])["name"]),
            changed_by_email=_email(latest[1][1]) if latest is not None else None,
        )

    def create_ticket(self, project: str, title: str, body: str) -> str:
        found = self._project(project)
        project_id = _str(found["id"])
        answered = self.http.get(f"{V3}/issue/createmeta/{project_id}/issuetypes", params={"maxResults": 50})
        types = [_obj(t) for t in _list(_obj(ok("createmeta", answered).json())["issueTypes"])]
        standard = [t for t in types if t["subtask"] is not True]
        chosen = next((t for t in standard if t["name"] == "Task"), standard[0] if standard else None)
        if chosen is None:
            raise LookupError(f"Jira offers no standard issue type in {project!r}")
        fields: dict[str, object] = {
            "project": {"id": project_id},
            "issuetype": {"id": _str(chosen["id"])},
            "summary": title,
        }
        if body:
            fields["description"] = adf(body)
        made = ok("create issue", self.http.post(f"{V3}/issue", json={"fields": fields}))
        return _str(_obj(made.json())["id"])

    def set_progress(self, ticket: str, progress: Progress) -> None:
        answered = self.http.get(f"{V3}/issue/{ticket}/transitions", params={"expand": "transitions.fields"})
        transitions = [_obj(t) for t in _list(_obj(ok("transitions", answered).json())["transitions"])]
        wanted = {
            Progress.OPEN: "new",
            Progress.IN_PROGRESS: "indeterminate",
            Progress.DONE: "done",
            Progress.CANCELLED: "done",
        }[progress]
        fitting = [t for t in transitions if _obj(_obj(t["to"])["statusCategory"])["key"] == wanted]
        named = [t for t in fitting if _obj(t["to"])["name"] == _CANCELLED]
        resolving = [t for t in fitting if "fields" in t and "resolution" in _obj(t["fields"])]
        body: dict[str, object]
        if progress is Progress.CANCELLED and named:
            body = {"transition": {"id": _str(named[0]["id"])}}
        elif progress is Progress.CANCELLED and resolving:
            body = {"transition": {"id": _str(resolving[0]["id"])}, "fields": {"resolution": {"name": _CANCELLED}}}
        elif progress is Progress.DONE:
            plain = [t for t in fitting if _obj(t["to"])["name"] != _CANCELLED]
            if not plain:
                raise LookupError(f"no transition of {ticket} reaches a done status")
            body = {"transition": {"id": _str(plain[0]["id"])}}
        elif progress is not Progress.CANCELLED and fitting:
            body = {"transition": {"id": _str(fitting[0]["id"])}}
        else:
            raise LookupError(f"no transition of {ticket} reaches {progress.value}")
        ok("transition", self.http.post(f"{V3}/issue/{ticket}/transitions", json=body))

    def assign(self, ticket: str, person: str | None) -> None:
        ok("assign", self.http.put(f"{V3}/issue/{ticket}/assignee", json={"accountId": person}))

    def comment(self, ticket: str, text: str) -> None:
        ok("comment", self.http.post(f"{V3}/issue/{ticket}/comment", json={"body": adf(text)}))

    def link(self, ticket: str, other: str) -> None:
        body = {"type": {"name": "Relates"}, "outwardIssue": {"id": ticket}, "inwardIssue": {"id": other}}
        ok("link", self.http.post(f"{V3}/issueLink", json=body))

    def relabel(self, ticket: str, labels: list[str]) -> None:
        ok("relabel", self.http.put(f"{V3}/issue/{ticket}", json={"fields": {"labels": labels}}))

    def delete_ticket(self, ticket: str) -> None:
        ok("delete", self.http.delete(f"{V3}/issue/{ticket}"))

    # -------------------------------------------------------------- what properties 7 and 8 read

    def observe(self) -> str:
        project = self.http.get(f"{V3}/project/search", params={"query": STAMP_PROJECT, "maxResults": 50})
        key = next(_str(_obj(v)["key"]) for v in _list(_obj(ok("project/search", project).json())["values"]))
        jql = f"project = {key} ORDER BY created ASC"
        params = {"jql": jql, "maxResults": 100, "fields": "summary,status,assignee,labels,created,updated"}
        issues = ok("search/jql", self.http.get(f"{V3}/search/jql", params=params))
        return "\n".join([project.text, issues.text])

    def change(self, label: str) -> None:
        self.create_ticket(STAMP_PROJECT, label, "")


def _basic(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


# ---------------------------------------------------------------------------------------------- the driver


def _token(tag: str, account: str) -> str:
    return f"jira-{tag}-{account}"


def _site(world: WorldView) -> str:
    if len(world.claims.keys) != 1:
        raise AssertionError(f"a Jira world claims one site, not {world.claims.keys}")
    return world.claims.keys[0]


def _body(entry: object) -> tuple[bool, dict[str, object]]:
    """Whether a `provider_seeds` entry is Jira's, and its body as structure."""
    found = _obj(entry)
    if found["provider"] != PROVIDER:
        return False, {}
    body = found["body"] if "body" in found else {}
    return True, _obj(json.loads(body) if isinstance(body, str) else body)


class JiraDriver(Driver):
    provider = PROVIDER
    session = JiraSession
    absent = {
        "accounts.title": "A Jira Cloud user object carries no job title: the REST v3 user has an accountId, a "
        "display name, an email, an account type, avatars, a time zone and whether it is active, nothing more.",
        "accounts.guest": "A Jira Cloud user says nothing of being a guest: its accountType is atlassian, app or "
        "customer, and no field of the REST v3 user marks an account as invited from outside.",
        "accounts.vendor_login": "Jira Cloud accounts have no username since Atlassian's 2019 privacy change: an "
        "account is named only by its accountId, its display name and its email.",
    }
    id_formats = {
        # An Atlassian accountId: "5b10ac8d82e05b22cc7d4ef5" (24 hex digits, the REST v3 user reference's example)
        # or, for accounts made since, "<prefix>:<uuid>" ("557058:f58131cb-b67d-43c7-b30d-6b58d40bd077" in
        # Atlassian's GDPR migration guide); at most 128 characters.
        IdKind.PERSON: re.compile(
            r"^(?:[0-9a-f]{24}|\d{6}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
        ),
        # An issue's id is a decimal number written as a string ("10002" in the REST v3 issue reference); its key
        # ("PROJ-12") is a separate field.
        IdKind.TICKET: re.compile(r"^[1-9]\d*$"),
    }
    # `users/search` and `search/jql` take any `maxResults` from 1 (REST v3 reference: "The maximum number of items
    # to return per page"), answering fewer when Jira chooses.
    page_floor = {"people": 1, "tickets": 1}
    # A Basic credential Jira does not accept is a 401 whose body reads "Client must be authenticated to access
    # this resource." (REST v3 reference, "Authentication": 401 when credentials are incorrect or missing). The fake
    # deliberately lets every credential in (the provider's CLAIMS.md), so this case is a known failure.

    def __init__(self) -> None:
        self._emails: dict[str, dict[str, str]] = {}

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        if logins:
            raise NotImplementedError("Jira Cloud accounts have no login of their own")
        people = [_obj(p) for p in _list(seed["people"])] if "people" in seed else []
        others: list[object] = []
        given: dict[str, object] = {}
        for entry in _list(seed["provider_seeds"]) if "provider_seeds" in seed else []:
            mine, body = _body(entry)
            if mine:
                given = body
            else:
                others.append(entry)
        # Basic auth is an account's email and its API token, so a person seeded with no email gets no credential.
        accounts = [AGENT, *(_str(p["key"]) for p in people if "email" in p)]
        credentials = [{"account": a, "api_token": _token(tag, a)} for a in accounts]
        given_credentials = _list(given["credentials"]) if "credentials" in given else []
        body = {"site": tag, "agent_email": AGENT_EMAIL} | given | {"credentials": [*credentials, *given_credentials]}
        site = _str(body["site"])
        agent_email = _str(body["agent_email"])
        self._emails[site] = {AGENT: agent_email} | {_str(p["key"]): _str(p["email"]) for p in people if "email" in p}
        whole = seed | {"provider_seeds": [*others, {"provider": PROVIDER, "body": body}]}
        return CreateWorld.model_validate(
            {
                "seed": whole,
                "claims": {"keys": [site], "tokens": [_token(tag, a) for a in accounts]},
            }
        )

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        site = _site(world)
        account = person or AGENT
        held = site in self._emails and account in self._emails[site]
        return _token(site, account) if held else None

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        site = _site(world)
        if site not in self._emails:
            raise LookupError(f"no Jira world for site {site!r} was made by this driver")
        account = person or AGENT
        emails = self._emails[site]
        if account not in emails:
            raise NotImplementedError(f"the Jira world {site!r} gives {account!r} no email, so no API token signs in")
        return JiraSession(api, site, emails[account], _token(site, account))

    def faults(self) -> list[FaultCase]:
        # Jira declares one fault kind, `rate_limits`: a method and path prefix answered 429 with Retry-After. No
        # Jira client library is installed, so the call is made with httpx (`typed=None`).
        fragment = {"rate_limits": [{"method": "GET", "path": f"{V3}/myself", "times": 1, "retry_after": 1}]}

        def call(api: Api, world: WorldView) -> httpx.Response:
            with self.connect(api, world) as session:
                assert isinstance(session, JiraSession)
                return session.http.get(f"{V3}/myself")

        def trigger(api: Api, world: WorldView) -> BaseException | None:
            try:
                ok("myself", call(api, world))
            except VendorRefused as refused:
                return refused
            return None

        def then(api: Api, world: WorldView) -> None:
            ok("myself", call(api, world))

        # Atlassian's rate-limiting page documents the status, 429 Too Many Requests, and its headers (Retry-After,
        # X-RateLimit-*, RateLimit-Reason), and no wording of the body: the phrase held is the empty one.
        return [
            FaultCase(
                name="rate_limits",
                fragment=fragment,
                trigger=trigger,
                typed=None,
                status=429,
                holds="",
                uses=1,
                then=then,
            )
        ]

    def declared_ids(self) -> list[DeclaredId]:
        # The Jira seed may name a project's key (`SeededProject.key`, README "Seeding"); an issue's key is the
        # project's key and its number, so the project's first seeded ticket reads back as LCH-1.
        def read(session: Session) -> str:
            assert isinstance(session, JiraSession)
            first = session.ticket_pages(STAMP_PROJECT, 50)[0][0]
            return session.read_ticket(first).key or ""

        seed = {
            "provider_seeds": [{"provider": PROVIDER, "body": {"projects": [{"name": STAMP_PROJECT, "key": "LCH"}]}}]
        }
        return [DeclaredId(name="project key", seed=seed, read=read, expected="LCH-1")]


DRIVER = JiraDriver()
