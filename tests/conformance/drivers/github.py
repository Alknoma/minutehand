"""GitHub, through its REST API at api.github.com, as a client holding personal access tokens.

GitHub is in the accounts family only: the provider answers the reads a code-reading client makes (its README's
table). The accounts it lists are an organization's members (`GET /orgs/{org}/members`, then `GET /users/{login}`
for each one's name, public email and type), and the one write a reading client's world changes through is a
commit, made the way any client makes one: `PUT /repos/{owner}/{repo}/contents/{path}`.

Every request carries what GitHub's REST reference asks a client to send: `Accept: application/vnd.github+json`,
`X-GitHub-Api-Version: 2022-11-28` and `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime

import httpx

from minutehand.adapters.control.wire import CreateWorld, WorldView
from minutehand.domain.scenario import Account
from tests.conformance.contract import (
    Api,
    Driver,
    FaultCase,
    IdKind,
    PersonSeen,
    Session,
    ok,
)

API = "https://api.github.com"
VERSION = "2022-11-28"
ORGANIZATION = "launch-team"
REPOSITORY = "notes"
AGENT_LOGIN = "the-agent"
AGENT_NAME = "The Agent"
TOKEN_PREFIX = "ghp_"
"""A classic personal access token, the kind GitHub writes `ghp_` on (README, "Credentials")."""
AGENT_SUFFIX = "agent"
NOBODY_SUFFIX = "nobody"
PERSON_MARK = "u"

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
"""GitHub writes every timestamp as ISO 8601 in UTC, to the second, with a Z (REST reference, "Timezones")."""

NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": VERSION,
        "Authorization": f"Bearer {token}",
    }


def _agent_token(tag: str) -> str:
    return f"{TOKEN_PREFIX}{tag}{AGENT_SUFFIX}"


def _nobody_token(tag: str) -> str:
    """A well-formed classic token no world claims and GitHub's seed never issues: an unknown credential."""
    return f"{TOKEN_PREFIX}{tag}{NOBODY_SUFFIX}"


def _person_token(tag: str, person: str) -> str:
    return f"{TOKEN_PREFIX}{tag}{PERSON_MARK}{person}"


def _tag_of(world: WorldView) -> str:
    """The tag the world was made with: its first claimed token is the agent's, `ghp_<tag>agent`."""
    first = world.claims.tokens[0]
    return first[len(TOKEN_PREFIX) : -len(AGENT_SUFFIX)]


def _login_for(person: str) -> str:
    """A GitHub login for a person key: letters and digits (a login holds no underscore)."""
    return person.replace("_", "")


def _parsed(stamp: str) -> datetime:
    return datetime.strptime(stamp, TIME_FORMAT).replace(tzinfo=UTC)


def _digest(label: str) -> str:
    return hashlib.sha1(label.encode()).hexdigest()[:16]


def _entry(**fields: object) -> dict[str, object]:
    return dict(fields)


def _identity(entry: Mapping[str, object], key: str) -> object:
    if key == "repositories":
        return (str(entry["owner"]).lower(), str(entry["name"]).lower())
    if key == "tokens":
        return entry["token"]
    return str(entry["login"]).lower()


def _merged(existing: dict[str, object], mine: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    """`mine` added to a GitHub seed a property already gave, every list kept, nothing it named given twice."""
    found = dict(existing)
    for key, entries in mine.items():
        held = found.get(key, [])
        assert isinstance(held, list)
        seen = {_identity(e, key) for e in held}
        found[key] = [*held, *(e for e in entries if _identity(e, key) not in seen)]
    return found


# ---------------------------------------------------------------------------------------------- the session


class GitHubSession(Session):
    def __init__(self, api: Api, token: str, nobody: str) -> None:
        self._api = api
        self._token = token
        self._nobody = nobody
        self._http = api.http(_headers(token), base_url=API)
        self.secrets = (token, nobody)

    def close(self) -> None:
        self._http.close()

    def _get(self, what: str, url: str, params: Mapping[str, str | int] | None = None) -> httpx.Response:
        return ok(what, self._http.get(url, params=dict(params or {})))

    def _pages(self, what: str, url: str, per_page: int) -> list[list[dict[str, object]]]:
        """Each page of a listing, following `Link: rel="next"` as GitHub's paging guide says to."""
        pages: list[list[dict[str, object]]] = []
        answered = self._get(what, url, {"per_page": per_page})
        while True:
            body = answered.json()
            if not isinstance(body, list):
                raise AssertionError(f"{what}: GitHub answered {type(body).__name__}, not a list")
            pages.append(body)
            following = NEXT.search(answered.headers.get("link", ""))
            if following is None:
                return pages
            answered = self._get(what, following.group(1))

    def whoami(self) -> str:
        login = self._get("get the authenticated user", "/user").json()["login"]
        assert isinstance(login, str)
        return login

    def _person(self, login: str, *, guest: bool) -> PersonSeen:
        user = self._get(f"get the user {login}", f"/users/{login}").json()
        return PersonSeen(
            id=str(user["id"]),
            name=user["name"] or user["login"],
            email=user["email"],
            bot=user["type"] == "Bot",  # enum-lint: exempt GitHub's account `type` on the wire
            guest=guest,
            login=user["login"],
        )

    def people(self) -> list[PersonSeen]:
        """The organization's members, then its outside collaborators (GitHub's guests), each read by login."""
        members = [m for page in self._pages("list organization members", f"/orgs/{ORGANIZATION}/members", 100)
                   for m in page]  # fmt: skip
        outside = [
            m
            for page in self._pages("list outside collaborators", f"/orgs/{ORGANIZATION}/outside_collaborators", 100)
            for m in page
        ]
        found = [self._person(str(m["login"]), guest=False) for m in members]
        found += [self._person(str(m["login"]), guest=True) for m in outside]
        return found

    def people_pages(self, page_size: int) -> list[list[str]]:
        pages = self._pages("list organization members", f"/orgs/{ORGANIZATION}/members", page_size)
        return [[str(m["id"]) for m in page] for page in pages]

    def stranger(self, *, credentialed: bool) -> httpx.Response:
        headers = _headers(self._nobody)
        if not credentialed:
            del headers["Authorization"]
        with self._api.http(headers, base_url=API) as client:
            return client.get("/user")

    def observe(self) -> str:
        """Who the token is, the repository's root, and its history: what a commit changes."""
        answers = [
            self._get("get the authenticated user", "/user"),
            self._get("get repository content", f"/repos/{ORGANIZATION}/{REPOSITORY}/contents/"),
            self._get("list commits", f"/repos/{ORGANIZATION}/{REPOSITORY}/commits", {"per_page": 100}),
        ]
        return "\n".join(a.text for a in answers)

    def _commit_file(self, path: str, label: str) -> dict[str, object]:
        """`PUT /repos/{owner}/{repo}/contents/{path}`: a new file, committed with `label` as its message."""
        body = {"message": label, "content": base64.b64encode(label.encode()).decode("ascii")}
        answered = ok(
            "create or update file contents",
            self._http.put(f"/repos/{ORGANIZATION}/{REPOSITORY}/contents/{path}", json=body),
        )
        commit = answered.json()["commit"]
        assert isinstance(commit, dict)
        return commit

    def change(self, label: str) -> None:
        self._commit_file(f"changes/{_digest(label)}.md", label)

    def stamp(self, label: str) -> tuple[str, datetime]:
        commit = self._commit_file(f"stamps/{_digest(label)}.md", label)
        sha, author = commit["sha"], commit["author"]
        assert isinstance(sha, str) and isinstance(author, dict)
        return sha, _parsed(author["date"])

    def stamped(self, made: str) -> datetime:
        """The commit listed from its own sha (`commits?sha=`), first in the answer."""
        listed = self._get(
            "list commits", f"/repos/{ORGANIZATION}/{REPOSITORY}/commits", {"sha": made, "per_page": 1}
        ).json()
        if not listed or listed[0]["sha"] != made:
            raise AssertionError(f"GitHub listed no commit {made} from its own sha")
        return _parsed(listed[0]["commit"]["author"]["date"])


# ---------------------------------------------------------------------------------------------- faults


def _faulted_call(api: Api, world: WorldView) -> BaseException | None:
    """`GET /user` as the agent. No GitHub client library is installed, so the call is httpx's (through the proxy,
    trusting its CA, as `Api.http` configures it) and the error is httpx's `HTTPStatusError`."""
    with api.http(_headers(_agent_token(_tag_of(world))), base_url=API) as client:
        try:
            client.get("/user").raise_for_status()
        except httpx.HTTPStatusError as error:
            return error
    return None


def _then(api: Api, world: WorldView) -> None:
    with api.http(_headers(_agent_token(_tag_of(world))), base_url=API) as client:
        ok("get the authenticated user", client.get("/user"))


# ---------------------------------------------------------------------------------------------- the driver


class GitHubDriver(Driver):
    provider = "github"
    session = GitHubSession
    # A login is letters, digits and single dashes, no dot (GitHub's sign-up rule); uppercase is kept as typed.
    vendor_login = "Lena-Ortiz-QA"
    absent = {
        "accounts.title": "A GitHub account has a name, company, location and bio, and no job title anywhere in "
        "the REST or GraphQL user objects.",
        "accounts.deactivated": "GitHub.com keeps no deactivated account an organization still lists: a removed "
        "member drops out of the members listing, and suspending a user exists only on GitHub Enterprise Server.",
    }
    id_formats = {
        # A user's `id` is a positive integer in every user object of the REST reference
        # (https://docs.github.com/en/rest/users/users#get-a-user, schema `id: integer`); the provider's seed numbers
        # each account from its login (`seed.number`), a positive integer.
        IdKind.PERSON: re.compile(r"^[1-9][0-9]*$"),
    }
    page_floor = {
        # `per_page` on "List organization members": "The number of results per page (max 100)", default 30; one
        # is the smallest positive page (https://docs.github.com/en/rest/orgs/members#list-organization-members).
        "people": 1,
    }
    # https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api: an invalid token is
    # 401 "Bad credentials". The provider deliberately does not enforce credentials (its README, "Credentials"), so
    # once a world claims a call it answers it; an unclaimed one never reaches it.

    def world(self, seed: dict[str, object], tag: str, *, logins: Mapping[str, str] | None = None) -> CreateWorld:
        named = dict(logins or {})
        people = seed.get("people", [])
        assert isinstance(people, list)
        keys = [str(p["key"]) for p in people]
        guests = {str(p["key"]) for p in people if Account(p.get("account", Account.MEMBER)) is Account.GUEST}
        login = {k: named.get(k, _login_for(k)) for k in keys}
        users: list[dict[str, object]] = [{"login": AGENT_LOGIN, "name": AGENT_NAME}]
        users += [_entry(login=login[k], person=k) for k in keys]
        tokens: list[dict[str, object]] = [{"token": _agent_token(tag), "kind": "classic", "login": AGENT_LOGIN}]
        tokens += [_entry(token=_person_token(tag, k), kind="classic", login=login[k]) for k in keys]
        mine: dict[str, list[dict[str, object]]] = {
            "users": users,
            "organizations": [
                {
                    "login": ORGANIZATION,
                    "name": "Launch",
                    "members": [AGENT_LOGIN, *(login[k] for k in keys if k not in guests)],
                }
            ],
            "tokens": tokens,
            "repositories": [
                {
                    "owner": ORGANIZATION,
                    "name": REPOSITORY,
                    "files": [{"path": "README.md", "text": "Launch notes\n"}],
                    "commits": [
                        {
                            "message": "Start the launch notes",
                            "author": AGENT_LOGIN,
                            "before": "P1D",
                            "paths": ["README.md"],
                        }
                    ],
                    "collaborators": [
                        {"login": AGENT_LOGIN, "permission": "admin"},
                        *({"login": login[k], "permission": "push"} for k in keys),
                    ],
                }
            ],
        }
        found = dict(seed)
        given = found.get("provider_seeds", [])
        assert isinstance(given, list)
        entries: list[dict[str, object]] = []
        placed = False
        for entry in given:
            assert isinstance(entry, dict)
            if entry.get("provider") != self.provider:
                entries.append(entry)
                continue
            body = entry["body"]
            existing = json.loads(body) if isinstance(body, str) else dict(body)
            entries.append({"provider": self.provider, "body": json.dumps(_merged(existing, mine))})
            placed = True
        if not placed:
            entries.append({"provider": self.provider, "body": json.dumps(_merged({}, mine))})
        found["provider_seeds"] = entries
        claims = [_agent_token(tag), *(_person_token(tag, k) for k in keys)]
        return CreateWorld.model_validate({"seed": found, "claims": {"tokens": claims}})

    def connect(self, api: Api, world: WorldView, *, person: str | None = None) -> Session:
        token = self.credential_of(world, person)
        assert token is not None
        return GitHubSession(api, token, _nobody_token(_tag_of(world)))

    def credential_of(self, world: WorldView, person: str | None) -> str | None:
        tag = _tag_of(world)
        return _agent_token(tag) if person is None else _person_token(tag, person)

    def faults(self) -> list[FaultCase]:
        # No GitHub client library is among the dev dependencies: each fault is triggered with httpx, whose
        # `HTTPStatusError` is no GitHub API error type, so `typed` is None.
        return [
            FaultCase(
                name="rate_limited",
                fragment={"faults": [{"kind": "rate_limited", "resource": "core"}]},
                trigger=_faulted_call,
                typed=None,
                status=403,
                holds="API rate limit exceeded",
                then=_then,
            ),
            FaultCase(
                name="secondary_rate_limited",
                fragment={"faults": [{"kind": "secondary_rate_limited"}]},
                trigger=_faulted_call,
                typed=None,
                status=403,
                holds="secondary rate limit",
                then=_then,
            ),
            FaultCase(
                name="server_error",
                fragment={"faults": [{"kind": "server_error"}]},
                trigger=_faulted_call,
                typed=None,
                status=502,
                holds="Server Error",
                then=_then,
            ),
        ]


DRIVER = GitHubDriver()
